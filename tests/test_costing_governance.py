"""DEMO_TICKETS B2 — per-subject governance in the costing path.

Each named classification resolves to ITS OWN governing contract and ITS OWN ratified
rules. A unit with no approved rule, no contract or stale rules gets a 'Not covered'
row with a reason — never another unit's rule and never a caller-supplied document.
No API key, BM25 backend, every request through the real FastAPI app on a scratch copy
of the shipped Santa Cruz case.
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import auth  # noqa: E402

CASE_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "cases", "santacruz")
FF = "Firefighter/Paramedic (56 hr, top step)"
FM = "Fire Marshal (top step)"
AA = "Administrative Analyst (top step)"


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


def _events(qid):
    return core_app._case().ledger().for_query(qid)


def _unit_of_doc(case, doc_id):
    return (core_app.load_case(str(case)).source_by_id(doc_id) or {}).get("bargaining_unit")


# (a) a mixed question prices the covered unit and refuses the other, by name --------
def test_mixed_units_price_each_under_its_own_contract(client, case_dir):
    res = _ask(client, f"Cost an 8-hour overtime shift for a {FF} and a {FM}")
    assert res["mode"] == "costing", res
    assert res["partial"] is True
    lines = res["result"]["line_items"]
    assert [(li["subject"], li["rule_id"], li["total"]) for li in lines] == [
        (FF, "firefighters_local3535_mou:overtime_premium_rate", 640.80)]
    assert lines[0]["bargaining_unit"] == "firefighters-local-3535"
    assert res["result"]["total"] == 640.80          # today: 1943.88
    unc = res["result"]["uncovered"]
    assert [u["subject"] for u in unc] == [FM]
    assert unc[0]["bargaining_unit"] == "management"
    assert "management" in unc[0]["reason"].lower()
    assert "no approved rule" in unc[0]["reason"].lower()
    assert "firefighters" not in unc[0]["reason"].lower()
    types = [e["type"] for e in _events(res["query_id"])]
    assert "costing.uncovered" in types
    assert "answer.snapshot" in types
    # the snapshot froze only the rules that were used — not the whole library
    snap = json.load(open(case_dir / "snapshots" / f"{res['query_id']}.json"))
    assert [r["id"] for r in snap["rule_versions"]] == [
        "firefighters_local3535_mou:overtime_premium_rate"]
    assert snap["result"]["uncovered"][0]["subject"] == FM


# (b) every line item is priced by a rule from the subject's own unit's document ------
def test_every_line_item_is_priced_by_its_own_units_document(client, case_dir):
    res = _ask(client, "Cost an 8-hour overtime shift for all classifications")
    assert res["mode"] == "costing", res
    case = core_app.load_case(str(case_dir))
    subjects = {s["name"]: s for s in case.subjects()}
    units_with_currency_rule = {_unit_of_doc(case_dir, r.citation.doc_id)
                                for r in case.rules() if r.result_type == "currency"}
    lines = res["result"]["line_items"]
    assert lines, res
    for li in lines:
        doc_id = li["rule_id"].split(":")[0]
        assert _unit_of_doc(case_dir, doc_id) == subjects[li["subject"]]["bargaining_unit"], li
        assert li["bargaining_unit"] == subjects[li["subject"]]["bargaining_unit"]
    by_subject = {li["subject"]: li for li in lines}
    assert by_subject[AA]["total"] == 716.16
    assert by_subject[AA]["rule_id"] == "admin_group_mou:overtime_premium_rate"
    for li in lines:
        assert subjects[li["subject"]]["bargaining_unit"] in units_with_currency_rule
    covered = {li["subject"] for li in lines}
    uncovered = {u["subject"] for u in res["result"]["uncovered"]}
    assert covered.isdisjoint(uncovered)
    assert covered | uncovered == set(subjects)
    assert res["result"]["total"] == round(sum(li["total"] for li in lines), 2)


# (c) a single uncovered unit keeps the long-standing blocked message -----------------
def test_single_unit_without_rules_is_blocked_with_unchanged_message(client):
    res = _ask(client, f"Cost an 8-hour overtime shift for a {FM}")
    assert res["mode"] == "blocked"
    assert "**management_mou** has no human-ratified rules" in res["message"]
    assert "result" not in res
    assert res["uncovered"][0]["subject"] == FM


# (d) a caller-supplied doc_id never replaces governance -------------------------------
def test_doc_id_in_body_is_ignored_and_ledgered(client):
    res = _ask(client, f"Cost an 8-hour overtime shift for a {FM}",
               doc_id="firefighters_local3535_mou")
    assert res["mode"] == "blocked", res
    assert "1303.08" not in json.dumps(res)
    types = [e["type"] for e in _events(res["query_id"])]
    assert "chat.prompt" in types and "intent.classify" in types
    assert "costing.doc_id_ignored" in types

    res = _ask(client, f"Cost an 8-hour overtime shift for a {FF}", doc_id="admin_group_mou")
    assert res["mode"] == "costing", res
    assert res["result"]["line_items"][0]["rule_id"].startswith("firefighters_local3535_mou")
    assert res["result"]["line_items"][0]["citations"][0]["doc_id"] == "firefighters_local3535_mou"
    assert "chat.prompt" in [e["type"] for e in _events(res["query_id"])]


# (e) a roster row whose unit has no contract is blocked, with no document buttons ----
def test_unit_with_no_source_is_blocked_without_options(client, case_dir):
    with open(case_dir / "data" / "roster.csv", "a") as f:
        f.write("Test Clerk (top step),admin,Test Clerk,10.00,Day,unit-with-no-contract\n")
    res = _ask(client, "Cost an 8-hour overtime shift for a Test Clerk (top step)")
    assert res["mode"] == "blocked", res
    assert "No contract on file" in res["message"]
    assert "unit-with-no-contract" in res["message"]
    assert not res.get("options") and not res.get("needs_confirmation")
    assert "result" not in res


# (f) two units with different multipliers: each subject gets its own ----------------
def test_each_unit_gets_its_own_multiplier(client, case_dir):
    path = case_dir / "rules" / "rules_ratified.json"
    lib = json.load(open(path))
    for r in lib["rules"]:
        if r["id"] == "admin_group_mou:overtime_premium_rate":
            r["compute"] = "effective_base * 2.0 * hours"
    json.dump(lib, open(path, "w"), indent=2)
    res = _ask(client, f"Cost an 8-hour overtime shift for a {FF} and an {AA}")
    assert res["mode"] == "costing", res
    got = {li["subject"]: (li["rule_id"], li["total"]) for li in res["result"]["line_items"]}
    assert got[FF] == ("firefighters_local3535_mou:overtime_premium_rate", 640.80)
    assert got[AA] == ("admin_group_mou:overtime_premium_rate", 954.88)   # 59.68 x 2 x 8
    assert res["result"]["total"] == 1595.68
    assert res["partial"] is False and res["result"]["uncovered"] == []


# the date can exclude a contract: the outcome says so, with the window ---------------
# (asked through cost_by_unit directly: the no-key date stub never captures a year, so
#  a 2020 date cannot reach governance through /chat until B4b's date grammar lands)
def test_date_outside_contract_window_names_the_window(case_dir):
    from core import costing
    case = core_app.load_case(str(case_dir))
    cat = core_app._catalog(case)
    led = case.ledger()
    subs = [s for s in case.subjects() if s["name"] == FF]
    out = costing.cost_by_unit(case, cat, led, "q-date", subs,
                               {"hours": 8.0, "date": "", "date_iso": "2020-01-01",
                                "holiday_weekday": "", "pay_type": "overtime"},
                               "2020-01-01", ["overtime"],
                               core_app._doc_integrity, core_app._raw_ratified)
    assert len(out) == 1 and out[0]["status"] == "no_contract", out
    assert out[0]["reason"].startswith(
        "No contract on file covers 'firefighters-local-3535' on 2020-01-01; ")
    assert "runs 2025" in out[0]["reason"]
    assert out[0]["line_items"] == []


# the shipped golden is untouched by all of this ---------------------------------------
def test_headline_question_still_640_80(client):
    res = _ask(client, f"Cost an 8-hour overtime shift for a {FF}")
    assert res["mode"] == "costing" and res["result"]["total"] == 640.80
    assert res["partial"] is False
    assert res["chosen_doc"] == "firefighters_local3535_mou"
    assert res["routing_path"] == "governance"
