"""DEMO_TICKETS B4 — show the interpretation and double-check the hours and the
classification.

Hours and classification are multiplicands and selectors in the money math. The app
states what it read ('Read as: 8 h · overtime · <classification> · no date given'),
asks when the hours are ambiguous instead of multiplying by the first number it sees,
and asks which classification when a description matches several rows.
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import auth  # noqa: E402
from core import costing, llm  # noqa: E402
from core.caseio import load_case  # noqa: E402

CASE_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "cases", "santacruz")
FF = "Firefighter/Paramedic (56 hr, top step)"
HEADLINE = f"Cost an 8-hour overtime shift for a {FF}"
LABELS = [s["name"] for s in load_case(CASE_SRC).subjects()]


@pytest.fixture
def case_dir(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE_SRC, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_BACKEND", "bm25")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    # the per-IP chat spend cap (20/min) is for visitors, not for a test file
    monkeypatch.setattr(auth.RateLimiter, "allow", lambda self, key: True)
    return case


@pytest.fixture
def client(case_dir):
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def _ask(client, prompt, **extra):
    return client.post("/chat", json={"prompt": prompt, **extra}).json()


# (a) extract_hours ---------------------------------------------------------------------
@pytest.mark.parametrize("prompt, candidates, invalid", [
    (HEADLINE, [8.0], []),
    ("Cost overtime for a 56-hour Firefighter working 8 hours", [56.0, 8.0], []),
    ("After a 24-hour shift, cost 4 hours of overtime", [24.0, 4.0], []),
    ("Cost an -8-hour overtime shift", [], ["8-hour"]),
    ("Cost 2,5 hours of overtime", [], ["5 hours"]),
    ("Cost eight hours of overtime", [8.0], []),
    ("Cost 12 hr of overtime", [12.0], []),
    ("an 8 hour shift and then a 12 hour shift", [8.0, 12.0], []),
    ("Cost 99999999-hour overtime", [99999999.0], []),
    ("Cost 2.5 hours of overtime", [2.5], []),
    ("twenty-four hours of standby", [24.0], []),
    ("Cost a shift for a Fire Captain (40 hr top step)", [], []),
])
def test_extract_hours_table(prompt, candidates, invalid):
    out = costing.extract_hours(prompt, LABELS)
    assert out["candidates"] == candidates, out
    assert out["invalid"] == invalid, out


# (b) chat: ambiguous or unusable hours ask instead of multiplying ----------------------
@pytest.mark.parametrize("prompt, options", [
    ("Cost overtime for a 56-hour Firefighter working 8 hours", ["56", "8"]),
    (f"After a 24-hour shift, cost 4 hours of overtime for a {FF}", ["24", "4"]),
    (f"Cost an 8 hour shift and then a 12 hour shift of overtime for a {FF}", ["8", "12"]),
])
def test_several_hour_candidates_clarify(client, prompt, options):
    res = _ask(client, prompt)
    assert res["mode"] == "clarify", res
    assert res["field"] == "hours"
    assert res["options"] == options
    assert "result" not in res


def test_hours_over_the_cap_clarify(client):
    res = _ask(client, f"Cost a 99999999-hour overtime shift for a {FF}")
    assert res["mode"] == "clarify" and res["field"] == "hours", res
    assert res["options"] == []
    assert "96" in res["question"]


def test_glued_number_clarifies(client):
    res = _ask(client, f"Cost 2,5 hours of overtime for a {FF}")
    assert res["mode"] == "clarify" and res["field"] == "hours", res


def test_no_hours_asks_how_many(client):
    res = _ask(client, f"Cost an overtime shift for a {FF}")
    assert res["mode"] == "clarify" and res["field"] == "hours", res
    assert "how many hours" in res["question"].lower()


def test_number_words_are_hours(client):
    res = _ask(client, f"Cost eight hours of overtime for a {FF}")
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 640.80
    assert res["interpretation"]["hours"] == 8.0


def test_clarified_hours_must_be_a_candidate(client):
    prompt = "Cost overtime for a 56-hour Firefighter working 8 hours"
    first = _ask(client, prompt)
    assert first["mode"] == "clarify"
    res = _ask(client, prompt, query_id=first["query_id"], clarified={"hours": "8"})
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 582.48                    # 48.54 x 1.5 x 8
    assert res["interpretation"]["hours"] == 8.0
    # a value the question never stated is not accepted
    res = _ask(client, prompt, query_id=first["query_id"], clarified={"hours": "10"})
    assert res["mode"] == "clarify" and res["field"] == "hours"


# (c) a model number that is not an hour candidate is unverified -----------------------
def test_model_hours_equal_to_a_label_number_are_unverified():
    subjects = load_case(CASE_SRC).subjects()
    out = llm._normalize_intent({"source": "claude", "hours": 56.0, "subjects": [FF]},
                                subjects, prompt=HEADLINE)
    assert out["hours"] == 0.0
    assert out["unverified_numbers"] == {"hours": 56.0}
    ok = llm._normalize_intent({"source": "claude", "hours": 8.0, "subjects": [FF]},
                               subjects, prompt=HEADLINE)
    assert ok["hours"] == 8.0 and "unverified_numbers" not in ok


# (d) several classifications: ask, unless typed verbatim or asked as a group ----------
def test_description_matching_several_rows_asks_which(client):
    res = _ask(client, "Cost 8 hours of overtime for a Firefighter/Paramedic top step.")
    assert res["mode"] == "clarify", res
    assert res["field"] == "subject"
    assert FF in res["options"] and len(res["options"]) == 3
    assert "result" not in res
    picked = _ask(client, "Cost 8 hours of overtime for a Firefighter/Paramedic top step.",
                  query_id=res["query_id"], clarified={"subject": FF})
    assert picked["mode"] == "costing" and picked["result"]["total"] == 640.80
    assert picked["interpretation"]["subjects"] == [FF]


def test_group_words_and_exact_labels_do_not_ask(client):
    res = _ask(client, "Cost an 8-hour overtime shift for all classifications")
    assert res["mode"] == "costing", res
    assert len(res["result"]["line_items"]) > 1
    res = _ask(client, f"Cost an 8-hour overtime shift for a {FF} and a "
                       "Firefighter (56 hr, top step)")
    assert res["mode"] == "costing", res
    assert len(res["result"]["line_items"]) == 2
    res = _ask(client, "Cost an 8-hour overtime shift for the firefighters")
    assert res["mode"] == "costing", res


# (e) every costing answer carries the interpretation the engine actually used ---------
def test_interpretation_matches_engine_inputs(client, case_dir):
    res = _ask(client, HEADLINE)
    assert res["mode"] == "costing"
    interp = res["interpretation"]
    assert interp["read_as"] == f"8 h · overtime · {FF} · no date given"
    assert interp["hours"] == 8.0 and interp["pay_type"] == "overtime"
    assert interp["subjects"] == [FF] and interp["date_iso"] is None
    assert interp["parsed_via"] == "stub"
    snap = json.load(open(case_dir / "snapshots" / f"{res['query_id']}.json"))
    assert snap["params"]["hours"] == interp["hours"]
    events = core_app._case().ledger().for_query(res["query_id"])
    ci = [e for e in events if e["type"] == "chat.interpretation"]
    assert len(ci) == 1 and ci[0]["payload"]["read_as"] == interp["read_as"]


def test_interpretation_states_an_assumed_year(client):
    res = _ask(client, f"{HEADLINE} on July 4")
    assert res["mode"] == "costing", res
    assert res["interpretation"]["date_iso"] == "2026-07-04"
    assert res["interpretation"]["date_note"] == "2026-07-04 (year assumed)"
    assert res["interpretation"]["read_as"].endswith("· 2026-07-04 (year assumed)")


def test_refusal_and_blocked_carry_the_interpretation(client):
    res = _ask(client, f"Cost a regular 8-hour shift for a {FF}")
    assert res["mode"] == "refused" and res["interpretation"]["pay_type"] == "regular"
    res = _ask(client, "Cost an 8-hour overtime shift for a Fire Marshal (top step)")
    assert res["mode"] == "blocked" and res["interpretation"]["hours"] == 8.0
