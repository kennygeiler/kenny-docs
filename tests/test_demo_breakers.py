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


# ---- F5: a bare heading is never the whole quote ----
def _hit(text, page=1, doc="mou", clause=""):
    return {"doc_id": doc, "page": page, "clause": clause, "text": text, "score": 1.0}


def test_compose_quote_skips_heading_only_hits():
    body = "Employees with ten or more years of service receive a 2.5% longevity differential."
    hits = [_hit("D. Longevity", page=12), _hit(body, page=13)]
    out = rulematch.compose_quote(hits, lambda d: "The MOU")
    assert out == f"From The MOU, p.13: {body}"
    hits = [_hit("Designated Holidays are as follows: -", page=20),
            _hit("XIIL. Sick Leave", page=22), _hit(body, page=13)]
    assert rulematch.compose_quote(hits, lambda d: "The MOU").endswith(body)


def test_compose_quote_keeps_heading_as_lead_in_to_same_page_chunk():
    hits = [_hit("Designated Holidays are as follows: -", page=20),
            _hit("There are 13 designated holidays (312 hours), paid in the pay period "
                 "the holiday occurs.", page=20)]
    out = rulematch.compose_quote(hits, lambda d: "The MOU")
    assert out.startswith("From The MOU, p.20: Designated Holidays are as follows: - There are 13")


def test_compose_quote_falls_back_when_only_headings_exist():
    hits = [_hit("D. Longevity", page=12)]
    assert rulematch.compose_quote(hits, lambda d: "The MOU") == "From The MOU, p.12: D. Longevity"


def test_compose_quote_lookup_rows_are_not_filtered():
    hits = [_hit("Step C $52.10", page=3, clause="12.1"), _hit("Step D $54.70", page=3)]
    out = rulematch.compose_quote(hits, lambda d: "Schedule", lookup=True)
    assert out.splitlines() == ["Per §12.1, p.3: Step C $52.10", "From Schedule, p.3: Step D $54.70"]


# ---- F6: entitlement snapshot carries its unit ----
def test_history_shows_bereavement_as_shifts_not_dollars(client):
    res = _ask(client, "How much bereavement leave does a firefighter get?")
    assert res["mode"] == "entitlement", res
    rows = audit.history(load_case(client.case_dir).ledger())
    row = next(r for r in rows if r["query_id"] == res["query_id"])
    assert row["total"] == 3 and row["result_type"] == "shifts"
    snap = [e for e in load_case(client.case_dir).ledger().read()
            if e.get("query_id") == res["query_id"] and e["type"] == "answer.snapshot"]
    assert snap[0]["payload"]["result_type"] == "shifts"
    assert snap[0]["payload"]["unit_label"] == "shifts"


def test_history_never_defaults_an_entitlement_to_currency(tmp_path):
    from core.ledger import Ledger
    led = Ledger(str(tmp_path / "ledger.jsonl"))
    led.append("chat.prompt", {"text": "bereavement?"}, actor="user", query_id="q1")
    led.append("answer.snapshot", {"total": 3, "intent": "entitlement"},
               actor="engine", query_id="q1")
    led.append("chat.prompt", {"text": "cost?"}, actor="user", query_id="q2")
    led.append("answer.snapshot", {"total": 640.8}, actor="engine", query_id="q2")
    rows = {r["query_id"]: r for r in audit.history(led)}
    assert rows["q1"]["result_type"] != "currency"
    assert rows["q2"]["result_type"] == "currency"


# ---- F9: a captured figure in the clause slot is not a section label ----
def test_figure_in_clause_slot_cites_document_and_page():
    assert not rulematch.clause_label_ok("300.00")
    assert not rulematch.clause_label_ok("12")
    assert rulematch.clause_label_ok("12.1") and rulematch.clause_label_ok("XIII") \
        and rulematch.clause_label_ok("D")
    hits = [_hit("Uniform allowance of $300.00 per year for all sworn members.",
                 page=40, clause="300.00")]
    out = rulematch.compose_quote(hits, lambda d: "Firefighters MOU")
    assert out.startswith("From Firefighters MOU, p.40:")
    assert "§300.00" not in out


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
