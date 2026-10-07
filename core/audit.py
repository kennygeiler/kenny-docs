"""Audit assembly + snapshots (PRD 5.6).

`snapshot()` freezes EVERY input an answer used — the subject records (the $53.40 rate),
the hours, the full rule text, and hashes of the roster, rule file, source PDFs and
engine — so the number can be recomputed later, not merely re-read (DEMO_TICKETS.md F1).
`replay()` does that recomputation and reports match / mismatch with hashes. `trail()`
reconstructs a query's event chain from the ledger for the chat drill-down and the admin
ledger view; `history()` gives one row per question for the Audit tab.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from typing import Any

from . import provenance
from .ruledsl import Rule

SNAPSHOT_SCHEMA = 2
# A snapshot name comes from a ledger EVENT, never from a URL, but it is still checked:
# a bare file name, no separators, so nothing outside the snapshots folder can be read.
_SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}(-[0-9a-f]{6})?\.json$")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _open_write_once(snapshots_dir: str, query_id: str):
    """Open `<qid>.json` for exclusive creation; if it already exists, create
    `<qid>-<6 hex>.json` instead. A frozen answer is never rewritten (F1 step 2)."""
    path = os.path.join(snapshots_dir, f"{query_id}.json")
    try:
        return open(path, "x"), path
    except FileExistsError:
        while True:
            path = os.path.join(snapshots_dir, f"{query_id}-{secrets.token_hex(3)}.json")
            try:
                return open(path, "x"), path
            except FileExistsError:
                continue


def data_sha256(case) -> str | None:
    """SHA-256 of the case's subject data file (the roster), or None."""
    data_cfg = case.manifest.get("data", {}) or {}
    data_path = data_cfg.get("path")
    if data_path and not os.path.isabs(data_path):
        data_path = os.path.join(case.dir, data_path)
    return provenance.file_sha256(data_path)


def case_inputs(case, rules: list[Rule]) -> dict:
    """Hashes of everything outside the request that the answer depended on."""
    data_cfg = case.manifest.get("data", {}) or {}
    sources = {}
    for r in rules:
        doc_id = r.citation.doc_id
        if not doc_id or doc_id in sources:
            continue
        src = case.source_by_id(doc_id)
        pdf = None
        if src and src.get("file"):
            pdf = src["file"] if os.path.isabs(src["file"]) else os.path.join(case.dir, src["file"])
        sources[doc_id] = provenance.file_sha256(pdf) if pdf else None
    return {
        "data": {"adapter": data_cfg.get("adapter"), "path": data_cfg.get("path"),
                 "sha256": data_sha256(case)},
        "rules_file_sha256": provenance.file_sha256(case.path("rules")),
        "sources": sources,
        "engine_sha256": provenance.engine_sha256(),
    }


def snapshot(snapshots_dir: str, query_id: str, params: dict,
             rules: list[Rule], result: dict, *,
             subjects: list[dict] | None = None, rounding_places: int = 2,
             basis_scope=None, inputs: dict | None = None) -> str:
    """Freeze an answer. With `subjects` given this writes schema 2 (replayable);
    without, the legacy schema-1 shape (rule versions + result only).

    Returns the path written. Write-once: an existing file is never rewritten."""
    os.makedirs(snapshots_dir, exist_ok=True)
    if subjects is None:
        payload = {
            "schema": 1,
            "query_id": query_id,
            "frozen_at": _utc_now(),
            "params": params,
            "result": result,
            "rule_versions": [
                {"id": r.id, "kind": r.kind, "priority": r.priority,
                 "when": r.when, "compute": r.compute, "set": r.set,
                 "citation": r.citation.to_dict()}
                for r in rules
            ],
        }
    else:
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "query_id": query_id,
            "frozen_at": _utc_now(),
            "params": params,
            "subjects": subjects,
            "rounding_places": rounding_places,
            "basis_scope": sorted(basis_scope) if basis_scope is not None else None,
            "rules": [{**provenance.rule_full_dict(r), "sha256": provenance.rule_fingerprint(r)}
                      for r in rules],
            "inputs": inputs or {},
            "result": result,
            "result_sha256": provenance.sha256_json(result),
        }
    f, path = _open_write_once(snapshots_dir, query_id)
    with f:
        json.dump(payload, f, indent=2)
    return path


def record_answer(case, led, query_id: str, params: dict, subjects: list[dict],
                  rules: list[Rule], result, *, basis_scope=None,
                  extra: dict | None = None) -> str:
    """Snapshot (schema 2) + the `answer.snapshot` ledger event that binds the file to
    the chain: the event carries the file's sha256, the result's sha256, the engine hash
    and every rule's fingerprint. Returns the snapshot path."""
    result_dict = result.to_dict() if hasattr(result, "to_dict") else result
    snap = snapshot(case.path("snapshots", "snapshots"), query_id, params, rules,
                    result_dict, subjects=subjects, rounding_places=case.rounding_places(),
                    basis_scope=basis_scope, inputs=case_inputs(case, rules))
    items = result_dict.get("line_items") or []
    result_type = items[0].get("result_type") if items else None
    payload = {
        "total": result_dict.get("total"),
        "result_type": result_type,
        "snapshot": os.path.basename(snap),
        "snapshot_sha256": provenance.file_sha256(snap),
        "result_sha256": provenance.sha256_json(result_dict),
        "engine_sha256": provenance.engine_sha256(),
        "rules": [{"id": r.id, "sha256": provenance.rule_fingerprint(r)} for r in rules],
        "schema": SNAPSHOT_SCHEMA,
    }
    if extra:
        payload.update(extra)
    led.append("answer.snapshot", payload, actor="engine", query_id=query_id)
    return snap


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def _not_replayable(query_id: str, reason: str, **more) -> dict:
    return {"status": "not_replayable", "query_id": query_id, "reason": reason,
            "checks": [], "replayed_at": _utc_now(), **more}


def replay(case, ledger, query_id: str) -> dict:
    """Recompute an answer from its frozen snapshot and compare with what the ledger
    recorded. Read-only: appends nothing, writes nothing.

    Returns {status: match|mismatch|not_replayable, recorded, recomputed, checks,
    inputs, drift, frozen_at, replayed_at}.
    """
    from .engine import calculate  # local import: audit must stay importable by app

    events = [e for e in ledger.for_query(query_id) if e.get("type") == "answer.snapshot"]
    if not events:
        return _not_replayable(query_id, "no answer.snapshot event for this question")
    ev = events[-1]
    pl = ev.get("payload") or {}
    name = pl.get("snapshot")
    if not name:
        return _not_replayable(query_id, "the answer was not snapshotted (no file recorded)")
    if os.path.basename(name) != name or not _SNAPSHOT_NAME_RE.match(name):
        return _not_replayable(query_id, "recorded snapshot name is not a bare file name")
    snapshots_dir = os.path.realpath(case.path("snapshots", "snapshots"))
    path = os.path.realpath(os.path.join(snapshots_dir, name))
    if os.path.dirname(path) != snapshots_dir:
        return _not_replayable(query_id, "snapshot path resolves outside the snapshots folder")
    if not os.path.exists(path):
        return _not_replayable(query_id, f"snapshot file {name} is missing")

    checks: list[dict] = []
    file_sha = provenance.file_sha256(path)
    if pl.get("snapshot_sha256"):
        checks.append({"name": "snapshot_sha256",
                       "ok": file_sha == pl["snapshot_sha256"],
                       "recorded": pl["snapshot_sha256"], "actual": file_sha,
                       "detail": "sha256 of the snapshot file vs the hash the ledger recorded"})
    try:
        with open(path) as f:
            snap = json.load(f)
    except (OSError, ValueError) as e:
        return _not_replayable(query_id, f"snapshot file unreadable: {e}", checks=checks)
    if snap.get("schema") != SNAPSHOT_SCHEMA:
        return _not_replayable(query_id, "snapshot predates input capture",
                               snapshot=name, checks=checks,
                               frozen_at=snap.get("frozen_at"))

    recorded = {"total": (snap.get("result") or {}).get("total"),
                "result_sha256": snap.get("result_sha256"),
                "ledger_total": pl.get("total"),
                "ledger_result_sha256": pl.get("result_sha256")}
    recomputed: dict = {}
    try:
        rules = case.assign_scope([Rule.from_dict(d) for d in snap.get("rules", [])])
        scope = snap.get("basis_scope")
        res = calculate(snap.get("params") or {}, snap.get("subjects") or [], rules,
                        int(snap.get("rounding_places", 2)),
                        basis_scope=frozenset(scope) if scope is not None else None)
        rd = res.to_dict()
        recomputed = {"total": rd["total"], "result_sha256": provenance.sha256_json(rd)}
        checks.append({"name": "recompute", "ok": True,
                       "detail": "engine ran on the frozen subjects, params and rules"})
    except Exception as e:  # a rule that no longer evaluates is a mismatch, not a 500
        checks.append({"name": "recompute", "ok": False, "detail": str(e)})

    if recomputed:
        checks.append({"name": "total", "ok": recomputed["total"] == recorded["total"],
                       "recorded": recorded["total"], "actual": recomputed["total"]})
        checks.append({"name": "result_sha256_vs_snapshot",
                       "ok": recomputed["result_sha256"] == recorded["result_sha256"],
                       "recorded": recorded["result_sha256"],
                       "actual": recomputed["result_sha256"]})
        if pl.get("result_sha256"):
            checks.append({"name": "result_sha256_vs_ledger",
                           "ok": recomputed["result_sha256"] == pl["result_sha256"],
                           "recorded": pl["result_sha256"],
                           "actual": recomputed["result_sha256"]})
    ok_chain, msg = ledger.verify()
    checks.append({"name": "ledger_chain", "ok": ok_chain, "detail": msg})

    # Drift: has anything the answer depended on changed SINCE it was frozen? Drift is
    # information, not failure — the old answer still reproduces from its own inputs.
    inputs = snap.get("inputs") or {}
    live = {r.id: r for r in case.rules()}
    rule_drift = []
    for d in snap.get("rules", []):
        rid = d.get("id")
        if rid not in live:
            state = "no longer live"
        elif provenance.rule_fingerprint(live[rid]) == d.get("sha256"):
            state = "unchanged"
        else:
            state = "changed"
        rule_drift.append({"id": rid, "state": state})
    now_inputs = case_inputs(case, rules if recomputed else [])
    data_then = (inputs.get("data") or {}).get("sha256")
    data_now = (now_inputs.get("data") or {}).get("sha256")
    drift = {
        "rules": rule_drift,
        "data": ("unknown" if not data_then else
                 "unchanged" if data_then == data_now else "changed"),
        "engine": ("unknown" if not inputs.get("engine_sha256") else
                   "unchanged" if inputs["engine_sha256"] == provenance.engine_sha256()
                   else "changed"),
    }
    status = "match" if checks and all(c["ok"] for c in checks) else "mismatch"
    return {
        "status": status, "query_id": query_id, "snapshot": name,
        "recorded": recorded, "recomputed": recomputed, "checks": checks,
        "inputs": {"subjects": snap.get("subjects"), "params": snap.get("params"),
                   "rules": [{"id": d.get("id"), "sha256": d.get("sha256")}
                             for d in snap.get("rules", [])],
                   "engine_sha256": inputs.get("engine_sha256"),
                   "data_sha256": data_then},
        "drift": drift,
        "frozen_at": snap.get("frozen_at"), "replayed_at": _utc_now(),
    }


# --------------------------------------------------------------------------- #
# trail + history
# --------------------------------------------------------------------------- #
def trail(ledger, query_id: str) -> list[dict]:
    return ledger.for_query(query_id)


# What happened to a question, derived from the event types in its group. The first
# matching rule wins; `computed` needs a snapshot with a number.
_OUTCOMES = (
    ("costing.blocked", "refused"),
    ("costing.clarify", "asked back"),
    ("policy.clarify", "asked back"),
    ("retrieval.confirm", "asked back"),
    ("policy.answer", "quoted"),
)


def history(ledger) -> list[dict]:
    """One row per query: prompt, total, result type, outcome and timestamp, newest
    first. `ts` is the hashed epoch-seconds field; `iso` is UTC without a zone suffix
    (kept for compatibility — render from `ts`)."""
    prompts: dict[str, dict] = {}
    types: dict[str, set] = {}
    for ev in ledger.read():
        qid = ev.get("query_id")
        if not qid:
            continue
        row = prompts.setdefault(qid, {"query_id": qid, "prompt": None, "total": None,
                                       "result_type": None, "outcome": None,
                                       "events": 0, "ts": ev.get("ts"),
                                       "iso": ev.get("iso")})
        row["events"] += 1
        types.setdefault(qid, set()).add(ev.get("type"))
        if ev["type"] == "chat.prompt":
            row["prompt"] = ev["payload"].get("text")
            row["iso"] = ev.get("iso")
            row["ts"] = ev.get("ts")
        if ev["type"] == "answer.snapshot":
            row["total"] = ev["payload"].get("total")
            row["result_type"] = ev["payload"].get("result_type") or "currency"
    for qid, row in prompts.items():
        seen = types.get(qid, set())
        if row["total"] is not None:
            row["outcome"] = "computed"
        else:
            for t, word in _OUTCOMES:
                if t in seen:
                    row["outcome"] = word
                    break
            else:
                row["outcome"] = "quoted" if "entitlement.fallback" in seen else "no answer"
    rows = list(prompts.values())
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows
