"""Tamper-evidence tests for the audit ledger (TICKETS.md A2).

The threat is an attacker WITH disk access: they can rewrite any event and recompute
every hash after it, or delete the tail. A plain hash chain passes both. The HMAC key
(held off-volume) defeats the rewrite; an externally-recorded head defeats truncation.
"""
import hashlib
import json

import pytest

from core import ledger as ledger_mod
from core.ledger import GENESIS, Ledger


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(ledger_mod.KEY_ENV, "test-secret-key")


def _make(tmp_path, n=3):
    led = Ledger(str(tmp_path / "ledger.jsonl"))
    for i in range(n):
        led.append("event", {"i": i}, actor="test")
    return led


def _rows(led):
    with open(led.path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _write(led, rows):
    with open(led.path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")


def _plain_hash(prev_hash, ev):
    material = (f"{prev_hash}|{ev['seq']}|{ev['ts']}|{ev['actor']}|{ev['type']}|"
                + json.dumps(ev["payload"], sort_keys=True, separators=(",", ":"),
                             default=str))
    return hashlib.sha256(material.encode()).hexdigest()


def test_plain_mode_still_verifies(tmp_path):
    led = _make(tmp_path)
    ok, msg = led.verify()
    assert ok, msg


def test_keyed_chain_verifies(tmp_path, keyed):
    led = _make(tmp_path)
    ok, msg = led.verify()
    assert ok, msg
    assert all(r["alg"] == ledger_mod.ALG_HMAC for r in _rows(led))


def test_rewrite_with_recompute_fails_without_key(tmp_path, keyed):
    """The attack verify() used to miss: edit event 1, recompute hashes 1..tail.
    The attacker has the disk but not KENNY_LEDGER_KEY, so the best they can do is a
    plain-SHA256 rechain — which verify() must reject."""
    led = _make(tmp_path, n=4)
    rows = _rows(led)
    rows[1]["payload"] = {"i": "FORGED"}
    prev = rows[0]["hash"]
    for ev in rows[1:]:
        ev["prev_hash"] = prev
        ev["alg"] = "sha256"          # attacker can't produce HMAC values
        ev["hash"] = _plain_hash(prev, ev)
        prev = ev["hash"]
    _write(led, rows)
    ok, msg = led.verify()
    assert not ok
    assert "unkeyed" in msg or "hash mismatch" in msg


def test_forged_hmac_without_key_fails(tmp_path, keyed):
    """Even claiming alg=hmac-sha256, the attacker can't compute valid values."""
    led = _make(tmp_path, n=3)
    rows = _rows(led)
    rows[1]["payload"] = {"i": "FORGED"}
    rows[1]["hash"] = hashlib.sha256(b"guess").hexdigest()
    _write(led, rows)
    ok, msg = led.verify()
    assert not ok


def test_truncation_passes_verify_but_fails_anchor(tmp_path, keyed):
    """Deleting the tail leaves a valid prefix — verify() alone cannot see it. The
    recorded head (seq+hash, held externally) catches it."""
    led = _make(tmp_path, n=5)
    head = led.head()
    assert head["count"] == 5
    _write(led, _rows(led)[:3])       # attacker deletes the last 2 events
    ok, _ = led.verify()
    assert ok                          # the blind spot, by construction
    ok, msg = led.verify_anchor(head["seq"], head["hash"])
    assert not ok
    assert "truncated" in msg


def test_anchor_intact_on_untouched_ledger(tmp_path, keyed):
    led = _make(tmp_path, n=4)
    head = led.head()
    led.append("later", {"more": True})
    ok, msg = led.verify_anchor(head["seq"], head["hash"])
    assert ok, msg


def test_unkeyed_history_rejected_once_key_is_set(tmp_path, monkeypatch):
    """A ledger written before the key existed must not silently pass under the key —
    that acceptance is exactly the downgrade a rewriting attacker would use."""
    monkeypatch.delenv(ledger_mod.KEY_ENV, raising=False)
    led = _make(tmp_path, n=2)
    monkeypatch.setenv(ledger_mod.KEY_ENV, "new-key")
    ok, msg = led.verify()
    assert not ok
    assert "unkeyed" in msg


def test_export_header_binds_head(tmp_path, keyed):
    led = _make(tmp_path, n=3)
    lines = led.export().splitlines()
    header = json.loads(lines[0])["export"]
    assert header["count"] == 3
    assert header["head"]["seq"] == 2
    assert header["keyed"] is True
    ok, msg = led.verify_anchor(header["head"]["seq"], header["head"]["hash"])
    assert ok, msg


# ---- v2 alg: query_id + alg in the hash; mixed v1/v2 chains; append robustness ----

def _v1_ledger(led, n, key=None):
    """Write n events the way the pre-v2 code did (alg sha256 / hmac-sha256)."""
    prev, rows = GENESIS, []
    for i in range(n):
        ev = {"seq": i, "ts": 1000.0 + i, "iso": "x", "actor": "old", "query_id": f"q{i}",
              "type": "event", "payload": {"i": i}, "prev_hash": prev,
              "alg": ledger_mod.ALG_HMAC_V1 if key else ledger_mod.ALG_PLAIN_V1}
        ev["hash"] = ledger_mod._hash(prev, i, ev["ts"], "old", "event", {"i": i}, key)
        prev = ev["hash"]
        rows.append(ev)
    _write(led, rows)


def test_query_id_edit_is_detected(tmp_path):
    led = Ledger(str(tmp_path / "ledger.jsonl"))
    for i in range(3):
        led.append("event", {"i": i}, actor="t", query_id=f"q{i}")
    rows = _rows(led)
    rows[1]["query_id"] = "someone-elses-question"
    _write(led, rows)
    d = led.verify_detail()
    assert not d["ok"] and d["reason"] == "hash_mismatch" and d["failed_seq"] == 1


def test_alg_relabel_is_detected(tmp_path):
    """Relabelling a v2 event as v1 (which ignores query_id) must not pass."""
    led = Ledger(str(tmp_path / "ledger.jsonl"))
    for i in range(3):
        led.append("event", {"i": i}, actor="t", query_id=f"q{i}")
    rows = _rows(led)
    rows[2]["alg"] = ledger_mod.ALG_PLAIN_V1
    _write(led, rows)
    d = led.verify_detail()
    assert not d["ok"] and d["reason"] == "alg_downgrade"


@pytest.mark.parametrize("use_key", [False, True])
def test_mixed_v1_then_v2_chain_verifies(tmp_path, monkeypatch, use_key):
    """Existing deploys hold hundreds of v1 events; new appends continue in v2."""
    if use_key:
        monkeypatch.setenv(ledger_mod.KEY_ENV, "k")
    else:
        monkeypatch.delenv(ledger_mod.KEY_ENV, raising=False)
    led = Ledger(str(tmp_path / "ledger.jsonl"))
    _v1_ledger(led, 5, key=b"k" if use_key else None)
    assert led.verify()[0]
    led.append("event", {"i": 5}, query_id="q5")
    led.append("event", {"i": 6}, query_id="q6")
    rows = _rows(led)
    assert [r["seq"] for r in rows] == list(range(7))
    assert rows[5]["prev_hash"] == rows[4]["hash"]
    assert rows[6]["alg"] == (ledger_mod.ALG_HMAC if use_key else ledger_mod.ALG_PLAIN)
    ok, msg = led.verify()
    assert ok, msg
    # v1 events still can't hide a payload edit
    rows[2]["payload"] = {"i": "FORGED"}
    _write(led, rows)
    assert not led.verify()[0]


def test_malformed_last_line_does_not_break_append(tmp_path):
    led = _make(tmp_path, n=3)
    with open(led.path, "a") as f:
        f.write('{"seq": 3, "torn')          # crash mid-write, no newline
    ev = led.append("after", {"ok": True})   # must not raise
    assert ev["seq"] == 3
    assert ev["prev_hash"] == _rows_ok(led)[2]["hash"]
    d = led.verify_detail()                  # the torn line is still reported
    assert not d["ok"] and d["reason"] == "unreadable_line"


def _rows_ok(led):
    out = []
    with open(led.path) as f:
        for l in f:
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
    return out


def test_head_cache_rereads_after_external_change(tmp_path):
    """Another process (or a second Ledger) appending must not fork the chain."""
    path = str(tmp_path / "ledger.jsonl")
    a, b = Ledger(path), Ledger(path)
    a.append("x", {})
    b.append("x", {})
    a.append("x", {})
    with open(path, "a") as f:               # out-of-band writer, bypasses the cache
        rows = _rows(a)
        ts = 5.0
        ev = {"seq": 3, "ts": ts, "iso": "x", "actor": "o", "query_id": None, "type": "x",
              "payload": {}, "prev_hash": rows[-1]["hash"], "alg": ledger_mod.ALG_PLAIN}
        ev["hash"] = ledger_mod.event_hash(ledger_mod.ALG_PLAIN, ev["prev_hash"], 3, ts,
                                           "o", "x", {}, None)
        f.write(json.dumps(ev) + "\n")
    a.append("x", {})
    assert [r["seq"] for r in _rows(a)] == [0, 1, 2, 3, 4]
    ok, msg = a.verify()
    assert ok, msg
    assert a.head()["seq"] == 4
