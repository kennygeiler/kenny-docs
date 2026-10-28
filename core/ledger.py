"""Hash-chained, append-only audit ledger (PRD 5.6).

Every action in the system — a chat prompt, a retrieval decision, an intent parse,
a data read, a rule evaluation, a math step, a citation, an authoring approval —
appends one event here. Each event stores the hash of the prior event, so any edit
or deletion breaks the chain and is caught by `verify()`. This is the record that
must survive union/council/legal scrutiny.

TAMPER MODEL. A plain hash chain only proves internal consistency: an attacker with
disk access can rewrite event N and recompute every hash after it, or truncate the
tail, and the chain still verifies. Two mechanisms close that gap:

  1. HMAC. With KENNY_LEDGER_KEY set (a secret that must NOT live on the same volume
     as the ledger — it belongs in the platform's secret store), every event hash is
     HMAC-SHA256 under that key. Rewriting the chain now requires the key, and
     `verify()` refuses any event that was not keyed.
  2. Head anchoring. When keyed, every append emits `KENNY_LEDGER_HEAD seq=N hash=…`
     to stdout, so the platform's log retention holds an external record of the head.
     `verify_anchor(seq, hash)` checks a recorded head against the current file —
     a truncated tail no longer contains the anchored event and fails.

Without the key the ledger degrades to the old integrity-only chain (local dev).

Storage is append-only JSONL (one event per line). The scale roadmap (PRD §8)
swaps this for SQLite/Postgres behind the same append()/read()/verify() surface.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import threading
import time
from typing import Any, Iterable

try:
    import fcntl  # POSIX advisory locking
except Exception:  # pragma: no cover - non-POSIX
    fcntl = None

GENESIS = "0" * 64
KEY_ENV = "KENNY_LEDGER_KEY"


def _key() -> bytes | None:
    """The HMAC key, read per call so tests and rotation see the live environment."""
    val = os.environ.get(KEY_ENV, "").strip()
    return val.encode("utf-8") if val else None

# A hash chain is read-then-append: read the last hash, link to it, write. Two writers
# interleaving produce duplicate seq numbers and a broken chain — i.e. two people asking
# a question at the same moment destroys the audit record. Serialise writes with an
# in-process lock (threads: FastAPI runs sync endpoints in a threadpool) AND an advisory
# file lock (processes: more than one uvicorn worker).
_APPEND_LOCK = threading.Lock()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# Hash algorithms, recorded per event in "alg". v1 ("sha256" / "hmac-sha256") covers
# prev|seq|ts|actor|type|payload and NOT query_id — so an edited query_id (which binds an
# event to the question it answered) went undetected (DEMO_TICKETS.md F9). v2 adds
# query_id AND the alg string itself to the material, so relabelling an event's alg
# changes its hash. New events are always v2; v1 events written before this change keep
# verifying under v1, so an existing ledger (hundreds of v1 events) stays valid and simply
# continues in v2. A v1 event AFTER a v2 event is refused: that is a downgrade.
ALG_PLAIN_V1, ALG_HMAC_V1 = "sha256", "hmac-sha256"
ALG_PLAIN, ALG_HMAC = "sha256-v2", "hmac-sha256-v2"
_V2 = {ALG_PLAIN, ALG_HMAC}
_KEYED = {ALG_HMAC_V1, ALG_HMAC}
_KNOWN = {ALG_PLAIN_V1, ALG_HMAC_V1, ALG_PLAIN, ALG_HMAC}


def _hash(prev_hash: str, seq: int, ts: float, actor: str, type_: str, payload: Any,
          key: bytes | None = None) -> str:
    """v1 material (kept for verifying old events and for tamper_demo's attacker)."""
    material = f"{prev_hash}|{seq}|{ts}|{actor}|{type_}|{_canonical(payload)}"
    return _digest(material, key)


def _digest(material: str, key: bytes | None) -> str:
    if key is not None:
        return _hmac.new(key, material.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def event_hash(alg: str, prev_hash: str, seq: int, ts: float, actor: str, type_: str,
               payload: Any, query_id: Any, key: bytes | None = None) -> str:
    """Hash an event under its recorded alg. Keyed algs use `key`; plain algs ignore it."""
    k = key if alg in _KEYED else None
    if alg in _V2:
        material = (f"{alg}|{prev_hash}|{seq}|{ts}|{actor}|{_canonical(query_id)}|"
                    f"{type_}|{_canonical(payload)}")
        return _digest(material, k)
    return _hash(prev_hash, seq, ts, actor, type_, payload, k)


# Head cache: path -> (st_dev, st_ino, st_size, st_mtime_ns, seq, hash). append() used to re-read and
# parse the WHOLE ledger on every event; now it trusts the cached head while the file is
# the same inode at exactly the size and mtime we left it, and otherwise tail-reads the last line.
_HEAD_CACHE: dict[str, tuple] = {}


def _tail_head(f) -> tuple[int, str] | None:
    """(seq, hash) of the last PARSEABLE event, reading backwards from the end.

    A torn/malformed final line (crash mid-write, stray bytes) is skipped rather than
    raised: one bad line must not make every later append fail. verify() still reports
    it as unreadable_line — nothing is hidden, the ledger just keeps recording."""
    f.seek(0, os.SEEK_END)
    size = f.tell()
    if size == 0:
        return None
    block = 1 << 16
    while True:
        start = max(0, size - block)
        f.seek(start)
        data = f.read(size - start)
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]          # first piece may be a partial line
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
                return int(ev["seq"]), str(ev["hash"])
            except (ValueError, KeyError, TypeError):
                continue
        if start == 0:
            return None
        block *= 4


class Ledger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    # ---- write ----
    def append(self, type_: str, payload: dict, actor: str = "system",
               query_id: str | None = None) -> dict:
        """Append one event, linked to the previous one's hash.

        The whole read-link-write is atomic: concurrent appends would otherwise reuse a
        seq/prev_hash and break the chain — verified by
        tests/test_engine_dsl.py::test_ledger_survives_concurrent_writes.
        """
        key = _key()
        alg = ALG_HMAC if key is not None else ALG_PLAIN
        cache_key = os.path.abspath(self.path)
        with _APPEND_LOCK:                       # serialise threads in this process
            with open(self.path, "ab+") as f:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)   # and other processes
                try:
                    st = os.fstat(f.fileno())
                    cached = _HEAD_CACHE.get(cache_key)
                    if cached and cached[:4] == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns):
                        last = cached[4:]
                    else:                        # first use, or another writer/edit
                        last = _tail_head(f)
                    seq = (last[0] + 1) if last else 0
                    prev_hash = last[1] if last else GENESIS
                    # A torn last line without its newline would swallow this event.
                    if st.st_size:
                        f.seek(-1, os.SEEK_END)
                        lead = b"" if f.read(1) == b"\n" else b"\n"
                    else:
                        lead = b""
                    ts = time.time()
                    event = {
                        "seq": seq,
                        "ts": ts,
                        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)),
                        "actor": actor,
                        "query_id": query_id,
                        "type": type_,
                        "payload": payload,
                        "prev_hash": prev_hash,
                        "alg": alg,
                        "hash": event_hash(alg, prev_hash, seq, ts, actor, type_,
                                           payload, query_id, key),
                    }
                    f.seek(0, os.SEEK_END)
                    f.write(lead + (_canonical(event) + "\n").encode("utf-8"))
                    f.flush()
                    os.fsync(f.fileno())         # the record must survive a crash
                    st = os.fstat(f.fileno())
                    _HEAD_CACHE[cache_key] = (st.st_dev, st.st_ino, st.st_size,
                                              st.st_mtime_ns, seq, event["hash"])
                except BaseException:
                    _HEAD_CACHE.pop(cache_key, None)
                    raise
                finally:
                    if fcntl is not None:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        if key is not None:
            # External anchor: platform log retention holds the head outside the volume,
            # so a truncated file can be caught against the last anchored head.
            print(f"KENNY_LEDGER_HEAD seq={event['seq']} hash={event['hash']}", flush=True)
        return event

    # ---- read ----
    def read(self) -> Iterable[dict]:
        if not os.path.exists(self.path):
            return []
        events = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def for_query(self, query_id: str) -> list[dict]:
        return [e for e in self.read() if e.get("query_id") == query_id]

    # ---- integrity ----
    def verify_detail(self) -> dict:
        """Recompute the whole chain and say exactly where (and why) it fails.

        Returns {ok, message, count, failed_seq, failed_line, reason, event_type,
        stored_hash, recomputed_hash, keyed, head}. `reason` is one of seq_gap |
        prev_hash_mismatch | hash_mismatch | unkeyed_event | unknown_alg | alg_downgrade |
        unreadable_line | None.
        A torn or malformed line is REPORTED (reason unreadable_line), never raised:
        one bad byte must not turn every health check into a 500 (DEMO_TICKETS.md F3).

        With KENNY_LEDGER_KEY set, every event MUST verify under the HMAC — an event
        hashed without the key fails, because accepting unkeyed events would let an
        attacker rewrite the file as a plain (keyless) chain and pass. A ledger written
        before the key was configured therefore fails verification once the key is on:
        archive it (export, then reseed) rather than mixing keyed and unkeyed history.
        """
        key = _key()
        out = {"ok": True, "message": "chain intact", "count": 0, "failed_seq": None,
               "failed_line": None, "reason": None, "event_type": None,
               "stored_hash": None, "recomputed_hash": None,
               "keyed": key is not None, "head": None}

        def fail(reason: str, message: str, ev: dict | None, line_no: int,
                 recomputed: str | None = None) -> dict:
            out.update({"ok": False, "message": message, "reason": reason,
                        "failed_line": line_no,
                        "failed_seq": ev.get("seq") if ev else None,
                        "event_type": ev.get("type") if ev else None,
                        "stored_hash": ev.get("hash") if ev else None,
                        "recomputed_hash": recomputed})
            return out

        if not os.path.exists(self.path):
            return out
        prev_hash = GENESIS
        expected_seq = 0
        seen_v2 = False
        with open(self.path) as f:
            for line_no, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                    seq, prev, ts = ev["seq"], ev["prev_hash"], ev["ts"]
                    actor, type_, payload, stored = (ev["actor"], ev["type"],
                                                     ev["payload"], ev["hash"])
                except (ValueError, KeyError, TypeError) as e:
                    return fail("unreadable_line",
                                f"unreadable ledger line {line_no}: {e}", None, line_no)
                if seq != expected_seq:
                    return fail("seq_gap", f"seq gap at {seq} (expected {expected_seq})",
                                ev, line_no)
                if prev != prev_hash:
                    return fail("prev_hash_mismatch",
                                f"broken chain at seq {seq}: prev_hash mismatch", ev, line_no)
                alg = ev.get("alg") or ALG_PLAIN_V1
                if alg not in _KNOWN:
                    return fail("unknown_alg", f"unknown hash alg {alg!r} at seq {seq}",
                                ev, line_no)
                if key is not None and alg not in _KEYED:
                    return fail("unkeyed_event",
                                f"unkeyed event at seq {seq}: KENNY_LEDGER_KEY is set but "
                                f"this event predates it (or was rewritten without the key). "
                                f"Archive the old ledger or unset the key.", ev, line_no)
                if seen_v2 and alg not in _V2:
                    return fail("alg_downgrade",
                                f"alg downgrade at seq {seq}: {alg} after a v2 event "
                                f"(v1 does not cover query_id)", ev, line_no)
                seen_v2 = seen_v2 or alg in _V2
                recomputed = event_hash(alg, prev_hash, seq, ts, actor, type_, payload,
                                        ev.get("query_id"), key)
                if recomputed != stored:
                    return fail("hash_mismatch", f"tampered event at seq {seq}: hash mismatch",
                                ev, line_no, recomputed)
                prev_hash = stored
                expected_seq += 1
                out["count"] = expected_seq
                out["head"] = {"seq": seq, "hash": stored, "count": expected_seq}
        return out

    def verify(self) -> tuple[bool, str]:
        """Recompute the whole chain: (ok, message). See verify_detail() for the where
        and why; the message strings here are unchanged for callers that match on them."""
        d = self.verify_detail()
        return d["ok"], d["message"]

    def head(self) -> dict | None:
        """The current chain head: {"seq", "hash", "count"}. Record it externally (it is
        also printed on every keyed append) to make truncation detectable later."""
        if not os.path.exists(self.path):
            return None
        with open(self.path, "rb") as f:
            last = _tail_head(f)
        if last is None:
            return None
        return {"seq": last[0], "hash": last[1], "count": last[0] + 1}

    def verify_anchor(self, seq: int, hash_: str) -> tuple[bool, str]:
        """Check a previously-recorded head against the current file.

        The chain must still verify AND the anchored event must still exist with the
        same hash. A tail truncated past the anchor, or a rewrite that changed history
        at or before it, both fail — this is the check `verify()` alone cannot make.
        """
        ok, msg = self.verify()
        if not ok:
            return False, msg
        for ev in self.read():
            if ev["seq"] == seq:
                if ev["hash"] == hash_:
                    return True, f"anchor at seq {seq} intact"
                return False, f"anchor mismatch at seq {seq}: history was rewritten"
        return False, (f"anchored seq {seq} is missing: the ledger was truncated "
                       f"below the recorded head")

    def export(self) -> str:
        """Full ledger plus a header line binding the export to its head. A saved export
        is an anchor: verify_anchor(header.head.seq, header.head.hash) later proves the
        live file still contains everything this export saw."""
        events = list(self.read())
        header = {"export": {
            "count": len(events),
            "head": ({"seq": events[-1]["seq"], "hash": events[-1]["hash"]}
                     if events else None),
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "keyed": _key() is not None,
        }}
        return "\n".join([_canonical(header)] + [_canonical(e) for e in events])
