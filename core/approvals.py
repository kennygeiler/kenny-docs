"""Approval provenance: who approved which rule text, against which clause and which
known answer, when (DEMO_TICKETS.md F2).

An `authoring.ratify` event used to be {rule_id, approver}. It now carries the rule's
fingerprint and text, its citation, the source PDF hash and the known answers it
reproduced, so the question "what exactly did the human approve?" is answered by the
ledger, not by the mutable rule file. `backfill()` writes the same event for the live
rules that predate this record — labelled back-filled, with the basis stated, never
passed off as contemporaneous.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from . import provenance

APPROVER_SOURCE = "self-declared"   # no login exists; say so in the record
BACKFILL_BASIS = ("approver and time copied from rules_ratified.json; rule text, clause "
                  "and known answers are as they stood at back-fill time, not captured at "
                  "the moment of approval")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _source_sha256(case, doc_id: str) -> str | None:
    """The catalog's recorded PDF hash, or None (the shipped corpus records none)."""
    try:
        from .catalog import Catalog
        entry = Catalog(case.path("catalog", "catalog.json")).get(doc_id) or {}
        return entry.get("pdf_sha256") or None
    except Exception:
        return None


def known_answers_for(rule_id: str, checks: list[tuple[int | None, dict]]) -> list[dict]:
    """The golden checks whose `fired` list names this rule. `checks` pairs each
    golden_check ledger seq (None when not ledgered) with its detail dict."""
    out = []
    for seq, detail in checks:
        if rule_id in (detail.get("fired") or []):
            out.append({"scenario": detail.get("scenario"),
                        "expected": detail.get("expected"),
                        "actual": detail.get("actual"),
                        "status": detail.get("status"),
                        "golden_check_seq": seq})
    return out


def approval_payload(case, rule: dict, approver: str, approved_at: str,
                     checks: list[tuple[int | None, dict]], *,
                     backfilled: bool = False, original_event: dict | None = None,
                     checked_at: str | None = None) -> dict:
    cit = rule.get("citation") or {}
    kas = known_answers_for(rule.get("id"), checks)
    payload = {
        "rule_id": rule.get("id"),
        "approver": approver,
        "approver_source": APPROVER_SOURCE,
        "approved_at": approved_at,
        "rule_sha256": provenance.rule_fingerprint(rule),
        "rule": {k: rule.get(k) for k in ("kind", "role", "result_type", "pay_basis",
                                          "when", "compute", "set", "human_readable")},
        "citation": {k: cit.get(k) for k in ("doc_id", "clause", "page", "bbox")},
        "source_sha256": _source_sha256(case, cit.get("doc_id", "")),
        "known_answers": kas,
        "exercised": bool(kas),
        "backfilled": backfilled,
    }
    if backfilled:
        payload.update({"backfilled_at": _utc_now(), "checked_at": checked_at or _utc_now(),
                        "original_event": original_event, "basis": BACKFILL_BASIS})
    return payload


def record_approvals(case, led, rules: list[dict], approver: str,
                     checks: list[tuple[int | None, dict]], *,
                     extra_by_rule: dict[str, dict] | None = None) -> list[dict]:
    """Append one full `authoring.ratify` event per rule, then stamp each rule dict with
    approval:{seq, hash} so the library entry points back at its own record. Mutates
    the dicts in place (the caller writes them). `extra_by_rule` adds caller fields to a
    rule's payload (the gate's proved_by)."""
    events = []
    for r in rules:
        payload = approval_payload(case, r, approver, r.get("approved_at") or _utc_now(),
                                   checks)
        payload.update((extra_by_rule or {}).get(r.get("id"), {}))
        ev = led.append("authoring.ratify", payload, actor="admin")
        r["approval"] = {"seq": ev["seq"], "hash": ev["hash"]}
        events.append(ev)
    return events


# --------------------------------------------------------------------------- #
# read side
# --------------------------------------------------------------------------- #
def latest_approvals(led) -> dict[str, dict]:
    """rule_id -> the newest authoring.ratify event that carries a rule_sha256."""
    out: dict[str, dict] = {}
    for ev in led.read():
        if ev.get("type") != "authoring.ratify":
            continue
        pl = ev.get("payload") or {}
        if pl.get("rule_sha256") and pl.get("rule_id"):
            out[pl["rule_id"]] = ev
    return out


def report(case, led, raw_rules: list[dict]) -> list[dict]:
    """Per live rule: the latest full approval event (or None) and whether the live rule
    text still matches the fingerprint that was approved."""
    latest = latest_approvals(led)
    rows = []
    for r in raw_rules:
        if r.get("status") != "ratified":
            continue
        ev = latest.get(r.get("id"))
        live_fp = provenance.rule_fingerprint(r)
        pl = (ev or {}).get("payload") or {}
        rows.append({
            "rule_id": r.get("id"),
            "live_sha256": live_fp,
            "approval": ({"seq": ev["seq"], "hash": ev["hash"], "iso": ev.get("iso"),
                          "ts": ev.get("ts"), **pl} if ev else None),
            "matches_live": bool(ev) and pl.get("rule_sha256") == live_fp,
        })
    return rows


# --------------------------------------------------------------------------- #
# back-fill
# --------------------------------------------------------------------------- #
def _thin_original(led, rule_id: str) -> dict | None:
    """A pre-F2 authoring.ratify ({rule_id, approver} only) for this rule, if the same
    ledger holds one: the back-fill links to it instead of pretending it never existed."""
    found = None
    for ev in led.read():
        pl = ev.get("payload") or {}
        if ev.get("type") == "authoring.ratify" and pl.get("rule_id") == rule_id \
                and not pl.get("rule_sha256"):
            found = {"seq": ev["seq"], "iso": ev.get("iso"), "hash": ev["hash"]}
    return found


def backfill(case, dry_run: bool = False) -> dict:
    """Append a back-filled `authoring.ratify` for every live rule whose current
    fingerprint has no full approval event yet. Idempotent: a second run appends
    nothing. Never touches the rule file."""
    from .app import _check_golden  # local: core.app imports this module

    rules_path = case.path("rules")
    if not rules_path or not os.path.exists(rules_path):
        return {"appended": [], "skipped": [], "reason": "no rule library"}
    with open(rules_path) as f:
        raw = json.load(f).get("rules", [])
    led = case.ledger()
    have = latest_approvals(led)
    live_dicts = [r for r in raw if r.get("status") == "ratified"]
    checked_at = _utc_now()
    checks = [(None, _check_golden(case, live_dicts, g)[1]) for g in case.golden_cases()]

    appended, skipped = [], []
    for r in live_dicts:
        fp = provenance.rule_fingerprint(r)
        ev = have.get(r.get("id"))
        if ev and (ev.get("payload") or {}).get("rule_sha256") == fp:
            skipped.append(r.get("id"))
            continue
        payload = approval_payload(case, r, r.get("approver") or "unknown",
                                   r.get("approved_at") or "unknown", checks,
                                   backfilled=True,
                                   original_event=_thin_original(led, r.get("id")),
                                   checked_at=checked_at)
        if not dry_run:
            led.append("authoring.ratify", payload, actor="system")
        appended.append(r.get("id"))
    return {"appended": appended, "skipped": skipped, "dry_run": dry_run}
