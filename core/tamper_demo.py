"""Break the chain — on a COPY (DEMO_TICKETS.md F3).

The tamper-evidence claim is only worth something if it can be shown. This module copies
the real ledger to a scratch directory, alters exactly one thing in the copy, runs the
same verification the app runs, and reports which entry fails and why. The real file is
never opened for writing; its byte prefix is re-hashed afterwards to prove it.

Modes:
  edit      change one field of event N (default: the latest answer.snapshot total + 300)
  delete    drop event N  -> verify reports a seq gap at N+1
  rechain   edit event N, then recompute every hash after it with plain SHA-256 — the
            smarter attack. An unsigned chain PASSES verify(); the recorded head
            (verify_anchor) is what catches it. With KENNY_LEDGER_KEY set the rechained
            events are unkeyed and verify() refuses them.
  truncate  drop the last 5 events -> verify passes, the anchored head is missing
"""
from __future__ import annotations

import copy as _copy
import hashlib
import json
import os
import shutil
import tempfile
from typing import Any

from .ledger import Ledger, _canonical, _hash

MODES = ("edit", "delete", "rechain", "truncate")
# Only fields that the hash COVERS may be offered: editing query_id, iso or alg is not
# detected today (DEMO_TICKETS.md F9), so the demo must not pretend it is.
ALLOWED_TOP_FIELDS = ("actor", "type")
REFUSED_FIELDS = ("query_id", "iso", "alg", "seq", "prev_hash", "hash", "ts")


class TamperDemoError(ValueError):
    """Bad request: unknown mode, field outside the allow-list, missing seq."""


def _prefix_sha256(path: str, nbytes: int) -> str:
    h = hashlib.sha256()
    remaining = nbytes
    with open(path, "rb") as f:
        while remaining > 0:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _rows(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _write_rows(path: str, rows: list[dict]) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(_canonical(r) + "\n")


def _get_dotted(obj: Any, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_dotted(obj: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _coerce(value: str | None, before: Any) -> Any:
    """A value arrives from a query string; keep the type of what it replaces."""
    if value is None:
        return None
    if isinstance(before, bool):
        return value.lower() in ("1", "true", "yes")
    if isinstance(before, (int, float)):
        try:
            num = float(value)
        except ValueError:
            return value
        return int(num) if isinstance(before, int) and num.is_integer() else num
    return value


def _check_field(field: str) -> None:
    top = field.split(".", 1)[0]
    if top in REFUSED_FIELDS:
        raise TamperDemoError(
            f"field {field!r} is not covered by the event hash today, so editing it is "
            f"not detected (see DEMO_TICKETS.md F9); choose a payload.* field, actor or type")
    if top != "payload" and top not in ALLOWED_TOP_FIELDS:
        raise TamperDemoError(f"field {field!r} is not editable in the demo; use payload.*, "
                              f"actor or type")


def _default_target(rows: list[dict], preset: str | None) -> tuple[int, str, Any]:
    """(seq, field, new value) when the caller names none."""
    if preset == "approver":
        for ev in reversed(rows):
            if ev.get("type") == "authoring.ratify":
                return ev["seq"], "payload.approver", "someone-else"
        raise TamperDemoError("no authoring.ratify event to target")
    for ev in reversed(rows):
        if ev.get("type") == "answer.snapshot" and isinstance(ev.get("payload"), dict) \
                and isinstance(ev["payload"].get("total"), (int, float)):
            return ev["seq"], "payload.total", round(ev["payload"]["total"] + 300, 2)
    # No costing answer yet: alter the newest event's actor.
    last = rows[-1]
    return last["seq"], "actor", "intruder"


def run(real_path: str, mode: str = "edit", seq: int | None = None,
        field: str | None = None, value: str | None = None,
        preset: str | None = None) -> dict:
    """Run one tamper demonstration on a scratch copy of `real_path`."""
    if mode not in MODES:
        raise TamperDemoError(f"mode must be one of {MODES}")
    if not os.path.exists(real_path):
        raise TamperDemoError("the ledger is empty: ask a question first")
    real_path = os.path.realpath(real_path)
    size_before = os.path.getsize(real_path)
    prefix_before = _prefix_sha256(real_path, size_before)
    real_led = Ledger(real_path)
    real_head = real_led.head()

    tmp = tempfile.mkdtemp(prefix="kenny-tamper-")
    try:
        copy_path = os.path.join(tmp, "ledger-copy.jsonl")
        shutil.copyfile(real_path, copy_path)
        assert os.path.realpath(copy_path) != real_path
        rows = _rows(copy_path)
        if not rows:
            raise TamperDemoError("the ledger is empty: ask a question first")
        n_events = len(rows)
        by_seq = {r["seq"]: i for i, r in enumerate(rows)}

        target: dict = {}
        explanation = ""
        if mode in ("edit", "rechain"):
            d_seq = d_field = d_value = None
            if seq is None or field is None:
                d_seq, d_field, d_value = _default_target(rows, preset)
            seq = d_seq if seq is None else seq
            field = d_field if field is None else field
            _check_field(field)
            if seq not in by_seq:
                raise TamperDemoError(f"no event with seq {seq}")
            ev = rows[by_seq[seq]]
            before = _copy.deepcopy(_get_dotted(ev, field))
            if value is not None:
                value_obj = _coerce(value, before)
            elif d_value is not None and seq == d_seq and field == d_field:
                value_obj = d_value
            elif isinstance(before, (int, float)) and not isinstance(before, bool):
                value_obj = round(before + 300, 2)
            else:
                value_obj = f"{before}-altered"
            _set_dotted(ev, field, value_obj)
            target = {"seq": seq, "type": ev.get("type"), "field": field,
                      "before": before, "after": value_obj}
            if mode == "rechain":
                prev = rows[by_seq[seq] - 1]["hash"] if by_seq[seq] > 0 else "0" * 64
                for r in rows[by_seq[seq]:]:
                    r["prev_hash"] = prev
                    r["alg"] = "sha256"           # no key: plain SHA-256 is all they have
                    r["hash"] = _hash(prev, r["seq"], r["ts"], r["actor"], r["type"],
                                      r["payload"], None)
                    prev = r["hash"]
        elif mode == "delete":
            if seq is None:
                seq = rows[len(rows) // 2]["seq"]
            if seq not in by_seq:
                raise TamperDemoError(f"no event with seq {seq}")
            ev = rows.pop(by_seq[seq])
            target = {"seq": seq, "type": ev.get("type"), "field": None,
                      "before": "present", "after": "deleted"}
        elif mode == "truncate":
            drop = min(5, len(rows) - 1)
            cut = rows[len(rows) - drop:]
            rows = rows[:len(rows) - drop]
            target = {"seq": cut[0]["seq"] if cut else None, "type": None, "field": None,
                      "before": f"{n_events} events", "after": f"{len(rows)} events",
                      "dropped": drop}
        _write_rows(copy_path, rows)

        copy_led = Ledger(copy_path)
        detail = copy_led.verify_detail()
        anchor = None
        if mode in ("rechain", "truncate") and real_head:
            a_ok, a_msg = copy_led.verify_anchor(real_head["seq"], real_head["hash"])
            anchor = {"ok": a_ok, "message": a_msg,
                      "recorded_head": {"seq": real_head["seq"], "hash": real_head["hash"]}}

        if mode == "edit":
            explanation = (f"Changed #{seq} {target['type']} {field} {target['before']!r} -> "
                           f"{target['after']!r} in the copy. Verify on the copy: "
                           + ("FAILED at #%s, %s (stored %s..., recomputed %s...)."
                              % (detail["failed_seq"], detail["reason"],
                                 (detail["stored_hash"] or "")[:8],
                                 (detail["recomputed_hash"] or "")[:8])
                              if not detail["ok"] else "passed (unexpected)."))
        elif mode == "delete":
            explanation = (f"Deleted #{seq} from the copy. Verify on the copy: "
                           + (f"FAILED at #{detail['failed_seq']}, {detail['reason']}: "
                              f"{detail['message']}." if not detail["ok"]
                              else "passed (unexpected)."))
        elif mode == "rechain":
            if detail["ok"]:
                explanation = (f"Changed #{seq} and recomputed every hash after it with plain "
                               f"SHA-256. The copy's chain verifies — an unsigned chain only "
                               f"proves internal consistency. What catches it is the recorded "
                               f"head: anchor check "
                               + ("FAILED: " + (anchor or {}).get("message", "")
                                  if anchor and not anchor["ok"] else "passed (unexpected)")
                               + ". With KENNY_LEDGER_KEY set, rechained events are unkeyed "
                                 "and verify() refuses them outright.")
            else:
                explanation = (f"Changed #{seq} and rechained without the key. Verify on the "
                               f"copy: FAILED at #{detail['failed_seq']}, {detail['reason']} — "
                               f"the key is set, so a plain-SHA-256 rewrite is refused.")
        elif mode == "truncate":
            explanation = (f"Dropped the last {target['dropped']} events from the copy. Verify "
                           f"on the copy: {'passed' if detail['ok'] else 'FAILED'} — a cut "
                           f"tail is a valid prefix. The recorded head catches it: anchor check "
                           + ("FAILED: " + (anchor or {}).get("message", "")
                              if anchor and not anchor["ok"] else "passed (unexpected)") + ".")

        copy_info = {"events": len(rows), **detail, "anchor": anchor}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The real file: same prefix bytes as before (a chat append during the demo only
    # ADDS bytes, so a prefix comparison cannot false-alarm).
    size_after = os.path.getsize(real_path)
    untouched = size_after >= size_before and _prefix_sha256(real_path, size_before) == prefix_before
    real_detail = real_led.verify_detail()
    return {
        "mode": mode,
        "target": target,
        "copy": copy_info,
        "real": {"untouched": untouched, "prefix_sha256": prefix_before,
                 "count": real_detail["count"], "verified": real_detail["ok"],
                 "message": real_detail["message"]},
        "explanation": explanation + (" Real ledger: untouched, still intact."
                                      if untouched and real_detail["ok"]
                                      else " Real ledger: CHECK IT."),
    }
