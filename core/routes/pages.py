"""Pages and static assets: /, /admin, /static/*, /healthz, /api/case, /api/coverage."""
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .. import llm, warm
from .. import version as _version  # handoff (K1): the commit this process runs
from ..services import provisions as _provisions
from ..services.sources import _doc_meta
from ..services.state import _NOCACHE, TEMPLATES, _case, _catalog

router = APIRouter()


@router.get("/healthz")
def healthz():
    """Platform liveness probe. Verifies the ledger chain so a corrupted audit trail
    surfaces as an unhealthy instance rather than as a wrong answer months later —
    but this endpoint is UNAUTHENTICATED, so it reports only pass/fail; the tamper
    detail ("event at seq N") is on /admin/ledger, behind the admin credential."""
    ok, _msg = _case().ledger().verify()
    # A1: a rule library or catalog that no longer parses makes every /chat and
    # /admin/* call 500 while a ledger-only probe stays green. Load both here so the
    # platform sees the outage. A5: report the retrieval warm-up (status unchanged).
    library_ok = True
    try:
        case = _case()
        case.rules()
        _catalog(case)
    except Exception:
        library_ok = False
    healthy = ok and library_ok
    return JSONResponse({"status": "ok" if healthy else "degraded",
                         "ledger": "ok" if ok else "broken",
                         "library": "ok" if library_ok else "broken",
                         "retrieval": warm.state()["state"]},
                        status_code=200 if healthy else 503)


@router.get("/", response_class=HTMLResponse)
def chat_page():
    return FileResponse(os.path.join(TEMPLATES, "chat.html"), headers=_NOCACHE)


@router.get("/admin", response_class=HTMLResponse)
def admin_page():
    return FileResponse(os.path.join(TEMPLATES, "admin.html"), headers=_NOCACHE)


@router.get("/static/app.js")
def app_js():
    return FileResponse(os.path.join(TEMPLATES, "app.js"),
                        media_type="application/javascript", headers=_NOCACHE)


@router.get("/static/admin.js")
def admin_js():
    """The admin console's script (was inline in admin.html). Served like app.js."""
    return FileResponse(os.path.join(TEMPLATES, "admin.js"),
                        media_type="application/javascript", headers=_NOCACHE)


@router.get("/static/tour.js")
def tour_js():
    """Guided demo tour: one shared step engine for both surfaces (chat starts it,
    /admin?tour=1 resumes it). Served like app.js so both templates can include it."""
    return FileResponse(os.path.join(TEMPLATES, "tour.js"),
                        media_type="application/javascript", headers=_NOCACHE)


@router.get("/static/styles.css")
def styles_css():
    return FileResponse(os.path.join(TEMPLATES, "styles.css"),
                        media_type="text/css", headers=_NOCACHE)


@router.get("/api/case")
def api_case():
    case = _case()
    return {"name": case.manifest.get("name"), "department": case.manifest.get("department"),
            "has_api_key": llm.have_key(),
            "retrieval": warm.state()["state"],  # A5: header can say "warming up"
            "llm_mode": llm.llm_mode(),          # 'claude' | 'off' | 'no-key' (G9)
            "llm_status": llm.llm_status(),      # 'claude' | 'degraded' | 'none' (A8)
            # Set on the shared deploy. A hosted link gets mistaken for a product; this
            # is a prototype on a synthetic corpus and every viewer must be told so
            # before they read a dollar figure off it.
            "banner": os.environ.get("KENNY_BANNER", ""),
            "version": _version.running_version(),   # handoff (K1): {sha, dirty, source}
            # Declared document titles, so the chat page can name a contract wherever
            # a response only carries its id (I4).
            "sources": [_doc_meta(case, s.get("id")) for s in case.manifest.get("sources", [])
                        if s.get("id")]}


@router.get("/static/tree.js")
def tree_js():
    """The search-tree renderer for the audit drawer (L3): pure treeHtml(tree) plus
    renderTree(tree, el). Served like app.js; chat.html includes it after app.js."""
    return FileResponse(os.path.join(TEMPLATES, "tree.js"),
                        media_type="application/javascript", headers=_NOCACHE)


@router.get("/api/coverage")
def api_coverage():
    """Provision coverage (ROADMAP M1.5): per document, how many provisions answer with a
    number (live), how many are drafted and awaiting approval, how many are not modelled
    and why, plus data errors. `available: false` when the case has no provisions.yaml;
    every surface hides the feature then. Public read, like /api/case."""
    return JSONResponse(_provisions.coverage(), headers=_NOCACHE)
