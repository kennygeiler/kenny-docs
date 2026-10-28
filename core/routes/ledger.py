"""Ledger: events, export, history, approvals, answer replay and the tamper demo.

DEMO_TICKETS.md F1/F2/F3. Nothing here writes to the ledger or the rule file.
"""
from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import approvals, audit, tamper_demo
from ..services.rulestore import _raw_ratified
from ..services.state import _case

router = APIRouter()


@router.get("/admin/ledger")
def admin_ledger():
    led = _case().ledger()
    d = led.verify_detail()
    events = list(led.read()) if d["reason"] != "unreadable_line" else []
    return {"verified": d["ok"], "verify_message": d["message"], "events": events,
            "count": len(events), "head": led.head() if events else None,
            "keyed": d["keyed"], "failed_seq": d["failed_seq"], "reason": d["reason"]}


@router.get("/admin/ledger/export")
def admin_ledger_export():
    return PlainTextResponse(_case().ledger().export(), media_type="text/plain")


@router.get("/admin/history")
def admin_history():
    return {"history": audit.history(_case().ledger())}

_QID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


@router.get("/chat/replay/{query_id}")
def chat_replay(query_id: str):
    """Recompute a costing answer from its snapshot and compare hashes. Read-only; the
    snapshot file name is taken from the ledger event, never from the URL."""
    if not _QID_RE.match(query_id):
        return JSONResponse({"status": "not_replayable", "query_id": query_id,
                             "reason": "malformed query id"}, status_code=400)
    case = _case()
    return audit.replay(case, case.ledger(), query_id)


@router.get("/admin/approvals")
def admin_approvals():
    """Per live rule: the newest full approval event and whether the live text still
    matches the fingerprint that was approved."""
    case = _case()
    return {"approvals": approvals.report(case, case.ledger(), _raw_ratified(case))}


@router.get("/admin/ledger/tamper-demo")
def admin_ledger_tamper_demo(mode: str = "edit", seq: int | None = None,
                             field: str | None = None, value: str | None = None,
                             preset: str | None = None):
    """Tamper with one event in a COPY of the ledger and report which entry fails and
    why. GET because nothing durable changes: the copy is deleted before returning."""
    led = _case().ledger()
    try:
        return tamper_demo.run(led.path, mode=mode, seq=seq, field=field, value=value,
                               preset=preset)
    except tamper_demo.TamperDemoError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
