"""Process-wide state shared by every route: the active case, template dir, job registry.

Exactly one instance of each: CASE_DIR (tests patch it here), the ingest/upload job
registry and its lock, the upload cap. `_case()` reads CASE_DIR on every call.
"""
from __future__ import annotations

import os
import threading
import uuid

import yaml

from .. import index
from ..caseio import default_case_dir, load_case
from ..catalog import Catalog
from ..envfile import _load_dotenv
from ..retriever import CatalogLLMRetriever

# Load the repo's .env before anything below reads the environment (CASE, upload cap).
_load_dotenv()

CASE_DIR = default_case_dir()
TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates")
DEFAULT_YEAR = 2026  # assumed when a prompt gives a date without a year

# In-process job registry for async ingestion (large PDFs). Single-worker; a
# multi-worker deploy swaps this for a shared queue (PRD §8B).
_JOBS: dict[str, dict] = {}
# Guards check-then-register: bulk ingest (sync, threadpool) and upload (async, event
# loop) both create jobs, so the single-flight test must be atomic across them.
_JOBS_LOCK = threading.Lock()


def _register_job(**fields) -> str | None:
    """Register a new running job; None when one is already running.

    Single-flight across BOTH bulk ingest and uploads (TICKETS.md C5): each job is a
    thread plus docling doing minutes of torch work — two concurrent jobs double
    memory on a 2GB instance and race on the same catalog/index files. Completed jobs
    are pruned so the registry cannot grow without bound."""
    with _JOBS_LOCK:
        if any(j.get("status") == "running" for j in _JOBS.values()):
            return None
        while len(_JOBS) > 20:
            _JOBS.pop(next(iter(_JOBS)))
        job_id = uuid.uuid4().hex[:12]
        _JOBS[job_id] = {"status": "running", "result": None, "error": None, **fields}
        return job_id

_MAX_UPLOAD_BYTES = int(os.environ.get("KENNY_MAX_UPLOAD_MB", "50")) << 20


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _case():
    return load_case(CASE_DIR)


def _catalog(case) -> Catalog:
    return Catalog(case.path("catalog", "catalog.json"))


def _backend(case):
    return index.make_backend(case)


def _taxonomy(case) -> dict:
    p = case.path("taxonomy")
    if p and os.path.exists(p):
        with open(p) as f:
            return yaml.safe_load(f) or {}
    return {}


def _extraction(case) -> dict:
    p = case.path("extraction")
    if p and os.path.exists(p):
        with open(p) as f:
            return yaml.safe_load(f) or {}
    return {}


def _retriever() -> CatalogLLMRetriever:
    return CatalogLLMRetriever()


_NOCACHE = {"Cache-Control": "no-store, max-age=0"}
