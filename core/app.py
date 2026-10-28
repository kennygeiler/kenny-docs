"""FastAPI application — two surfaces over one pipeline (PRD 5.7).

  Chat  (/)       end-user: prompt -> route -> parse -> deterministic engine -> cited answer
  Admin (/admin)  ops: ingest, catalog/storage, rule-review gate, taxonomy, ledger, history

Every step appends to the hash-chained ledger. The LLM only routes/translates; the
engine does all math.

This module only assembles the app (`core.app:app` is the entrypoint). Routes live in
core/routes/ (pages, chat, documents, rules, ledger); shared state and helpers in
core/services/ (state, sources, rulestore, gate, answers).
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import auth, warm
from .routes import chat, documents, ledger, pages, rules
from .services.state import _backend, _case, _catalog

app = FastAPI(title="Kenny", lifespan=warm.lifespan)  # A5: warm retrieval at startup
# No-op locally; required on any shared deploy (see core/auth.py). The factory lets the
# middleware ledger auth events (failed logins, role denials, CSRF blocks) without a
# circular import.
auth.install(app, ledger_factory=lambda: _case().ledger())

# The warm-up thread (core/warm.py) runs from the app lifespan and needs the same
# case/backend/catalog factories the request path uses, registered here so the warm
# module never imports the app.
warm.configure(case=_case, backend=_backend, catalog=_catalog)

for _module in (pages, chat, documents, rules, ledger):
    app.include_router(_module.router)


@app.exception_handler(Exception)
async def _any_exception(request: Request, exc: Exception):
    """No turn ends without a terminal event and the page always receives JSON (A4 step
    6 / G2 step 4). The event carries type and message only, never a traceback."""
    try:
        _case().ledger().append("chat.error",
                                {"type": type(exc).__name__, "message": str(exc)[:500],
                                 "path": request.url.path}, actor="system")
    except Exception:
        pass
    return JSONResponse({"mode": "blocked",
                         "message": "Something failed while answering; nothing was "
                                    "computed. The failure is recorded in the audit "
                                    "trail."}, status_code=500)

