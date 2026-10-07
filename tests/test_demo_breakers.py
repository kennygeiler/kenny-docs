"""Demo-breaking findings from the 2026-10-07 review, one test per fix (no API key).

F2  "How much would it COST ... to work 8 HOURS" was refused as entitlement.
F3  "ten years of service" read as no years and silently costed $640.80.
F4  "pay for" in the cost cues sent "What is the callback pay for ...?" to costing.
F5  A bare heading ("D. Longevity", "Sick Leave:") was quoted as the whole answer.
F6  The Audit tab showed "$3.00" for 3 bereavement shifts.
F9  A captured figure ("300.00") in the clause slot was cited as "Per §300.00".
F10 "Fire Inspector" derived department=fire from the rank and matched nobody.
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import audit, costing, llm, rulematch  # noqa: E402
from core.caseio import load_case  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE_DIR = os.path.join(ROOT, "cases", "santacruz")
FF = "Firefighter/Paramedic (56 hr, top step)"


@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE_DIR, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    c = TestClient(core_app.app)
    c.case_dir = str(case)
    return c


def _ask(client, prompt):
    return client.post("/chat", json={"prompt": prompt}).json()


# ---- F2: an ask to compute outranks the "how much … hours" entitlement pattern ----
def test_how_much_would_it_cost_to_work_hours_is_costing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    q = f"How much would it cost for a {FF} to work 8 hours of overtime?"
    assert llm.classify_intent(q) == "costing"
    assert llm.classify_intent(
        "How much vacation does a Firefighter (56 hr, top step) accrue?") == "entitlement"
    assert llm.classify_intent("How many bereavement days does a sergeant get?") == "entitlement"


def test_how_much_would_it_cost_reaches_the_engine(client):
    res = _ask(client, f"How much would it cost for a {FF} to work 8 hours of overtime?")
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 640.80


# ---- F3: years of service as words; "years" with no number is surfaced ----
@pytest.mark.parametrize("phrase, years", [
    ("ten years of service", 10.0), ("12 years of service", 12.0),
    ("twenty-five years", 25.0), ("a twelve-year firefighter", 12.0),
])
def test_stub_reads_number_words_for_years(phrase, years):
    out = llm._parse_intent_stub(f"cost an 8-hour overtime shift for a firefighter with {phrase}", [])
    assert out["years_of_service"] == years and out["hours"] == 8.0
    assert "years_note" not in out


def test_stub_years_without_a_number_are_left_unset_and_noted():
    out = llm._parse_intent_stub(
        "cost an 8-hour overtime shift for a firefighter with several years of service", [])
    assert out["years_of_service"] == 0.0
    assert "no number read" in out["years_note"]
    none = llm._parse_intent_stub("cost an 8-hour overtime shift", [])
    assert none["years_of_service"] == 0.0 and "years_note" not in none


def test_echo_back_accepts_model_years_spelled_in_words():
    out = llm._normalize_intent({"source": "claude", "hours": 8.0, "years_of_service": 10.0,
                                 "subjects": []}, [],
                                prompt="cost an 8-hour shift with ten years of service")
    assert out["years_of_service"] == 10.0


def test_interpretation_line_carries_years_of_service():
    i = costing.interpretation(8.0, "overtime", [FF], None, False, "stub",
                               years_of_service=12)
    assert i["read_as"].endswith("· 12 years of service (from the question)")
    i = costing.interpretation(8.0, "overtime", [FF], None, False, "stub",
                               years_note="years of service mentioned, no number read")
    assert i["read_as"].endswith("· years of service mentioned, no number read")
    i = costing.interpretation(8.0, "overtime", [FF], None, False, "stub")
    assert "years" not in i["read_as"] and i["years_of_service"] is None


def test_chat_ten_years_in_words_is_656_82_and_read_back(client):
    res = _ask(client, f"What does an 8-hour overtime shift cost for a {FF} "
                       "with ten years of service?")
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 656.82
    assert "10 years of service (from the question)" in res["interpretation"]["read_as"]
    res = _ask(client, f"What does an 8-hour overtime shift cost for a {FF} "
                       "with several years of service?")
    assert res["result"]["total"] == 640.80
    assert "no number read" in res["interpretation"]["read_as"]


# ---- F4: "pay for" is not a cost cue ----
def test_callback_pay_question_is_policy_not_costing(monkeypatch, client):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "pay for" not in llm._COST_CUES
    assert llm.classify_intent("What is the callback pay for firefighters?") == "policy"
    assert llm.classify_intent(f"Cost an 8-hour overtime shift for a {FF}") == "costing"
    assert llm.classify_intent("overtime pay for 8 hours for a firefighter") == "costing"
    res = _ask(client, "What is the callback pay for firefighters?")
    assert res["mode"] == "policy", res
    assert res.get("question") is None


# ---- F10: a matched rank never derives the department from its own words ----
def test_rank_match_does_not_derive_department_from_rank_words():
    subs = load_case(CASE_DIR).subjects()
    got = llm._resolve_classifications("cost an 8-hour overtime shift for a fire inspector", subs)
    assert got == ["Fire Inspector (top step)"]
    # an explicit department outside the rank still constrains
    caps = llm._resolve_classifications("fire captain", subs)
    assert caps and all("Fire Captain" in c for c in caps)


def test_fire_inspector_without_top_step_is_costed(client):
    res = _ask(client, "Cost an 8-hour overtime shift for a Fire Inspector")
    assert res["mode"] == "costing", res
    assert res["interpretation"]["subjects"] == ["Fire Inspector (top step)"]
    assert res["result"]["total"] > 0
