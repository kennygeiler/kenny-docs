"""The human gate proves what it claims (DEMO_TICKETS.md epic E: E1, E2, E3, E7).

Every test runs against a private copy of cases/santacruz with no API key, through the
real HTTP surface. The gate must judge the library chat runs (E2), refuse any rule no
known answer exercises (E1), let a reviewer try to break a rule without a model (E3),
and say on screen exactly what was computed (E7).
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import llm  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "cases", "santacruz")
FF = "firefighters_local3535_mou"
FF_OT = f"{FF}:overtime_premium_rate"
FF_LONG = f"{FF}:longevity_10yr"
# live rules shipped in the case (data-goldens added longevity_10yr as the fifth)
N_LIVE = len(json.load(open(os.path.join(SRC, "rules", "rules_ratified.json")))["rules"])
GOLDEN_640 = ("8-hour overtime shift at base rate, hours beyond the 182-hour threshold, "
              "Firefighter/Paramedic top step (1.5x per Local 3535 MOU)")
GOLDEN_656 = ("8-hour overtime shift, 12-year Firefighter/Paramedic top step "
              "(longevity 2.5% into the 1.5x rate)")
FF_CIT = {"doc_id": FF, "clause": "Overtime Rate (p.8)", "page": 8,
          "bbox": [141.418, 505.662, 511.182, 441.459]}
MGMT_CIT = {"doc_id": "management_mou", "clause": "Flex time (p.6)", "page": 6,
            "bbox": [82.4, 212.5, 538.9, 162.4]}
Q8 = "Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step)."


@pytest.fixture
def env(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(SRC, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app), str(case)


def _propose(case, rules):
    with open(os.path.join(case, "rules", "rules_proposed.json"), "w") as f:
        json.dump({"rules": rules, "needs_data": []}, f)


def _library(case):
    with open(os.path.join(case, "rules", "rules_ratified.json")) as f:
        return json.load(f)["rules"]


def _set_library(case, rules):
    with open(os.path.join(case, "rules", "rules_ratified.json"), "w") as f:
        json.dump({"rules": rules}, f)


def _rule(rid, cit, **kw):
    r = {"id": rid, "kind": "selector", "role": "base", "result_type": "currency",
         "pay_basis": "hourly", "topic": "overtime", "when": "hours > 0",
         "compute": "effective_base * 1.5 * hours", "citation": dict(cit)}
    r.update(kw)
    return r


def _total(c, prompt):
    r = c.post("/chat", json={"prompt": prompt}).json()
    return r.get("mode"), (r.get("result") or {}).get("total")


def _ledger(case):
    path = os.path.join(case, "ledger.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------- E2: the gate evaluates the stored library ----------------
def test_gate_sees_role_and_pay_basis(env):
    c, case = env
    lib = _library(case)
    # a correct ANNUAL premium: real money, wrong unit for a shift — must not count
    lib.append({**_rule(f"{FF}:tk_uniform", FF_CIT, role="premium", pay_basis="annual",
                        when="True", compute="1200", priority=10),
                "status": "ratified", "approver": "x"})
    _set_library(case, lib)
    assert _total(c, Q8) == ("costing", 640.8)
    v = c.get("/admin/verification").json()
    g = next(g for g in v["goldens"] if g["name"] == GOLDEN_640)
    assert (g["status"], g["actual"]) == ("pass", 640.8)
    # a bogus HOURLY premium is the thing the gate must now see and refuse
    _propose(case, [_rule(f"{FF}:tk_bogus_25", FF_CIT, role="premium", when="True",
                          compute="25", priority=5)])
    res = c.post("/admin/ratify", json={"approver": "tester"}).json()
    assert res["ratified"] == [] and res["golden_failed"]["actual"] == 665.8
    assert _total(c, Q8) == ("costing", 640.8)


def test_gate_total_equals_chat_total_for_currency_known_answers(env):
    c, case = env
    v = c.get("/admin/verification").json()
    cs = core_app._case()
    for g in cs.golden_cases():
        if g.get("result_type", "currency") != "currency":
            continue
        subj = g["subjects"][0]
        q = f"Cost an {int(g['params']['hours'])}-hour overtime shift for a {subj}"
        if g["params"].get("years_of_service"):      # J1a: tenure comes from the question
            q += f" with {int(g['params']['years_of_service'])} years of service"
        mode, total = _total(c, q + ".")
        actual = next(x["actual"] for x in v["goldens"] if x["name"] == g["name"])
        assert (mode, total) == ("costing", actual), g["name"]


def test_no_lossy_copy_of_the_library_remains():
    needle = "_ratified" + "_dicts"   # built at run time so this file does not match itself
    hits = []
    for sub in ("core", "scripts", "tests"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, sub)):
            for name in files:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    if needle in f.read():
                        hits.append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    assert hits == [], hits


def test_release_gate_passes_on_the_shipped_case(env):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import prepare_deploy
    assert prepare_deploy._goldens_fail("cases/santacruz") is False


# ---------------- E1: coverage ----------------
def test_shipped_library_every_rule_fires_in_a_passing_known_answer(env):
    c, case = env
    v = c.get("/admin/verification").json()
    assert v["all_passing"] is True and v["unverified"] == []
    assert set(v["proved_by"]) == {r["id"] for r in _library(case)}
    for g in v["goldens"]:
        assert g["fired"], g["name"]


def test_rule_no_known_answer_exercises_is_refused(env):
    c, case = env
    _propose(case, [_rule("management_mou:tk_99x", MGMT_CIT,
                          compute="effective_base * 99 * hours")])
    res = c.post("/admin/ratify", json={"rule_ids": ["management_mou:tk_99x"], "approver": "tester"}).json()
    assert res["ratified"] == [] and res["uncovered"] == ["management_mou:tk_99x"]
    assert "no known answer exercises" in res["warning"]
    assert len(_library(case)) == N_LIVE
    assert _total(c, "Cost an 8-hour overtime shift for a Fire Marshal (top step).")[0] == "blocked"


def test_unexercised_branch_of_a_covered_unit_is_refused(env):
    c, case = env
    _propose(case, [_rule(f"{FF}:tk_long_shift", FF_CIT, priority=50, when="hours > 8",
                          compute="effective_base * 100 * hours")])
    res = c.post("/admin/ratify", json={"approver": "tester"}).json()
    assert res["ratified"] == [] and res["uncovered"] == [f"{FF}:tk_long_shift"]
    assert _total(c, Q8.replace("an 8-hour", "a 12-hour")) == ("costing", 961.2)


def test_replacing_a_proven_rule_so_its_known_answer_goes_unanswered_is_refused(env):
    c, case = env
    _propose(case, [_rule(FF_OT, FF_CIT, when="hours > 8",
                          compute="effective_base * 100 * hours")])
    res = c.post("/admin/ratify", json={"approver": "tester"}).json()
    assert res["ratified"] == [], res
    assert "no longer be answered" in res["warning"]
    assert _total(c, Q8) == ("costing", 640.8)


def test_untagged_wrong_rule_into_empty_library_is_refused(env):
    c, case = env
    _set_library(case, [])
    _propose(case, [_rule(FF_OT, FF_CIT, compute="effective_base * 2.0 * hours")])
    res = c.post("/admin/ratify", json={"approver": "tester"}).json()
    assert res["ratified"] == [] and res["golden_failed"]["actual"] == 854.4
    assert _library(case) == []


def test_correct_rule_into_empty_library_is_approved(env):
    c, case = env
    # The 12-year known answer (656.82) fires the overtime rule too, so the overtime
    # rule alone fails that answer; it must come in with the shipped longevity rule.
    longevity = next(r for r in _library(case) if r["id"] == FF_LONG)
    _set_library(case, [])
    _propose(case, [_rule(FF_OT, FF_CIT),
                    {k: v for k, v in longevity.items()
                     if k not in ("status", "approver", "approved_at")}])
    res = c.post("/admin/ratify", json={"approver": "tester"}).json()
    assert res["ratified"] == [FF_OT, FF_LONG] and res["library_size"] == 2
    assert res["proved_by"][FF_OT] == [GOLDEN_640, GOLDEN_656]
    ev = [e for e in _ledger(case) if e["type"] == "authoring.ratify"
          and e["payload"]["rule_id"] == FF_OT][-1]
    assert ev["payload"]["proved_by"] == res["proved_by"][FF_OT]
    assert _total(c, Q8) == ("costing", 640.8)


def test_approving_the_shipped_rules_again_keeps_the_response_shape(env):
    c, case = env
    _propose(case, [{k: v for k, v in r.items() if k not in ("status", "approver", "approved_at")}
                    for r in _library(case)])
    # J1b: three vacation tiers cite p.22 cells the OCR misread; a person must confirm
    # each on the page image before the gate will (re-)approve them.
    for row, value in ((2, "10.15"), (4, "13.85"), (5, "14.78")):
        assert c.post("/admin/cell_confirm", json={
            "doc_id": FF, "page": 22, "row": row, "col": 1, "value": value,
            "by": "tester"}).status_code == 200
    res = c.post("/admin/ratify", json={"approver": "tester"}).json()
    assert sorted(res["ratified"]) == sorted(r["id"] for r in _library(case))
    assert res["library_size"] == N_LIVE and set(res["proved_by"]) == set(res["ratified"])


def test_verification_lists_a_live_rule_that_never_fires(env):
    c, case = env
    lib = _library(case)
    lib.append({**_rule(f"{FF}:tk_dead", FF_CIT, when="hours > 9999"),
                "status": "ratified", "approver": "x"})
    _set_library(case, lib)
    v = c.get("/admin/verification").json()
    assert [u["id"] for u in v["unverified"]] == [f"{FF}:tk_dead"]
    assert f"{FF}:tk_dead" not in v["proved_by"]


def test_malformed_rule_is_a_validation_error_not_a_500(env):
    c, case = env
    _set_library(case, [])
    _propose(case, [_rule(FF_OT, FF_CIT, role="zzz")])
    res = c.post("/admin/ratify", json={"approver": "tester"}).json()
    assert res["ratified"] == [] and FF_OT in res["rejected"]
    _propose(case, [_rule(FF_OT, FF_CIT, priority="high")])
    res = c.post("/admin/ratify", json={"approver": "tester"}).json()
    assert res["ratified"] == [] and FF_OT in res["rejected"]
    assert c.get("/admin/proposed").status_code == 200
    assert c.get("/admin/verification").status_code == 200
    assert _library(case) == []


def test_block_is_ledgered(env):
    c, case = env
    _propose(case, [_rule("management_mou:tk_99x", MGMT_CIT,
                          compute="effective_base * 99 * hours")])
    c.post("/admin/ratify", json={"approver": "tester"})
    blocked = [e for e in _ledger(case) if e["type"] == "authoring.blocked"]
    assert blocked and blocked[-1]["payload"]["uncovered"] == ["management_mou:tk_99x"]
    assert blocked[-1]["payload"]["approver"] == "tester"
    assert c.get("/admin/ledger").json()["verified"] is True


# ---------------- E3: try to break it ----------------
def test_try_break_shipped_overtime_rule(env):
    c, case = env
    with open(os.path.join(case, "rules", "rules_ratified.json"), "rb") as f:
        before_bytes = f.read()
    n_before = len(_ledger(case))
    res = c.post("/admin/try_break", json={"rule_id": FF_OT}).json()
    assert (res["total"], res["caught"], res["verdict"]) == (7, 4, "partly tested")
    row = next(m for m in res["mutants"] if m["label"] == "compute: 1.5 -> 1.6")
    assert row["caught"] and (row["caught_by"]["expected"], row["caught_by"]["actual"]) == (640.8, 683.52)
    assert row["caught_by"]["expected_text"] == "$640.80"
    assert row["caught_by"]["actual_text"] == "$683.52"
    assert row["refusal"] and "Nothing was approved" in row["refusal"]
    survivors = [m["label"] for m in res["mutants"] if not m["caught"]]
    assert survivors == ["drop condition: when hours > 0 -> True",
                         "move threshold: 0 -> 0.1", "move threshold: 0 -> -0.1"]
    assert res["survivors"] == survivors
    with open(os.path.join(case, "rules", "rules_ratified.json"), "rb") as f:
        assert f.read() == before_bytes
    events = _ledger(case)
    new = [e for e in events[n_before:]]
    assert [e["type"] for e in new] == ["authoring.mutation_check"]
    assert new[0]["payload"] == {"rule_id": FF_OT, "total": 7, "caught": 4,
                                 "survivors": survivors, "verdict": "partly tested"}
    assert c.get("/admin/ledger").json()["verified"] is True


def test_try_break_flags_untested_rule(env):
    c, case = env
    lib = _library(case)
    lib.append({**_rule(f"{FF}:tk_dead", FF_CIT, when="hours > 9999"),
                "status": "ratified", "approver": "x"})
    _set_library(case, lib)
    res = c.post("/admin/try_break", json={"rule_id": f"{FF}:tk_dead"}).json()
    assert res["verdict"] == "not actually tested" and res["caught"] == 0
    assert res["fires_in"] == []


def test_try_break_takes_a_proposed_rule_first(env):
    c, case = env
    _propose(case, [_rule(FF_OT, FF_CIT, compute="effective_base * 2.0 * hours")])
    res = c.post("/admin/try_break", json={"rule_id": FF_OT}).json()
    # the 2.0x draft fires in no PASSING known answer, so nothing tests it
    assert res["verdict"] == "not actually tested"
    # 2.0 is integer-valued and >= 2, so its step is 1
    assert any(m["label"] == "compute: 2.0 -> 3.0" for m in res["mutants"])


def test_try_break_unknown_rule_is_404(env):
    c, _ = env
    assert c.post("/admin/try_break", json={"rule_id": "nope:x"}).status_code == 404


def test_try_break_makes_no_model_call(env, monkeypatch):
    c, _ = env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    with llm.record() as trail:
        res = c.post("/admin/try_break", json={"rule_id": FF_OT}).json()
    assert res["total"] == 7 and trail == []


def test_mutation_report_over_the_live_library(env):
    c, case = env
    n_before = len(_ledger(case))
    res = c.get("/admin/mutation_report").json()
    # 7 mutants per currency rule, 3 per selector, 9 for the longevity modifier; J1b's
    # five vacation tiers add 9+9+9+9+7 (value, drop/invert condition, move each
    # threshold, re-home). Their survivors are the moved-threshold mutants: the known
    # answers sit mid-tier (3, 7, 12, 15, 20 years), so an edge moved by one year is
    # not exercised (E10 boundary known answers would catch them).
    assert (res["total"], res["caught"]) == (72, 43)
    vac = {f"{FF}:vacation_accrual_{t}" for t in ("1_5", "6_10", "11_13", "14_16", "17_plus")}
    assert {s["rule_id"] for s in res["survivors"]} == {FF_OT, FF_LONG,
                                                        "admin_group_mou:overtime_premium_rate"} | vac
    assert all("move threshold" in s["label"] or "drop condition" in s["label"]
               for s in res["survivors"] if s["rule_id"] in vac)
    assert len(_ledger(case)) == n_before


# ---------------- E7: every verification sentence literally true ----------------
def test_admin_page_makes_no_stale_verification_claims(env):
    c, _ = env
    html = c.get("/admin").text
    for stale in ("Every live rule is exercised by at least one known answer",
                  "They reproduced every known answer",
                  "Checks passed",
                  "a real paystub, or a figure"):
        assert stale not in html, stale
    assert "No rules are live" in html
    assert "Proved by" in html
    assert "Try to break it" in html


def test_tour_does_not_call_the_known_answers_a_paystub(env):
    c, _ = env
    js = c.get("/static/tour.js").text
    assert "a real paystub or a hand-verified figure" not in js
    assert "not taken from payroll" in js
