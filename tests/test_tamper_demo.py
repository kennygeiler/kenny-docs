"""F3 — break the chain on a COPY: which entry fails, why, and the real file untouched."""
import glob
import json
import os
import shutil
import stat
import tempfile

import pytest

import core.app as core_app
from core import ledger as ledger_mod
from core import tamper_demo
from core.ledger import Ledger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def led(tmp_path, monkeypatch):
    monkeypatch.delenv(ledger_mod.KEY_ENV, raising=False)
    led = Ledger(str(tmp_path / "ledger.jsonl"))
    for i in range(10):
        led.append("event", {"i": i}, actor="test", query_id="q1")
    led.append("answer.snapshot", {"total": 640.8, "snapshot": "x.json"}, actor="engine",
               query_id="q1")
    led.append("authoring.ratify", {"rule_id": "r", "approver": "kenny"}, actor="admin")
    return led


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _no_scratch_left():
    return not glob.glob(os.path.join(tempfile.gettempdir(), "kenny-tamper-*"))


def test_edit_names_the_entry_and_both_hashes(led):
    before = _bytes(led.path)
    out = tamper_demo.run(led.path)          # default target: latest answer.snapshot total
    assert out["mode"] == "edit"
    assert out["target"]["seq"] == 10 and out["target"]["field"] == "payload.total"
    assert out["target"]["before"] == 640.8 and out["target"]["after"] == 940.8
    assert out["copy"]["ok"] is False
    assert out["copy"]["failed_seq"] == 10
    assert out["copy"]["reason"] == "hash_mismatch"
    assert out["copy"]["stored_hash"] != out["copy"]["recomputed_hash"]
    assert out["real"]["untouched"] is True and out["real"]["verified"] is True
    assert _bytes(led.path) == before
    assert _no_scratch_left()


def test_edit_approver_preset(led):
    out = tamper_demo.run(led.path, preset="approver")
    assert out["target"]["seq"] == 11 and out["target"]["field"] == "payload.approver"
    assert out["copy"]["failed_seq"] == 11 and out["copy"]["reason"] == "hash_mismatch"


def test_delete_reports_seq_gap(led):
    out = tamper_demo.run(led.path, mode="delete", seq=4)
    assert out["copy"]["reason"] == "seq_gap"
    assert out["copy"]["failed_seq"] == 5
    assert out["real"]["untouched"] is True


def test_rechain_without_key_passes_verify_but_fails_anchor(led):
    out = tamper_demo.run(led.path, mode="rechain", seq=3, field="payload.i", value="99")
    assert out["copy"]["ok"] is True
    assert out["copy"]["anchor"]["ok"] is False
    assert "rewritten" in out["copy"]["anchor"]["message"]
    assert "recorded head" in out["explanation"]


def test_rechain_with_key_is_refused_as_unkeyed(tmp_path, monkeypatch):
    monkeypatch.setenv(ledger_mod.KEY_ENV, "demo-key")
    led = Ledger(str(tmp_path / "keyed.jsonl"))
    for i in range(6):
        led.append("event", {"i": i}, actor="test")
    out = tamper_demo.run(led.path, mode="rechain", seq=2, field="payload.i", value="7")
    assert out["copy"]["ok"] is False
    assert out["copy"]["reason"] == "unkeyed_event"
    assert out["copy"]["failed_seq"] == 2


def test_truncate_passes_verify_but_fails_anchor(led):
    out = tamper_demo.run(led.path, mode="truncate")
    assert out["copy"]["ok"] is True
    assert out["copy"]["events"] == 7
    assert out["copy"]["anchor"]["ok"] is False
    assert "truncated" in out["copy"]["anchor"]["message"]


def test_readonly_real_file_every_mode(led):
    os.chmod(led.path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        for mode in tamper_demo.MODES:
            out = tamper_demo.run(led.path, mode=mode, seq=5 if mode != "truncate" else None)
            assert out["real"]["untouched"] is True, mode
    finally:
        os.chmod(led.path, stat.S_IRUSR | stat.S_IWUSR)


def test_uncovered_fields_are_refused(led):
    for field in ("query_id", "iso", "alg", "hash"):
        with pytest.raises(tamper_demo.TamperDemoError) as ei:
            tamper_demo.run(led.path, seq=1, field=field, value="x")
        assert "F9" in str(ei.value) or "not editable" in str(ei.value)


def test_verify_detail_reports_garbage_line(led):
    with open(led.path, "a") as f:
        f.write('{"seq": 12, "this is not": \n')
    d = led.verify_detail()
    assert d["ok"] is False
    assert d["reason"] == "unreadable_line"
    assert d["failed_line"] == 13
    ok, msg = led.verify()
    assert not ok and "unreadable" in msg


def test_verify_detail_head_and_count(led):
    d = led.verify_detail()
    assert d["ok"] and d["count"] == 12 and d["keyed"] is False
    assert d["head"]["seq"] == 11 and d["head"]["hash"] == led.head()["hash"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(ledger_mod.KEY_ENV, raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def test_endpoint_on_case_copy(client):
    client.post("/chat", json={"prompt": "Cost an 8-hour overtime shift for a "
                                         "Firefighter/Paramedic (56 hr, top step)"})
    before = client.get("/admin/ledger").json()
    res = client.get("/admin/ledger/tamper-demo")
    assert res.status_code == 200
    body = res.json()
    assert body["copy"]["reason"] == "hash_mismatch"
    assert "Real ledger: untouched" in body["explanation"]
    after = client.get("/admin/ledger").json()
    assert after["verified"] is True and after["count"] == before["count"]
    assert after["head"]["hash"] == before["head"]["hash"]
    assert client.get("/admin/ledger/tamper-demo",
                      params={"field": "query_id", "seq": 1}).status_code == 400
    assert client.get("/admin/ledger/tamper-demo", params={"mode": "nope"}).status_code == 400
