"""The rule files on disk: proposed queue, ratified library, atomic writes.

_RULES_LOCK is the one lock every read-modify-write of either file holds.
"""
from __future__ import annotations

import os
import threading

from .state import _case


def _proposed_queue():
    """The proposed-rule queue plus the live library's state (GET /admin/proposed)."""
    case = _case()
    path = case.path("proposed_rules", "rules/rules_proposed.json")
    data = {"rules": [], "needs_data": []}
    if path and os.path.exists(path):
        import json
        with open(path) as f:
            data = json.load(f)
    # which proposals are already live, so the gate shows state instead of a
    # permanently-checked box
    ratified = {r.id: {"approver": r.approver, "result_type": r.result_type}
                for r in case.rules()}
    data["ratified_ids"] = sorted(ratified.keys())
    data["ratified_meta"] = ratified
    # Rules whose cited evidence changed on a re-ingest (marked by _revalidate_citations)
    # — excluded from the engine until a human re-verifies and re-approves them.
    data["stale_rules"] = [r for r in _raw_ratified(case) if r.get("status") == "stale"]
    # The FULL ratified library, verbatim from disk. The rule-library UI must render from
    # this — not from the proposed queue filtered to ratified ids. The queue is ephemeral
    # (cleared on reseed, not shipped in the image), so deriving the library display from
    # it showed "4" in the tab count and an empty page beneath it.
    data["ratified_rules"] = _raw_ratified(case)
    return data


def _live_dicts(case) -> list[dict]:
    """The live library exactly as stored (role, pay_basis, flags, human_readable intact),
    limited to what load_rules() would execute — the same filter as core/ruledsl.py, so a
    stale rule stays out.

    This replaced a 10-key re-serialisation that dropped `role` and `pay_basis`: the gate
    re-parsed every live premium as a competing hourly base and every annual term as
    hourly, so a correct annual premium turned the $640.80 known answer red at 1200.0
    while chat (reading the real library) still said 640.80 (DEMO_TICKETS.md E2). The
    gate must judge the library chat runs, not a lossy copy of it."""
    return [r for r in _raw_ratified(case)
            if r.get("status") == "ratified" and r.get("approver")]


def _with_live(case, extra: list[dict]) -> list[dict]:
    """The live library merged with candidate rules, BY ID — never concatenated.

    A rule can be both ratified and still sitting in the proposed queue (approving it
    does not remove it from the queue). Concatenating the two lists put the same
    differential in the candidate set twice, and differentials ACCUMULATE — graveyard
    ×1.055 applied twice, bilingual ×1.05 applied twice — so a re-approval of already-live
    rules "broke" a passing scenario at 4747.05. Merge by id; the candidate (newer draft)
    wins over the stored copy.
    """
    merged = {r["id"]: r for r in _live_dicts(case)}
    for r in extra:
        merged[r["id"]] = r
    return list(merged.values())


# --------------------------------------------------------------------------- #
# One lock for every read-modify-write of rules_proposed.json / rules_ratified.json
# (ratify, draft_scenario, demo_draft load, cell_confirm, citation revalidation). Two
# concurrent writers used to read the same queue/library and the later write silently
# dropped the earlier one's rules. A plain Lock (not RLock): async handlers acquire it
# by polling, so it may be released from a different thread than the one that took it.
_RULES_LOCK = threading.Lock()


class _rules_locked:
    """`async with _rules_locked():` — take _RULES_LOCK without blocking the event loop
    (and without a threadpool acquire that a cancelled request could leave held)."""

    async def __aenter__(self):
        import asyncio
        while not _RULES_LOCK.acquire(blocking=False):
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc):
        _RULES_LOCK.release()
        return False


def _atomic_json_write(path: str, obj) -> None:
    """Temp file in the SAME directory + fsync + os.replace: a crash or a full disk
    mid-write leaves the previous file, never a truncated rule library."""
    import json
    import tempfile
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _write_proposed(case, rules: list[dict], needs_data: list[dict] | None = None) -> None:
    path = case.path("proposed_rules", "rules/rules_proposed.json")
    _atomic_json_write(path, {"rules": rules, "needs_data": needs_data or []})


def _raw_ratified(case) -> list[dict]:
    """The ratified library exactly as stored — full fields (approver, timestamp,
    status), unlike `case.rules()` which parses and drops metadata. Used when merging so
    a prior approval is preserved byte-for-byte, not round-tripped lossily."""
    import json
    path = case.path("rules")
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f).get("rules", [])
    return []


def _write_ratified(case, rules: list[dict]) -> None:
    """Write the ratified library, backing up the previous one first. Ratification
    replaces the live rule set, so the prior version is always recoverable."""
    import shutil
    import time as _t
    path = case.path("rules")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        stamp = _t.strftime("%Y%m%d-%H%M%S")
        shutil.copy(path, f"{path}.{stamp}.bak")
    _atomic_json_write(path, {"rules": rules})


# --- ids-stale-warm ---
# A1 (server-issued ids), A2 (evidence-based stale check), A5 (startup warm-up).
def _cited_rule_count(case, doc_id: str) -> int:
    """How many live rules cite a document — what a re-read re-checks (A2)."""
    return sum(1 for r in _raw_ratified(case)
               if r.get("status") == "ratified"
               and (r.get("citation") or {}).get("doc_id") == doc_id)


# --------------------------------------------------------------------------- #
# --- tour-and-gate-demo --- (DEMO_TICKETS.md I15: the human gate, shown live)
# --------------------------------------------------------------------------- #
# A seeded, deliberately WRONG but plausible draft (cases/santacruz/rules/
# demo_wrong_draft.json): the $640.80 overtime rule at double time, citing the same
# p.8 clause. Loading it into the review queue is idempotent (merged by id); approving
# it goes through the ordinary gate, which refuses it ($854.40 is not $640.80) and
# ledgers authoring.blocked. Nothing here writes rules_ratified.json. No model call.
DEMO_DRAFT_FILE = "rules/demo_wrong_draft.json"


def _demo_drafts(case) -> list[dict]:
    """The seeded demo drafts, verbatim from the tracked file ([] when absent)."""
    import json
    path = os.path.join(case.dir, DEMO_DRAFT_FILE)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [r for r in json.load(f).get("rules", []) if r.get("id")]


def _demo_draft_status(case) -> dict:
    drafts = _demo_drafts(case)
    queued = {r.get("id") for r in _proposed_queue().get("rules", [])}
    live = {r.get("id") for r in _raw_ratified(case)}
    ids = [r["id"] for r in drafts]
    return {"rules": drafts, "ids": ids,
            "loaded": [i for i in ids if i in queued],
            "live": [i for i in ids if i in live],   # must stay empty: the gate's promise
            "file": DEMO_DRAFT_FILE}
