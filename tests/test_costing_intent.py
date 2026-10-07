"""DEMO_TICKETS B1 — typed costing intent with a clean refusal.

The pay branch a question asks for (overtime, regular, holiday, callback ...) is read
deterministically from the case's lexicon and must match the topic of a ratified base
rule for the subject's unit. Otherwise: refuse with the reason, what IS approved, and the
nearest clause as text — never a number from the wrong formula.
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import auth  # noqa: E402
from core import costing  # noqa: E402

CASE_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "cases", "santacruz")
FF = "Firefighter/Paramedic (56 hr, top step)"
HEADLINE = f"Cost an 8-hour overtime shift for a {FF}"
OFF_SCRIPT = [
    f"Cost a regular 8-hour shift for a {FF}",
    f"Cost an 8-hour regular straight-time shift (no overtime) for a {FF}",
    f"Cost an 8-hour holiday shift for a {FF} on July 4",
    f"Cost a 2-hour callback for a {FF}",
    f"Cost a 24-hour regular shift for a {FF}, no overtime",
]


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


def _types(qid):
    return [e["type"] for e in core_app._case().ledger().for_query(qid)]


# (a) the headline question is typed overtime and still 640.80 -------------------------
def test_headline_is_overtime_and_640_80(client):
    res = _ask(client, HEADLINE)
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 640.80
    assert res["interpretation"]["pay_type"] == "overtime"
    assert res["params"]["pay_type"] == "overtime"


# (b) off-script pay branches refuse: no number, a reason, the nearest clause ----------
@pytest.mark.parametrize("prompt", OFF_SCRIPT)
def test_off_script_pay_branch_is_refused(client, case_dir, prompt):
    res = _ask(client, prompt)
    assert res["mode"] == "refused", res
    assert "result" not in res
    assert res["approved_topics"] == ["overtime"]
    assert "approved for this unit: overtime" in res["reason"]
    assert res["nearest"], "the nearest clause must be shown"
    for n in res["nearest"]:
        assert n["doc_id"] == "firefighters_local3535_mou"
        assert n["page"] and n["text"]
    assert res["nearest_label"] == "closest text — not a computed answer"
    assert [a["page"] for a in res["approved_clauses"]] == [8]
    assert res["approved_clauses"][0]["topic"] == "overtime"
    types = _types(res["query_id"])
    assert "costing.refused" in types
    assert "answer.snapshot" not in types
    assert not os.path.exists(case_dir / "snapshots" / f"{res['query_id']}.json")
    assert "640.8" not in json.dumps(res)


def test_refusal_names_the_asked_branch(client):
    res = _ask(client, OFF_SCRIPT[0])
    assert res["asked"] == ["regular"]
    assert "No approved regular rule" in res["reason"]
    assert res["interpretation"]["pay_type"] == "regular"


# (c) no pay type named: ask, accept only a lexicon key -------------------------------
def test_unstated_pay_type_asks_and_accepts_only_known_keys(client):
    res = _ask(client, f"Cost an 8-hour shift for a {FF}")
    assert res["mode"] == "clarify", res
    assert res["field"] == "pay_type"
    assert res["options"] == ["overtime"]
    assert "Approved for this unit: overtime" in res["question"]

    again = _ask(client, f"Cost an 8-hour shift for a {FF}", query_id=res["query_id"],
                 clarified={"pay_type": "overtime"})
    assert again["mode"] == "costing", again
    assert again["result"]["total"] == 640.80
    assert again["interpretation"]["pay_type"] == "overtime"

    bogus = _ask(client, f"Cost an 8-hour shift for a {FF}", query_id=res["query_id"],
                 clarified={"pay_type": "bogus"})
    assert bogus["mode"] == "clarify" and bogus["field"] == "pay_type"


# (d) overtime AND holiday: the unapproved branch is named ----------------------------
def test_overtime_on_a_holiday_is_refused_naming_holiday(client):
    res = _ask(client, f"Cost an 8-hour overtime shift on a holiday for a {FF}")
    assert res["mode"] == "refused", res
    assert res["asked"] == ["overtime", "holiday"]
    assert "holiday" in res["reason"]
    assert "result" not in res


# (e) the lexicon reader ----------------------------------------------------------------
@pytest.mark.parametrize("prompt, asked, negated", [
    ("cost an 8-hour shift, not overtime", ["regular"], ["overtime"]),
    ("8 hours of OT for a captain", ["overtime"], []),
    ("time and a half for 4 hours", ["overtime"], []),
    ("an 8-hour day shift", [], []),
    ("an 8-hour holiday shift", ["holiday"], []),
    ("quote the over-time clause", ["overtime"], []),
    ("a regular 1.5x shift", ["regular", "overtime"], []),
    ("without any overtime, a callback", ["callback"], ["overtime"]),
    ("non-overtime hours", ["regular"], ["overtime"]),
    ("the hotel is standing by", [], []),
])
def test_detect_pay_type_table(prompt, asked, negated):
    out = costing.detect_pay_type(prompt)
    assert out["asked"] == asked, out
    assert out["negated"] == negated, out


def test_case_lexicon_is_read_from_extraction_yaml():
    import yaml
    cfg = yaml.safe_load(open(os.path.join(CASE_SRC, "prompt", "extraction.yaml")))
    lex = costing.pay_type_lexicon(cfg)
    assert set(lex) >= {"overtime", "regular", "holiday", "callback"}
    assert cfg["output_shape"]["pay_type"] == "str"
    assert costing.hours_limit(cfg) == 96
    # a case without a lexicon still gets the built-in one
    assert costing.pay_type_lexicon({}) == costing.DEFAULT_PAY_TYPES


# the pay type reaches the engine as a fact, so a future rule can key on it -----------
def test_pay_type_is_an_engine_fact(client, case_dir):
    res = _ask(client, HEADLINE)
    snap = json.load(open(case_dir / "snapshots" / f"{res['query_id']}.json"))
    assert snap["params"]["pay_type"] == "overtime"
    assert snap["params"]["hours"] == 8.0
