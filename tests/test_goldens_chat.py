"""Every known answer must ALSO come out of POST /chat with no key (DEMO_TICKETS J1a, B5).

The golden gate (tests/test_case_santacruz.py) proves the rules reproduce the numbers
through the engine; this file proves the chat path — parser, governance, engine params,
ledger — carries the same facts end to end. Also the J1a longevity mechanics: years of
service as a question fact, the differential's threshold, and the parser stub/echo-back.
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import llm, queryfacts  # noqa: E402
from core.caseio import load_case  # noqa: E402
from core.engine import calculate  # noqa: E402
from core.ruledsl import SHIFT_BASES, load_rules  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE_DIR = os.path.join(ROOT, "cases", "santacruz")

FF_PM = "Firefighter/Paramedic (56 hr, top step)"
Q_640 = "What does an 8-hour overtime shift cost for a Firefighter/Paramedic (56 hr, top step)?"
Q_656 = ("What does an 8-hour overtime shift cost for a Firefighter/Paramedic (56 hr, top step) "
         "with 12 years of service?")
Q_716_DATED = "Cost an 8-hour overtime shift for an Administrative Analyst (top step) on October 7"


@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE_DIR, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def _rules_for(case, doc_id):
    rules = [r for r in case.rules()
             if r.citation.doc_id == doc_id and r.result_type == "currency"]
    assert rules
    return rules


def _ff_pm(case):
    return [s for s in case.subjects() if s["name"] == FF_PM]


# --------------------------------------------------------------------------- #
# chat-level goldens
# --------------------------------------------------------------------------- #
def test_chat_640_80_unchanged_when_no_years_given(client):
    res = client.post("/chat", json={"prompt": Q_640}).json()
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 640.80
    li = res["result"]["line_items"][0]
    assert li["rule_id"] == "firefighters_local3535_mou:overtime_premium_rate"
    assert [c["page"] for c in li["citations"]] == [8]
    assert "question_facts" not in res["params"]


def test_chat_656_82_for_12_years_of_service(client):
    res = client.post("/chat", json={"prompt": Q_656}).json()
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 656.82
    li = res["result"]["line_items"][0]
    assert li["rule_id"] == "firefighters_local3535_mou:overtime_premium_rate"
    # p.12 longevity first (differential), then p.8 overtime (base) — in that order.
    assert [(c["doc_id"], c["page"]) for c in li["citations"]] == [
        ("firefighters_local3535_mou", 12), ("firefighters_local3535_mou", 8)]
    # The answer labels where the years came from.
    qf = res["params"]["question_facts"]["years_of_service"]
    assert qf["value"] == 12.0
    assert "from the question, unverified" in qf["source"]
    # ...and the audit trail carries the rate derivation.
    audit = client.get(f"/chat/audit/{res['query_id']}").json()
    events = audit.get("events") or audit.get("trail") or audit
    mods = [e for e in events if e.get("type") == "rule.modifier"]
    assert mods, audit
    assert mods[0]["payload"]["detail"].startswith(
        "effective_base = round(effective_base * 1.025, 6) -> 54.735")


def test_chat_dated_known_answer_still_resolves(client):
    """B5: 'on October 7' must resolve under the Term-clause windows (the old
    title-derived windows ended 2026-06-30 for the Admin Group)."""
    res = client.post("/chat", json={"prompt": Q_716_DATED}).json()
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 716.16
    assert res["shift_date"] == "2026-10-07"
    assert res["routing_path"] == "governance"


def test_chat_dated_firefighter_question_resolves_too(client):
    res = client.post("/chat", json={"prompt": Q_640 + " on October 7"}).json()
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 640.80
    assert res["shift_date"] == "2026-10-07"


def test_chat_battalion_chief_overtime_is_priced_under_local_3535(client):
    """B5: the Battalion Chief rows now sit in the Local 3535 unit, so a costing
    question resolves to that MOU (75.34 x 1.5 x 8 = 904.08)."""
    res = client.post("/chat", json={
        "prompt": "Cost an 8-hour overtime shift for a Battalion Chief (56 hr top step)"}).json()
    assert res["mode"] == "costing", res
    assert res["chosen_doc"] == "firefighters_local3535_mou"
    assert res["result"]["total"] == 904.08


# --------------------------------------------------------------------------- #
# engine: the differential's threshold and the seeded default
# --------------------------------------------------------------------------- #
def test_longevity_differential_applies_only_from_ten_years():
    case = load_case(CASE_DIR)
    rules = _rules_for(case, "firefighters_local3535_mou")
    subs = _ff_pm(case)
    base = dict(queryfacts.query_defaults(case), hours=8)
    assert calculate(dict(base, years_of_service=9), subs, rules, basis_scope=SHIFT_BASES).total == 640.80
    assert calculate(dict(base, years_of_service=10), subs, rules, basis_scope=SHIFT_BASES).total == 656.82
    assert calculate(dict(base, years_of_service=12), subs, rules, basis_scope=SHIFT_BASES).total == 656.82
    # Param absent from the question but seeded by query_defaults -> no RuleError, 640.80.
    assert calculate(base, subs, rules, basis_scope=SHIFT_BASES).total == 640.80


def test_longevity_rule_is_ratified_and_cites_page_12():
    rules = load_rules(os.path.join(CASE_DIR, "rules", "rules_ratified.json"))
    r = next(r for r in rules if r.id == "firefighters_local3535_mou:longevity_10yr")
    assert r.role == "differential" and r.when == "years_of_service >= 10"
    assert r.citation.page == 12 and r.citation.doc_id == "firefighters_local3535_mou"
    assert "not yet human-ratified" in r.approver


def test_query_defaults_cover_every_declared_numeric_fact():
    case = load_case(CASE_DIR)
    d = queryfacts.query_defaults(case)
    assert d["years_of_service"] == 0.0 and d["hours"] == 0.0
    assert queryfacts.engine_extras(case, {"years_of_service": "12"}) == {"years_of_service": 12.0}
    assert queryfacts.engine_extras(case, {"years_of_service": "twelve"}) == {"years_of_service": 0.0}
    assert queryfacts.engine_extras(case, {}) == {"years_of_service": 0.0}


# --------------------------------------------------------------------------- #
# parser: stub regex and model echo-back
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prompt", [
    "What does an 8-hour overtime shift cost for a firefighter with 12 years of service?",
    "cost an 8-hour overtime shift for a 12-year Firefighter/Paramedic",
    "8 hour OT, 12 yrs service",
])
def test_stub_parses_years_of_service(prompt):
    out = llm._parse_intent_stub(prompt, [])
    assert out["years_of_service"] == 12.0
    assert out["hours"] == 8.0


def test_stub_years_default_to_zero_and_do_not_steal_hours():
    out = llm._parse_intent_stub("What does an 8-hour overtime shift cost?", [])
    assert out["years_of_service"] == 0.0 and out["hours"] == 8.0
    out = llm._parse_intent_stub("cost an 8-hour shift on October 7, 2026", [])
    assert out["years_of_service"] == 0.0


def test_model_years_absent_from_prompt_are_stripped():
    out = llm._normalize_intent({"source": "claude", "hours": 8.0, "years_of_service": 15.0,
                                 "subjects": []}, [], prompt="cost an 8-hour shift")
    assert out["years_of_service"] == 0.0
    assert out["unverified_numbers"] == {"years_of_service": 15.0}
    assert "question_facts" not in out


def test_model_years_present_in_prompt_pass_and_are_labelled():
    out = llm._normalize_intent({"source": "claude", "hours": 8.0, "years_of_service": 12.0,
                                 "subjects": []}, [], prompt="8-hour shift, 12 years")
    assert out["years_of_service"] == 12.0
    assert "unverified_numbers" not in out
    assert out["question_facts"]["years_of_service"]["value"] == 12.0
