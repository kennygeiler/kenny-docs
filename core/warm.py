"""Startup warm-up of the retrieval stack (DEMO_TICKETS.md A5).

The embedding model used to load lazily inside the first policy question after a
restart (4 s on a quiet laptop, up to a minute under load — long enough for the demo
tour's policy step to time out), and its loader talked to the Hugging Face hub. A
daemon thread started from the app's lifespan now loads the model, builds the one
shared search backend and reads the catalog before anyone asks. State is reported on
/healthz and /api/case so the header can say "warming up" until it is ready.

Skipped when KENNY_WARM=0 (tests that never need retrieval, constrained containers).
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

_STATE: dict = {"state": "cold", "seconds": None, "error": None}
_LOCK = threading.Lock()
_FACTORIES: dict = {}
_THREAD: threading.Thread | None = None


def configure(**factories) -> None:
    """Register the zero-argument callables the warm thread uses: `case`, `backend`
    (case -> backend), `catalog` (case -> catalog). Registered by the app module so this
    module never imports it (no import cycle)."""
    _FACTORIES.update(factories)


def state() -> dict:
    with _LOCK:
        return dict(_STATE)


def _set(**fields) -> None:
    with _LOCK:
        _STATE.update(fields)


def enabled() -> bool:
    return os.environ.get("KENNY_WARM", "1").strip().lower() not in ("0", "false", "no", "")


def _warm() -> None:
    from . import index
    t0 = time.time()
    try:
        case = _FACTORIES["case"]()
        if index.embeddings_available():
            index.embedder()
        backend = _FACTORIES["backend"](case)
        backend.search("overtime", k=1)
        _FACTORIES["catalog"](case)
        _set(state="ready", seconds=round(time.time() - t0, 2), error=None)
        log.info("retrieval warm in %.2fs", time.time() - t0)
    except Exception as e:  # a failed warm-up must never take the app down
        _set(state="failed", seconds=round(time.time() - t0, 2),
             error=f"{type(e).__name__}: {e}")
        log.exception("retrieval warm-up failed")


def start() -> bool:
    """Start the warm thread once. Returns whether a thread was started."""
    global _THREAD
    if not enabled():
        _set(state="skipped", seconds=None, error=None)
        return False
    if not _FACTORIES:
        _set(state="failed", seconds=None, error="warm.configure() was never called")
        return False
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STATE.update(state="warming", seconds=None, error=None)
        _THREAD = threading.Thread(target=_warm, name="kenny-warm", daemon=True)
        _THREAD.start()
    return True


def reset() -> None:
    """Test hook: forget a previous warm so the next start() runs again."""
    global _THREAD
    with _LOCK:
        _THREAD = None
        _STATE.update(state="cold", seconds=None, error=None)


def wait(timeout: float = 30.0) -> dict:
    """Block until the warm thread finishes (tests, scripts)."""
    t = _THREAD
    if t is not None:
        t.join(timeout)
    return state()


@contextlib.asynccontextmanager
async def lifespan(_app):
    start()
    yield
