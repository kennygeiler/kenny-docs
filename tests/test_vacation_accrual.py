"""Vacation accrual as five tiered rules over the OCR-damaged p.22 table, with a
ratify gate that blocks on an unconfirmed cell (DEMO_TICKETS.md J1b; the "confirm
value" slice of D2).

The stored text layer prints '0) £5' where the page prints 10.15. The rules carry the
page-image values and cite ONE cell each; the gate refuses a rule whose cited cell is
disputed until a person confirms it (POST /admin/cell_confirm -> ledger cell.confirm),
and chat answers the 7-year question from the engine with the p.22 citation. Every test
runs on a private copy of cases/santacruz with no API key.
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import cellcheck  # noqa: E402
from core.app import _check_golden  # noqa: E402
from core.caseio import load_case  # noqa: E402
from core.catalog import Catalog  # noqa: E402
from core.engine import NoRuleApplies, calculate  # noqa: E402
from core.ruledsl import Rule  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "cases", "santacruz")
FIRE = "firefighters_local3535_mou"
TIERS = {"1_5": 7.38, "6_10": 10.15, "11_13": 12.0, "14_16": 13.85, "17_plus": 14.78}
SIX_TEN = f"{FIRE}:vacation_accrual_6_10"
Q7 = "How many vacation hours does a firefighter with 7 years accrue per pay period?"


def _shipped_rules():
    with open(os.path.join(SRC, "rules", "rules_ratified.json")) as f:
        return json.load(f)["rules"]


def _vacation_rules():
    return [r for r in _shipped_rules() if ":vacation_accrual_" in r["id"]]


@pytest.fixture
def env(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(SRC, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app), str(case)


def _propose(case_dir, rules):
    with open(os.path.join(case_dir, "rules", "rules_proposed.json"), "w") as f:
        json.dump({"rules": rules, "needs_data": []}, f)


def _as_proposed(rule):
    return {k: v for k, v in rule.items()
            if k not in ("status", "approver", "approved_at", "approval")}


def _ledger(case_dir):
    path = os.path.join(case_dir, "ledger.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _confirm(c, value="10.15", by="kenny", row=2, col=1, **extra):
    return c.post("/admin/cell_confirm", json={"doc_id": FIRE, "page": 22, "row": row,
                                               "col": col, "value": value, "by": by, **extra})


# ---------------- the rules and the engine ----------------
def test_five_tier_rules_ship_with_page_image_values_and_one_cell_each():
    rules = {r["id"].rsplit("_accrual_", 1)[1]: r for r in _vacation_rules()}
    assert set(rules) == set(TIERS)
    for tier, hours in TIERS.items():
        r = rules[tier]
        assert r["kind"] == "selector" and r["role"] == "base"
        assert r["result_type"] == "hours" and r["pay_basis"] == "per_pay_period"
        assert r["topic"] == "vacation" and float(r["compute"]) == hours
        cell = r["citation"]["cell"]
        assert r["citation"]["doc_id"] == FIRE and r["citation"]["page"] == 22
        assert cell["col"] == 1 and float(cell["quoted_text"]) == hours
        # the rule says what the page shows AND what the text layer printed
        assert cell["stored_text"] != "" and "human-ratified" in r["approver"]


def test_rule_values_are_the_reread_not_the_stored_text():
    """The value a tier computes is the second engine's page-image reading; the stored
    OCR text for three tiers is garbage and must never be the number."""
    for r in _vacation_rules():
        cell = cellcheck.find_cell(SRC, FIRE, 22, r["citation"]["cell"]["row"], 1)
        assert cell is not None
        assert cellcheck.parse_cell(cell["reread"])[0] == pytest.approx(float(r["compute"]))
        assert cell["stored"] == r["citation"]["cell"]["stored_text"]
    stored = {r["id"].rsplit("_", 1)[-1]: r["citation"]["cell"]["stored_text"]
              for r in _vacation_rules()}
    assert stored["10"] == "0) £5"          # the showcase misread


def test_vacation_tiers_cover_every_boundary():
    rules = [Rule.from_dict(r) for r in _vacation_rules()]
    subj = [{"name": "Firefighter/Paramedic (56 hr, top step)", "base_hourly": 53.40}]
    expect = {1: 7.38, 5: 7.38, 6: 10.15, 10: 10.15, 11: 12, 13: 12, 14: 13.85,
              16: 13.85, 17: 14.78, 30: 14.78}
    for years, hours in expect.items():
        res = calculate({"hours": 0.0, "years_of_service": float(years)}, subj, rules)
        assert res.total == hours, (years, res.total)
        assert res.line_items[0].result_type == "hours"
        assert res.line_items[0].citations[0]["page"] == 22
    with pytest.raises(NoRuleApplies):      # the table starts at one completed year
        calculate({"hours": 0.0, "years_of_service": 0.0}, subj, rules)


def test_tiers_are_mutually_exclusive():
    rules = [Rule.from_dict(r) for r in _vacation_rules()]
    subj = [{"name": "x", "base_hourly": 1.0}]
    for years in range(1, 40):
        res = calculate({"hours": 0.0, "years_of_service": float(years)}, subj, rules)
        considered = [t for t in res.line_items[0].trace if t.kind == "selector-considered"]
        assert sum(1 for t in considered if t.value) == 1, years


# ---------------- known answers ----------------
def test_vacation_accrual_7yr_is_10_15():
    case = load_case(SRC)
    g = next(g for g in case.golden_cases() if "7-year member" in g["name"])
    ok, detail = _check_golden(case, _shipped_rules(), g)
    assert ok and detail["status"] == "pass", detail
    assert detail["actual"] == 10.15 and detail["chosen"] == [SIX_TEN]


def test_every_tier_has_a_passing_known_answer_and_the_money_goldens_are_untouched():
    case = load_case(SRC)
    rules = _shipped_rules()
    by_name = {g["name"]: _check_golden(case, rules, g)[1] for g in case.golden_cases()}
    fired = {d["chosen"][0]: d["actual"] for n, d in by_name.items() if "vacation accrual" in n}
    assert fired == {f"{FIRE}:vacation_accrual_{t}": v for t, v in TIERS.items()}
    assert by_name["8-hour overtime shift at base rate, hours beyond the 182-hour threshold, "
                   "Firefighter/Paramedic top step (1.5x per Local 3535 MOU)"]["actual"] == 640.80
    assert by_name["8-hour overtime shift, 12-year Firefighter/Paramedic top step "
                   "(longevity 2.5% into the 1.5x rate)"]["actual"] == 656.82
    assert all(d["status"] == "pass" for d in by_name.values()), by_name


# ---------------- the cell-evidence gate ----------------
def test_gate_function_judges_the_cited_cell_not_the_whole_table():
    cat = Catalog(os.path.join(SRC, "catalog.json"))
    by_tier = {r["id"].rsplit("_accrual_", 1)[1]: r for r in _vacation_rules()}
    assert cellcheck.gate_rule(SRC, cat, by_tier["1_5"]) == []        # '7,38' verified
    assert cellcheck.gate_rule(SRC, cat, by_tier["11_13"]) == []      # '12' verified
    for tier in ("6_10", "14_16", "17_plus"):
        errs = cellcheck.gate_rule(SRC, cat, by_tier[tier])
        assert len(errs) == 1 and "cell_confirm" in errs[0], errs
    e = cellcheck.gate_rule(SRC, cat, by_tier["6_10"])[0]
    assert "'0) £5'" in e and "'10.15'" in e and "disputed" in e
    # a rule citing the table box with no cell is judged on every cell of the table
    whole = {**by_tier["1_5"], "citation": {k: v for k, v in by_tier["1_5"]["citation"].items()
                                            if k != "cell"}}
    assert any("not all verified" in x for x in cellcheck.gate_rule(SRC, cat, whole))
    # a prose citation is untouched by this gate
    prose = next(r for r in _shipped_rules() if r["id"].endswith(":overtime_premium_rate"))
    assert cellcheck.gate_rule(SRC, cat, prose) == []


def test_rule_citing_unverified_ocr_cell_is_rejected(env):
    c, case_dir = env
    six = next(r for r in _vacation_rules() if r["id"] == SIX_TEN)
    _propose(case_dir, [_as_proposed(six)])
    res = c.post("/admin/ratify", json={"approver": "kenny", "rule_ids": [SIX_TEN]}).json()
    assert res["ratified"] == []
    assert "0) £5" in res["rejected"][SIX_TEN][0] and "10.15" in res["rejected"][SIX_TEN][0]
    assert "OCR cell nobody has confirmed" in res["warning"]
    blocked = [e for e in _ledger(case_dir) if e["type"] == "authoring.blocked"]
    assert blocked and blocked[-1]["payload"]["reason"] == "unverified OCR cell"
    assert SIX_TEN in blocked[-1]["payload"]["rejected"]
    # the known answers all still passed: this refusal is about evidence, not the number
    checks = [e for e in _ledger(case_dir) if e["type"] == "authoring.golden_check"]
    assert checks and all(e["payload"]["passed"] for e in checks)


def test_confirmed_cell_ratifies_and_is_ledgered(env):
    c, case_dir = env
    six = next(r for r in _vacation_rules() if r["id"] == SIX_TEN)
    _propose(case_dir, [_as_proposed(six)])
    before = c.get("/admin/cell_gate").json()
    assert SIX_TEN in before["blocked"]
    res = _confirm(c, note="read on the p.22 page image").json()
    assert res["ok"] and res["cell"]["confirmed"]["by"] == "kenny"
    assert res["cell"]["confirmed"]["agrees_with_reread"] is True
    assert res["cell"]["confirmed"]["agrees_with_stored"] is False
    assert res["cell"]["stored"] == "0) £5" and res["cell"]["reread"] == "10.15"   # kept beside
    assert SIX_TEN in res["unblocked"]
    ev = [e for e in _ledger(case_dir) if e["type"] == "cell.confirm"]
    assert len(ev) == 1 and ev[0]["seq"] == res["ledger_seq"] and ev[0]["actor"] == "admin"
    assert ev[0]["payload"] == {**ev[0]["payload"], "doc_id": FIRE, "page": 22, "row": 2,
                                "col": 1, "value": "10.15", "by": "kenny",
                                "stored": "0) £5", "reread": "10.15",
                                "status_before": "disputed"}
    after = c.get("/admin/cell_gate").json()
    assert SIX_TEN not in after["blocked"]
    ok = c.post("/admin/ratify", json={"approver": "kenny", "rule_ids": [SIX_TEN]}).json()
    assert ok["ratified"] == [SIX_TEN], ok
    with open(os.path.join(case_dir, "rules", "rules_ratified.json")) as f:
        live = {r["id"]: r for r in json.load(f)["rules"]}
    assert live[SIX_TEN]["approver"] == "kenny"
    assert live[SIX_TEN]["citation"]["cell"]["quoted_text"] == "10.15"
    # the confirmation persisted and the clauses endpoint shows it on the cell
    stored = cellcheck.find_cell(case_dir, FIRE, 22, 2, 1)
    assert stored["confirmed"]["value"] == "10.15" and stored["status"] == "disputed"
    body = c.get(f"/doc/{FIRE}/clauses", params={"page": 22}).json()
    rows = [cl for cl in body["clauses"] if cl["kind"] == "table-row" and cl["cells"]]
    assert rows[1]["cells"][1]["confirmed"]["by"] == "kenny"


def test_quoted_text_matching_stored_text_needs_no_verification(env):
    c, case_dir = env
    tier = next(r for r in _vacation_rules() if r["id"].endswith("_11_13"))   # '12' vs '12'
    _propose(case_dir, [_as_proposed(tier)])
    res = c.post("/admin/ratify", json={"approver": "kenny", "rule_ids": [tier["id"]]}).json()
    assert res["ratified"] == [tier["id"]], res


def test_confirmation_that_disagrees_with_the_rule_still_blocks(env):
    c, case_dir = env
    six = next(r for r in _vacation_rules() if r["id"] == SIX_TEN)
    _propose(case_dir, [_as_proposed(six)])
    assert _confirm(c, value="10.75").status_code == 200    # the reader saw something else
    res = c.post("/admin/ratify", json={"approver": "kenny", "rule_ids": [SIX_TEN]}).json()
    assert res["ratified"] == []
    msg = res["rejected"][SIX_TEN]
    assert any("quotes '10.15'" in m and "'10.75'" in m for m in msg), msg


def test_wrong_number_is_still_caught_as_a_wrong_number(env):
    """The cell gate runs AFTER the known answers: a mutated value on an unconfirmed cell
    is refused for being wrong, not for being unread."""
    c, case_dir = env
    six = next(r for r in _vacation_rules() if r["id"] == SIX_TEN)
    _propose(case_dir, [{**_as_proposed(six), "compute": "1015"}])   # 'fs 13,85' -> 1385 style
    res = c.post("/admin/ratify", json={"approver": "kenny", "rule_ids": [SIX_TEN]}).json()
    assert res["ratified"] == [] and res["golden_failed"]["actual"] == 1015.0
    reason = [e for e in _ledger(case_dir) if e["type"] == "authoring.blocked"][-1]["payload"]
    assert reason["reason"] != "unverified OCR cell"


def test_cell_confirm_validation(env):
    c, _ = env
    assert _confirm(c, by="").status_code == 400
    assert _confirm(c, value="").status_code == 400
    assert _confirm(c, row=99).status_code == 404
    assert c.post("/admin/cell_confirm", json={"doc_id": FIRE}).status_code == 400
    assert c.get("/admin/cell_gate", params={"rule_id": "nope:x"}).status_code == 404


def test_confirmed_cell_is_no_longer_refused_as_evidence(env):
    """D2's /admin/clause 409 and the ratify gate read the same record: after the one
    disputed hours cell is confirmed, only the OTHER disputed cells remain."""
    c, case_dir = env
    bbox = "109.832,589.507,534.307,449.771"
    before = c.get("/admin/clause", params={"doc_id": FIRE, "page": 22, "bbox": bbox}).json()
    assert {x["stored"] for x in before["cells"]} >= {"0) £5", "A468", "fs 13,85"}
    _confirm(c)
    after = c.get("/admin/clause", params={"doc_id": FIRE, "page": 22, "bbox": bbox})
    assert after.status_code == 409
    assert "0) £5" not in {x["stored"] for x in after.json()["cells"]}


def test_shipped_state_blocks_exactly_the_three_misread_tiers(env):
    c, _ = env
    body = c.get("/admin/cell_gate").json()
    assert set(body["blocked"]) == {f"{FIRE}:vacation_accrual_{t}" for t in ("6_10", "14_16", "17_plus")}
    rows = {r["rule_id"]: r for r in body["rules"]}
    assert rows[SIX_TEN]["cell"]["stored"] == "0) £5" and rows[SIX_TEN]["cell"]["reread"] == "10.15"
    assert rows[SIX_TEN]["cell"]["confirmed"] is None
    assert all(r["cell"] is None and r["approvable"] for r in rows.values() if not r["cell_ref"])


# ---------------- chat ----------------
def test_chat_answers_the_7_year_question_from_the_engine(env):
    c, case_dir = env
    res = c.post("/chat", json={"prompt": Q7}).json()
    assert res["mode"] == "entitlement", res
    assert res["result"]["total"] == 10.15
    li = res["result"]["line_items"]
    assert len(li) == 1 and li[0]["rule_id"] == SIX_TEN and li[0]["result_type"] == "hours"
    assert li[0]["citations"][0]["doc_id"] == FIRE and li[0]["citations"][0]["page"] == 22
    assert "row 6-10" in li[0]["citations"][0]["clause"]
    assert res["params"]["years_of_service"] == 7.0
    assert any(m["rule_id"] == SIX_TEN for m in res["match"])
    assert any(e["type"] == "answer.snapshot" for e in _ledger(case_dir))


def test_chat_without_years_is_not_answered_by_a_tier(env):
    """No years stated -> years_of_service 0 -> no tier applies -> the engine refuses
    and chat falls back to quoting the clause, never guessing a tier."""
    c, _ = env
    res = c.post("/chat", json={"prompt": "How many vacation hours does a firefighter accrue per pay period?"}).json()
    assert res["mode"] != "entitlement" or not any(
        li["rule_id"].startswith(f"{FIRE}:vacation_accrual") for li in res["result"]["line_items"])


def test_chat_twenty_years_hits_the_open_tier(env):
    """'Fire Captain' names two roster rows (56 hr and 40 hr); each row gets the 17+
    tier. (The entitlement path sums rows into `total` — 29.56 for two rows — which
    is that path's own convention, not this rule's; each line is 14.78.)"""
    c, _ = env
    res = c.post("/chat", json={"prompt": "How many vacation hours does a Fire Captain with 20 years of service accrue per pay period?"}).json()
    assert res["mode"] == "entitlement", res
    items = res["result"]["line_items"]
    assert items and all(li["total"] == 14.78 and li["rule_id"].endswith("_17_plus")
                         for li in items), items
