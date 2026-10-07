"""F2 — approvals in the ledger: who approved which rule text, against which clause and
known answer, when; honest back-fill for the live rules that predate the record."""
import json
import os
import shutil
import subprocess
import sys

import pytest

import core.app as core_app
from core import approvals, provenance
from core.caseio import load_case
from core.ledger import Ledger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# live rules shipped in the case (data-goldens added longevity_10yr as the fifth)
N_LIVE = len(json.load(open(os.path.join(ROOT, "cases", "santacruz", "rules",
                                         "rules_ratified.json")))["rules"])
OT_ID = "firefighters_local3535_mou:overtime_premium_rate"


@pytest.fixture
def case_dir(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("KENNY_LEDGER_KEY", raising=False)
    return str(case)


@pytest.fixture
def client(case_dir):
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def _events(case_dir):
    p = os.path.join(case_dir, "ledger.jsonl")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def _ratified(case_dir):
    with open(os.path.join(case_dir, "rules", "rules_ratified.json")) as f:
        return json.load(f)["rules"]


def _propose_overtime(case_dir):
    """Put the live Local 3535 overtime rule in the proposed queue with its scenario."""
    rule = next(r for r in _ratified(case_dir) if r["id"] == OT_ID)
    rule = {k: v for k, v in rule.items() if k not in ("status", "approver", "approved_at")}
    rule["_scenario"] = ("8-hour overtime shift, Firefighter/Paramedic top step "
                         "(1.5x per Local 3535 MOU)")
    with open(os.path.join(case_dir, "rules", "rules_proposed.json"), "w") as f:
        json.dump({"rules": [rule], "needs_data": []}, f)
    return rule


def test_ratify_records_full_approval(client, case_dir):
    rule = _propose_overtime(case_dir)
    res = client.post("/admin/ratify", json={"approver": "dana", "rule_ids": [OT_ID]}).json()
    assert res["ratified"] == [OT_ID], res
    evs = _events(case_dir)
    ev = [e for e in evs if e["type"] == "authoring.ratify"][-1]
    pl = ev["payload"]
    assert pl["rule_sha256"] == provenance.rule_fingerprint(rule)
    assert pl["approver"] == "dana" and pl["approver_source"] == "self-declared"
    assert pl["citation"]["clause"] == "Overtime Rate (p.8)"
    assert pl["rule"]["compute"] == "effective_base * 1.5 * hours"
    assert pl["backfilled"] is False
    ka = pl["known_answers"][0]
    assert ka["expected"] == 640.8 and ka["status"] == "pass"
    by_seq = {e["seq"]: e for e in evs}
    assert by_seq[ka["golden_check_seq"]]["type"] == "authoring.golden_check"
    # the library entry points back at its own record
    live = next(r for r in _ratified(case_dir) if r["id"] == OT_ID)
    assert live["approval"] == {"seq": ev["seq"], "hash": ev["hash"]}
    assert live["approver"] == "dana"


def test_ratify_requires_named_approver(client, case_dir):
    _propose_overtime(case_dir)
    before = len(_events(case_dir))
    res = client.post("/admin/ratify", json={"approver": "", "rule_ids": [OT_ID]}).json()
    assert res["ratified"] == []
    assert "name" in res["warning"].lower()
    assert not [e for e in _events(case_dir)[before:] if e["type"] == "authoring.ratify"]


def test_backfill_empty_ledger(case_dir):
    """Production shape: the ledger holds no authoring events at all."""
    p = os.path.join(case_dir, "ledger.jsonl")
    if os.path.exists(p):
        os.remove(p)
    rules_path = os.path.join(case_dir, "rules", "rules_ratified.json")
    rules_bytes = open(rules_path, "rb").read()
    res = approvals.backfill(load_case(case_dir))
    assert len(res["appended"]) == N_LIVE
    evs = [e for e in _events(case_dir) if e["type"] == "authoring.ratify"]
    assert len(evs) == N_LIVE
    lib = {r["id"]: r for r in json.loads(rules_bytes)["rules"]}
    for e in evs:
        pl = e["payload"]
        assert pl["backfilled"] is True and pl["original_event"] is None
        # the approver/time are copied from the library entry (four rules say kenny,
        # 2026-07-18; data-goldens' longevity_10yr still carries its agent approver)
        assert pl["approver"] == lib[pl["rule_id"]]["approver"]
        assert pl["approved_at"] == lib[pl["rule_id"]]["approved_at"]
        assert e["actor"] == "system"
        assert "not captured at the moment of approval" in pl["basis"]
        assert pl["known_answers"] and pl["known_answers"][0]["status"] == "pass"
    ok, msg = Ledger(p).verify()
    assert ok, msg
    # idempotent, and the shipped rule file is byte-identical
    res2 = approvals.backfill(load_case(case_dir))
    assert res2["appended"] == [] and len(res2["skipped"]) == N_LIVE
    assert len([e for e in _events(case_dir) if e["type"] == "authoring.ratify"]) == N_LIVE
    assert open(rules_path, "rb").read() == rules_bytes


def test_backfill_links_originals(case_dir):
    """A ledger that holds the old thin {rule_id, approver} approvals: the back-fill
    cites them as original_event instead of pretending they never happened."""
    led = Ledger(os.path.join(case_dir, "ledger.jsonl"))
    thin = {}
    for r in _ratified(case_dir):
        ev = led.append("authoring.ratify", {"rule_id": r["id"], "approver": "kenny"},
                        actor="admin")
        thin[r["id"]] = ev["seq"]
    approvals.backfill(load_case(case_dir))
    full = [e for e in _events(case_dir)
            if e["type"] == "authoring.ratify" and e["payload"].get("rule_sha256")]
    assert len(full) == N_LIVE
    for e in full:
        assert e["payload"]["original_event"]["seq"] == thin[e["payload"]["rule_id"]]


def test_backfill_script_is_idempotent(case_dir):
    script = os.path.join(ROOT, "scripts", "backfill_provenance.py")
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    rules_path = os.path.join(case_dir, "rules", "rules_ratified.json")
    rules_bytes = open(rules_path, "rb").read()
    out = subprocess.run([sys.executable, script, case_dir, "--approvals"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.count("appended authoring.ratify") == N_LIVE
    n = len(_events(case_dir))
    out = subprocess.run([sys.executable, script, case_dir, "--approvals"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0
    assert "appended" not in out.stdout.split("ledger verify")[0]
    assert len(_events(case_dir)) == n
    assert open(rules_path, "rb").read() == rules_bytes


def test_approvals_endpoint(client, case_dir):
    approvals.backfill(load_case(case_dir))
    rows = client.get("/admin/approvals").json()["approvals"]
    assert len(rows) == N_LIVE
    assert all(r["matches_live"] for r in rows)
    assert all(r["approval"]["backfilled"] for r in rows)
    rules_path = os.path.join(case_dir, "rules", "rules_ratified.json")
    with open(rules_path) as f:
        lib = json.load(f)
    for r in lib["rules"]:
        if r["id"] == OT_ID:
            r["compute"] = "effective_base * 2 * hours"
    with open(rules_path, "w") as f:
        json.dump(lib, f, indent=2)
    rows = {r["rule_id"]: r for r in client.get("/admin/approvals").json()["approvals"]}
    assert rows[OT_ID]["matches_live"] is False
    assert all(v["matches_live"] for k, v in rows.items() if k != OT_ID)
