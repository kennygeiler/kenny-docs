"""F1 — replayable answers: every input frozen, the snapshot bound to the ledger, and
GET /chat/replay recomputing the number from the frozen inputs."""
import hashlib
import json
import os
import shutil
import subprocess
import sys

import pytest

import core.app as core_app
from core import audit, provenance
from core.caseio import load_case
from core.ruledsl import Rule

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT = "Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step)"


@pytest.fixture
def case_dir(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return str(case)


@pytest.fixture
def client(case_dir):
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def _ask(client, prompt=PROMPT, **body):
    res = client.post("/chat", json={"prompt": prompt, **body}).json()
    assert res["mode"] == "costing", res
    return res


def _snapshot(case_dir, name):
    with open(os.path.join(case_dir, "snapshots", name)) as f:
        return json.load(f)


def _events(case_dir):
    with open(os.path.join(case_dir, "ledger.jsonl")) as f:
        return [json.loads(l) for l in f if l.strip()]


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def test_snapshot_records_inputs(client, case_dir):
    res = _ask(client)
    evs = _events(case_dir)
    snap_ev = [e for e in evs if e["type"] == "answer.snapshot"][-1]["payload"]
    snap = _snapshot(case_dir, snap_ev["snapshot"])
    assert snap["schema"] == 2
    assert snap["subjects"][0]["base_hourly"] == 53.4
    assert snap["rules"][0]["role"] == "base"
    assert snap["inputs"]["engine_sha256"]
    assert snap["inputs"]["data"]["sha256"]
    assert snap_ev["snapshot_sha256"] == _sha(os.path.join(case_dir, "snapshots",
                                                            snap_ev["snapshot"]))
    assert snap_ev["result_sha256"] == snap["result_sha256"]
    assert snap_ev["rules"][0]["sha256"] == snap["rules"][0]["sha256"]
    data_read = [e for e in evs if e["type"] == "data.read"][-1]["payload"]
    assert 53.4 in [r.get("base_hourly") for r in data_read["records"]]
    assert res["result"]["total"] == 640.8


def test_replay_matches(client, case_dir):
    qid = _ask(client)["query_id"]
    before = len(_events(case_dir))
    rep = client.get(f"/chat/replay/{qid}").json()
    assert rep["status"] == "match", rep
    assert rep["recomputed"]["total"] == 640.8
    assert all(c["ok"] for c in rep["checks"])
    assert {c["name"] for c in rep["checks"]} >= {"snapshot_sha256", "total",
                                                    "result_sha256_vs_ledger", "ledger_chain"}
    assert rep["drift"]["data"] == "unchanged"
    assert len(_events(case_dir)) == before          # read-only


def test_replay_survives_roster_change(client, case_dir):
    qid = _ask(client)["query_id"]
    roster = os.path.join(case_dir, "data", "roster.csv")
    with open(roster) as f:
        text = f.read()
    assert ",53.40," in text
    with open(roster, "w") as f:
        f.write(text.replace(",53.40,", ",55.00,"))
    rep = client.get(f"/chat/replay/{qid}").json()
    assert rep["status"] == "match"
    assert rep["recomputed"]["total"] == 640.8
    assert rep["drift"]["data"] == "changed"
    assert _ask(client)["result"]["total"] == 660.0


def test_replay_detects_edited_snapshot(client, case_dir):
    qid = _ask(client)["query_id"]
    name = [e for e in _events(case_dir) if e["type"] == "answer.snapshot"][-1]["payload"]["snapshot"]
    path = os.path.join(case_dir, "snapshots", name)
    snap = _snapshot(case_dir, name)
    snap["result"]["total"] = 999.99
    with open(path, "w") as f:
        json.dump(snap, f, indent=2)
    rep = client.get(f"/chat/replay/{qid}").json()
    assert rep["status"] == "mismatch"
    failing = {c["name"] for c in rep["checks"] if not c["ok"]}
    assert "snapshot_sha256" in failing
    assert "total" in failing


def test_snapshot_is_write_once(client, case_dir):
    first = _ask(client, query_id="abcdef123456")
    name1 = [e for e in _events(case_dir) if e["type"] == "answer.snapshot"][-1]["payload"]["snapshot"]
    path1 = os.path.join(case_dir, "snapshots", name1)
    bytes1 = open(path1, "rb").read()
    _ask(client, prompt="Cost a 4-hour overtime shift for a Firefighter/Paramedic "
                        "(56 hr, top step)", query_id="abcdef123456")
    name2 = [e for e in _events(case_dir) if e["type"] == "answer.snapshot"][-1]["payload"]["snapshot"]
    assert name2 != name1
    assert open(path1, "rb").read() == bytes1
    assert first["result"]["total"] == 640.8


def test_premium_is_ledgered_and_replays(client, case_dir):
    rules_path = os.path.join(case_dir, "rules", "rules_ratified.json")
    with open(rules_path) as f:
        lib = json.load(f)
    lib["rules"].append({
        "id": "firefighters_local3535_mou:hazmat_premium", "kind": "selector",
        "role": "premium", "result_type": "currency", "pay_basis": "per_shift",
        "topic": "hazmat", "when": "True", "compute": "25",
        "human_readable": "test premium",
        "citation": {"doc_id": "firefighters_local3535_mou", "clause": "test", "page": 8},
        "status": "ratified", "approver": "test", "approved_at": "2026-01-01T00:00:00Z"})
    with open(rules_path, "w") as f:
        json.dump(lib, f, indent=2)
    res = _ask(client)
    assert res["result"]["total"] == 665.8
    qid = res["query_id"]
    assert any(e["type"] == "rule.premium" for e in _events(case_dir))
    rep = client.get(f"/chat/replay/{qid}").json()
    assert rep["status"] == "match"
    assert rep["recomputed"]["total"] == 665.8


def test_v1_snapshot_not_replayable(client, case_dir):
    qid = _ask(client)["query_id"]
    ev = [e for e in _events(case_dir) if e["type"] == "answer.snapshot"][-1]["payload"]
    path = os.path.join(case_dir, "snapshots", ev["snapshot"])
    os.remove(path)
    # legacy writer: no subjects -> schema 1
    case = load_case(case_dir)
    rules = case.rules()
    audit.snapshot(os.path.join(case_dir, "snapshots"), qid, {"hours": 8}, rules,
                   {"total": 640.8})
    rep = client.get(f"/chat/replay/{qid}").json()
    assert rep["status"] == "not_replayable"
    assert "predates" in rep["reason"]


def test_replay_rejects_traversal(client, case_dir):
    _ask(client)
    rep = client.get("/chat/replay/..%2Frules%2Frules_ratified")
    assert rep.status_code in (400, 404)
    if rep.status_code == 400:
        assert rep.json()["status"] == "not_replayable"
    # a forged event naming a path outside snapshots/ is refused by name, never read
    from core.ledger import Ledger
    led = Ledger(os.path.join(case_dir, "ledger.jsonl"))
    led.append("answer.snapshot", {"total": 1, "snapshot": "../rules/rules_ratified.json"},
               actor="engine", query_id="forged000001")
    rep = client.get("/chat/replay/forged000001").json()
    assert rep["status"] == "not_replayable"
    assert "bare file name" in rep["reason"]


def test_rule_fingerprint_ignores_bookkeeping():
    base = {"id": "x:y", "kind": "selector", "compute": "1", "citation": {"clause": "c"}}
    a = provenance.rule_fingerprint(base)
    b = provenance.rule_fingerprint({**base, "status": "ratified", "approver": "kenny",
                                     "approved_at": "2026", "scope_rank": 2,
                                     "_scenario": "s", "citation": {"clause": "c",
                                                                    "doc_sha256": "ff"}})
    assert a == b
    assert a == provenance.rule_fingerprint(
        Rule.from_dict({**base, "status": "ratified", "approver": "k"}))
    assert a != provenance.rule_fingerprint({**base, "compute": "2"})


def test_replay_script_all_exits_zero(client, case_dir):
    _ask(client)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "replay.py"),
                          case_dir, "--all"], capture_output=True, text=True,
                         env={**os.environ, "HF_HUB_OFFLINE": "1"})
    assert out.returncode == 0, out.stdout + out.stderr
    assert "match" in out.stdout
