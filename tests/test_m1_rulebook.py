"""M1 full rulebook for the Local 3535 MOU (ROADMAP M1, 2026-10-28).

The agent-drafted Local 3535 rules ship in the Review queue (rules_proposed.json), each
with a known answer named by its `_scenario`, a `topic`, and a citation bound to the
catalog text. These tests prove, without an API key:

  * every rule's value is the one read off the page image (pinned here by hand),
  * its citation is the catalog chunk it names (quote hash, document hash),
  * its known answer reads PENDING on the shipped library and PASS once it is approved,
  * each rule can be approved on its own (approval by contract section), and
  * a natural question reaches it through POST /chat once it is ratified.

provisions.yaml (the inventory the coverage meter reads) is checked for shape and for
rule ids that exist.
"""
import json
import os
import re
import shutil
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app, evidence  # noqa: E402
from core.caseio import load_case  # noqa: E402
from core.services import state as core_state  # noqa: E402
from core.services.gate import _check_golden, _gate_verdict  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "cases", "santacruz")
FIRE = "firefighters_local3535_mou"
P = f"{FIRE}:"
FFPM = "Firefighter/Paramedic (56 hr, top step)"

# rule -> (result_type, topic, value as a number). Values were read off the page images
# (pages 7, 8, 14, 16, 20-24, 28-32, 38), not only the OCR text.
VALUES = {
    "regular_straight_time": ("currency", "regular", None),
    "holiday_pay_base_rate": ("currency", "holiday", None),
    "flsa_scheduled_overtime_half_time": ("currency", "flsa", None),
    "acting_captain_ff_top_step": ("currency", "out_of_class", None),
    "acting_captain_ffpm_top_step": ("currency", "out_of_class", None),
    "sick_leave_accrual": ("hours", "sick", 11.08),
    "sick_leave_annual_cash_out_max": ("hours", "sick cash", 144),
    "sick_leave_separation_payout_max": ("hours", "sick separation", 894),
    "holiday_hours_per_year": ("hours", "holiday", 312),
    "administrative_holiday_40hr_safety": ("hours", "administrative holiday", 40),
    "administrative_holiday_40hr_battalion_chief": ("hours", "administrative holiday", 40),
    "education_time_off": ("hours", "education", 36),
    "compensatory_time_max": ("hours", "compensatory", 120),
    "union_leave_bank_contribution": ("hours", "union leave", 4),
    "grievance_filing_deadline": ("days", "grievance", 10),
    "arbitration_request_deadline": ("days", "arbitration", 14),
    "arbitration_compliance_deadline": ("days", "arbitrator compliance", 30),
    "disability_waiting_period": ("days", "disability", 60),
    "flsa_overtime_threshold": ("hours", "flsa", 182),
    "paramedic_training_travel_time": ("hours", "travel", 1),
}
# The currency formulas, as read: multiplier / add-on per the clause.
COMPUTE = {
    "regular_straight_time": "effective_base * hours",
    "holiday_pay_base_rate": "effective_base * hours",
    "flsa_scheduled_overtime_half_time": "effective_base * 0.5 * hours",
    "acting_captain_ff_top_step": "(effective_base + 6.80) * hours",
    "acting_captain_ffpm_top_step": "(effective_base + 1.94) * hours",
}

# question -> (rule that must answer it, total). Asked once every queued rule is live.
CHAT = [
    ("Cost an 8-hour regular shift for a Firefighter/Paramedic (56 hr, top step)",
     "regular_straight_time", 427.20),
    ("Cost 24 hours of holiday pay for a Firefighter/Paramedic (56 hr, top step)",
     "holiday_pay_base_rate", 1281.60),
    ("Cost 24 hours of holiday pay for a Fire Captain (40 hr top step)",
     "holiday_pay_base_rate", 2158.56),
    ("Cost 10 hours of FLSA half-time for a Firefighter/Paramedic (56 hr, top step)",
     "flsa_scheduled_overtime_half_time", 267.00),
    ("Cost an 8-hour acting captain shift for a Firefighter/Paramedic (56 hr, top step)",
     "acting_captain_ffpm_top_step", 442.72),
    ("Cost an 8-hour acting captain shift for a Firefighter (56 hr, top step)",
     "acting_captain_ff_top_step", 442.72),
    ("How many hours of sick leave does a Firefighter/Paramedic (56 hr, top step) accrue per pay period?",
     "sick_leave_accrual", 11.08),
    ("How many sick leave hours does a Fire Captain (40 hr top step) accrue per pay period?",
     "sick_leave_accrual", 11.08),
    ("How many sick leave hours can a firefighter cash out each year?",
     "sick_leave_annual_cash_out_max", 144),
    ("How many hours of unused sick leave are paid when a firefighter separates from the district?",
     "sick_leave_separation_payout_max", 894),
    ("How many holiday hours does a firefighter get per year?", "holiday_hours_per_year", 312),
    ("How many holiday hours does a Fire Captain (56 hr top step) get per year?",
     "holiday_hours_per_year", 312),
    ("How many administrative holiday hours does a Fire Captain (40 hr top step) get?",
     "administrative_holiday_40hr_safety", 40),
    ("How many administrative holiday hours does a Battalion Chief (40 hr top step) get?",
     "administrative_holiday_40hr_battalion_chief", 40),
    ("How many hours of education time off does a firefighter get per year?",
     "education_time_off", 36),
    ("How many hours of compensatory time off can a firefighter accumulate?",
     "compensatory_time_max", 120),
    ("How many vacation hours does each firefighter donate to the Union Leave Bank each year?",
     "union_leave_bank_contribution", 4),
    ("What is the deadline for a firefighter to file a grievance?", "grievance_filing_deadline", 10),
    ("What is the deadline for a firefighter to request arbitration?",
     "arbitration_request_deadline", 14),
    ("How many days does the district have to comply with an arbitrator's decision for a firefighter?",
     "arbitration_compliance_deadline", 30),
    ("How long is the waiting period for long-term disability for a firefighter?",
     "disability_waiting_period", 60),
    ("How many hours before a Firefighter/Paramedic (56 hr, top step) earns FLSA overtime in a "
     "24-day work period?", "flsa_overtime_threshold", 182),
    ("How many hours of travel time are allowed each direction for out-of-area paramedic training "
     "for a Firefighter/Paramedic (56 hr, top step)?", "paramedic_training_travel_time", 1),
    # the live and queued rules the new ones share a document with still answer
    ("How many vacation hours does a firefighter with 7 years accrue per pay period?",
     "vacation_accrual_6_10", 10.15),
    ("What does an 8-hour overtime shift cost for a Firefighter/Paramedic (56 hr, top step)?",
     "overtime_premium_rate", 640.80),
    ("How many shifts of bereavement leave does a Firefighter/Paramedic (56 hr, top step) get?",
     "bereavement_shifts", 3),
]


def _queue():
    with open(os.path.join(SRC, "rules", "rules_proposed.json")) as f:
        return json.load(f)["rules"]


def _m1():
    return {r["id"][len(P):]: r for r in _queue() if r["id"][len(P):] in VALUES}


def _live():
    with open(os.path.join(SRC, "rules", "rules_ratified.json")) as f:
        return json.load(f)["rules"]


def _catalog_doc():
    with open(os.path.join(SRC, "catalog.json")) as f:
        return next(d for d in json.load(f)["documents"] if d["doc_id"] == FIRE)


@pytest.fixture
def live_all(tmp_path, monkeypatch):
    """A private case copy where every queued rule has been moved to the live library,
    as if the owner had approved every section."""
    case = tmp_path / "santacruz"
    shutil.copytree(SRC, case)
    lib = _live() + [dict(r, status="ratified", approver="test",
                          approved_at="2026-10-28T00:00:00Z") for r in _queue()]
    with open(case / "rules" / "rules_ratified.json", "w") as f:
        json.dump({"rules": lib}, f)
    with open(case / "rules" / "rules_proposed.json", "w") as f:
        json.dump({"rules": [], "needs_data": []}, f)
    monkeypatch.setattr(core_state, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


# ---------------- the rules as shipped ----------------
def test_every_m1_rule_ships_queued_with_the_page_image_value():
    rules = _m1()
    assert set(rules) == set(VALUES)
    live_ids = {r["id"] for r in _live()}
    for name, (rtype, topic, value) in VALUES.items():
        r = rules[name]
        assert r["status"] == "proposed" and "approver" not in r, name
        assert r["id"] not in live_ids, name
        assert (r["kind"], r["role"], r["result_type"], r["topic"]) == ("selector", "base", rtype, topic)
        if value is None:
            assert r["compute"] == COMPUTE[name] and r["pay_basis"] == "hourly"
        else:
            assert float(r["compute"]) == value, name
        assert re.search(r"p\.\d+", r["human_readable"]) and "Local 3535 MOU" in r["human_readable"]
        assert r["assumptions"] and r["inputs"] and r["_scenario"]


def test_citations_are_bound_to_the_catalog_text():
    doc = _catalog_doc()
    for name, r in _m1().items():
        cit = r["citation"]
        assert cit["doc_id"] == FIRE and cit["doc_sha256"] == doc["pdf_sha256"]
        found, how = evidence.locate(doc["clauses"], cit)
        assert found is not None and how == "quote", (name, how)
        assert found["page"] == cit["page"] and found["text"].startswith(cit["quote"])
        assert not re.search(r"[\"'<>`]", cit["clause"]), name


def test_existing_rules_are_untouched():
    q = {r["id"] for r in _queue()}
    assert {f"{P}vacation_accrual_{t}" for t in ("1_5", "6_10", "11_13", "14_16", "17_plus")} <= q
    assert len(_live()) == 5


# ---------------- known answers ----------------
def test_every_rule_has_a_known_answer_named_by_its_scenario():
    case = load_case(SRC)
    goldens = {g["name"]: g for g in case.golden_cases()}
    for name, r in _m1().items():
        g = goldens.get(r["_scenario"])
        assert g is not None, name
        assert g["topic"] == r["topic"] and g.get("result_type", "currency") == r["result_type"]
        assert g["source"].startswith("analyst-derived") and "p." in g["source"]


def test_m1_known_answers_are_pending_on_the_shipped_library_and_pass_once_approved():
    case = load_case(SRC)
    m1 = list(_m1().values())
    topics = {r["topic"] for r in m1}
    m1_goldens = [g for g in case.golden_cases() if g.get("topic") in topics
                  and g.get("topic") not in ("overtime", "bereavement", "vacation")]
    assert len(m1_goldens) == 22
    for g in m1_goldens:
        _, before = _check_golden(case, _live(), g)
        assert before["status"] == "pending", g["name"]
        _, after = _check_golden(case, _live() + _queue(), g)
        assert after["status"] == "pass", (g["name"], after)
        assert set(after["fired"]) & {r["id"] for r in m1}, g["name"]


@pytest.mark.parametrize("name", sorted(VALUES))
def test_each_rule_can_be_approved_on_its_own(name):
    """Approval is by contract section: no M1 rule needs another queued rule to pass."""
    case = load_case(SRC)
    v = _gate_verdict(case, [_m1()[name]], "tester")
    assert v["approved"], v["response"]
    assert f"{P}{name}" in v["proved_by"]


# ---------------- reachable through /chat ----------------
@pytest.mark.parametrize("question,rule,total", CHAT, ids=[c[1] for c in CHAT])
def test_a_natural_question_reaches_the_rule_once_ratified(live_all, question, rule, total):
    res = live_all.post("/chat", json={"prompt": question}).json()
    assert res.get("mode") in ("costing", "entitlement"), res
    items = res["result"]["line_items"]
    assert {li["rule_id"] for li in items} == {f"{P}{rule}"}, res
    assert res["result"]["total"] == pytest.approx(total)
    assert any(c.get("doc_id") == FIRE for c in items[0]["citations"])


def test_a_named_topic_that_does_not_fit_is_refused_not_answered_by_another_rule(live_all):
    """Administrative holiday leave is a 40-hour benefit. Asked for a 56-hour member, Kenny
    must not answer with the 312 designated-holiday hours that do fit him."""
    q = "How many administrative holiday hours does a Firefighter/Paramedic (56 hr, top step) get?"
    res = live_all.post("/chat", json={"prompt": q}).json()
    items = (res.get("result") or {}).get("line_items") or []
    assert not items, res


def test_flsa_half_time_refuses_more_than_the_ten_scheduled_hours(live_all):
    q = "Cost 12 hours of FLSA half-time for a Firefighter/Paramedic (56 hr, top step)"
    res = live_all.post("/chat", json={"prompt": q}).json()
    assert not ((res.get("result") or {}).get("line_items")), res


# ---------------- the provision inventory ----------------
def test_provisions_inventory_shape_and_rule_ids():
    with open(os.path.join(SRC, "provisions.yaml")) as f:
        inv = yaml.safe_load(f)
    assert inv["doc_id"] == FIRE and inv["label"] == "Local 3535 MOU"
    known = {r["id"] for r in _live()} | {r["id"] for r in _queue()}
    ids = set()
    doc = _catalog_doc()
    texts = " ".join(re.sub(r"\s+", " ", c["text"]) for c in doc["clauses"])
    for p in inv["provisions"]:
        assert re.fullmatch(r"[a-z0-9_.]+", p["id"]) and p["id"] not in ids, p["id"]
        ids.add(p["id"])
        assert p["category"] in ("pay", "leave", "time", "benefit", "procedure", "narrative")
        assert p["value_kind"] in ("currency", "hours", "shifts", "days", "percent", "boolean", "none")
        assert p["status"] in ("modelled", "not_modelled")
        assert p["title"] and p["article"] and p["pages"] and all(1 <= n <= 40 for n in p["pages"])
        assert p["quote"] and len(p["quote"]) <= 200
        if p["status"] == "modelled":
            assert p["rules"] and set(p["rules"]) <= known, p["id"]
            assert not p.get("reason"), p["id"]
        else:
            assert p["rules"] == [] and p["reason"].strip(), p["id"]
        # the quote is verbatim catalog text, except [bracketed] OCR fixes
        for piece in re.split(r"\[[^\]]*\]", p["quote"].replace("...", "\0")):
            for frag in piece.split("\0"):
                frag = re.sub(r"\s+", " ", frag).strip()
                if len(frag) >= 12:
                    assert frag in texts, (p["id"], frag)
    # every M1 rule and every queued/live Local 3535 rule is accounted for
    listed = {rid for p in inv["provisions"] for rid in p["rules"]}
    assert {r["id"] for r in _live() + _queue() if r["id"].startswith(P)} <= listed


def test_a_bare_rank_is_not_summed_across_its_roster_rows(live_all):
    """'Firefighter/Paramedic' matches the top- and entry-step rows. Sick leave is per
    person: the answer is 11.08 hours once, not 11.08 times the number of rows."""
    q = "How many hours of sick leave does a Firefighter/Paramedic accrue per pay period?"
    res = live_all.post("/chat", json={"prompt": q}).json()
    assert res.get("mode") == "entitlement", res
    items = res["result"]["line_items"]
    assert len(items) == 1, items
    assert items[0]["total"] == pytest.approx(11.08)
    assert res["result"]["total"] == pytest.approx(11.08)


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """A private copy of the case exactly as shipped: the M1 rules are queued, not live."""
    case = tmp_path / "santacruz"
    shutil.copytree(SRC, case)
    monkeypatch.setattr(core_state, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def test_a_refused_holiday_question_names_one_provision_the_holiday_pay_one(shipped):
    """The refusal names the provision whose drafted rule has the asked topic, once — not
    every provision whose title mentions the word (e.g. '... not holiday pay ...')."""
    q = "Cost 24 hours of holiday pay for a Firefighter/Paramedic (56 hr, top step)"
    res = shipped.post("/chat", json={"prompt": q}).json()
    named = res.get("provisions") or []
    assert [p["id"] for p in named] == ["l3535.holiday_pay"], named
    assert named[0]["state"] == "awaiting"
