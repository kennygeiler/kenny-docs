"""FastAPI application — two surfaces over one pipeline (PRD 5.7).

  Chat  (/)       end-user: prompt -> route -> parse -> deterministic engine -> cited answer
  Admin (/admin)  ops: ingest, catalog/storage, rule-review gate, taxonomy, ledger, history

Every step appends to the hash-chained ledger. The LLM only routes/translates; the
engine does all math.
"""
from __future__ import annotations

import os
import re
import threading
import uuid

import yaml
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import audit, auth, cellcheck, governance, index, ingest, llm, queryfacts, refusal, rulematch
from . import evidence, rowbands, warm
from . import qid as qid_mod
from .caseio import default_case_dir, load_case
from .catalog import Catalog
from .engine import NoRuleApplies, calculate
from . import pdfview
from .pdfview import page_dims, render_page_with_bbox
from .retriever import CatalogLLMRetriever
from .ruledsl import SHIFT_BASES, Rule, RuleError, load_rules, validate_rules
from . import costing  # costing-correctness (B1, B2, B4)
from starlette.concurrency import run_in_threadpool

def _load_dotenv(force: bool = False) -> None:
    """Load the repo's .env at startup so the server works with a plain `uvicorn`
    command — no --env-file flag required. Real environment variables always win.
    Skipped under pytest (and when KENNY_NO_DOTENV is set): this runs at import, during
    test collection, which is exactly how a developer's real ANTHROPIC_API_KEY used to
    leak into the suite (DEMO_TICKETS K2; tests/conftest.py is the other half).
    `force` is for tests of the loader itself: it skips that guard, nothing else."""
    import sys
    if not force and (os.environ.get("KENNY_NO_DOTENV") or "pytest" in sys.modules):
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and k not in os.environ:   # an explicitly EMPTY real variable still wins
            os.environ[k] = v


_load_dotenv()

CASE_DIR = default_case_dir()
TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
DEFAULT_YEAR = 2026  # assumed when a prompt gives a date without a year

app = FastAPI(title="Kenny", lifespan=warm.lifespan)  # A5: warm retrieval at startup
# No-op locally; required on any shared deploy (see core/auth.py). The factory lets the
# middleware ledger auth events (failed logins, role denials, CSRF blocks) without a
# circular import.
auth.install(app, ledger_factory=lambda: _case().ledger())

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


# SHA-256 of source PDFs, cached by (mtime, size) so serving doesn't re-hash a multi-MB
# file per request. An attacker who can also forge mtime+size defeats the cache until
# restart — the cache is a hot-path economy, not the security boundary; ingest and the
# stale-rule gate re-hash for real.
_HASH_CACHE: dict[str, tuple[float, int, str]] = {}


def _file_sha256(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    cached = _HASH_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    sha = ingest.sha256_file(path)
    _HASH_CACHE[path] = (st.st_mtime, st.st_size, sha)
    return sha


def _check_source_hash(cat, doc_id: str, pdf_path: str) -> tuple[bool, str, str]:
    """Does the file on disk still match the bytes that were ingested? (TICKETS.md A1)
    Returns (ok, expected, actual). A catalog entry with no recorded hash (pre-hashing
    ingest) passes — there is nothing to verify against until the next re-ingest."""
    entry = cat.get(doc_id) or {}
    expected = entry.get("pdf_sha256", "")
    if not expected:
        return True, "", ""
    actual = _file_sha256(pdf_path)
    return actual == expected, expected, actual


def _doc_integrity(case, cat, doc_ids: list[str], rules) -> list[str]:
    """Every provenance failure that must BLOCK a costing answer:
    a source PDF on disk that no longer matches its ingested hash, or a ratified rule
    whose citation was bound (at draft time) to different bytes than the catalog now
    holds. Returns human-readable problems; empty means the chain is intact."""
    problems: list[str] = []
    for d in doc_ids:
        entry = cat.get(d)
        if not entry:
            continue
        pdf_path = _resolve_pdf(case, d)
        if pdf_path:
            ok, expected, actual = _check_source_hash(cat, d, pdf_path)
            if not ok:
                problems.append(f"{d}: file on disk (sha {actual[:12]}) no longer "
                                f"matches the ingested document (sha {expected[:12]})")
    for r in rules:
        cit = r.citation
        entry = cat.get(cit.doc_id) or {}
        now = entry.get("pdf_sha256", "")
        if cit.doc_sha256 and now and cit.doc_sha256 != now:
            problems.append(f"rule {r.id}: ratified against {cit.doc_id} sha "
                            f"{cit.doc_sha256[:12]}, but the ingested document is now "
                            f"sha {now[:12]}")
    return problems


def _extraction_tier(parse_source: str, kind: str, origin: str | None = None) -> str | None:
    """Human label for HOW a piece of cited text was extracted (OCR-4) — the trust
    signal at the moment of reading an answer. The ONE mapping; app.js only renders
    the `tier` field this stamps, so client and server can never disagree.

      recovered-* kind        -> "recovered layout"  (layout model misread the page;
                                                      text regrouped from span geometry)
      docling + normal/table  -> "OCR'd scan"        when the page's text origin is
                                                      'ocr-layer' (D1: a scan with an
                                                      invisible OCR layer — misreads
                                                      possible, check the page image)
                              -> "text layer"        otherwise (digital text layer,
                                                      exact bboxes)
      raw-text-fallback       -> "page-level"        (page text only; citations open
                                                      the page, not the clause)
      sidecar                 -> "sidecar extract"   (hash-bound sidecar extraction)
      anything else           -> None                (no claim beats a wrong claim)

    `origin` is the page's (or, failing that, the document's) `text_origin` as
    recorded at ingest / by scripts/backfill_text_origin.py; None means unknown,
    which keeps the pre-D1 label rather than inventing a scan.
    """
    if kind in ("recovered-row", "recovered-text"):
        return "recovered layout"
    if parse_source == "docling" and kind in ("text", "table-row"):
        return "OCR'd scan" if origin == "ocr-layer" else "text layer"
    if parse_source == "raw-text-fallback":
        return "page-level"
    if parse_source == "sidecar":
        return "sidecar extract"
    return None


def _page_origin(entry: dict, page) -> str | None:
    """The text origin of one page of a catalogued document (D1): the per-page
    record when the back-fill/ingest wrote one, else the document-level origin,
    else None (a catalog that predates origin capture makes no claim)."""
    if not entry:
        return None
    pages = entry.get("text_origin_pages") or {}
    if page is not None and str(page) in pages:
        return pages[str(page)]
    return entry.get("text_origin") or None


def _enrich_citations(cat, result_dict: dict) -> dict:
    """Fill in each citation's page + bbox from the ingested document (docling) when
    the rule left them blank — so highlights land on the real section even for large
    PDFs whose coordinates aren't known at authoring time. Also stamps every citation
    with its extraction provenance (`parse_source`, `kind`, `tier` — OCR-4), looked up
    from the catalog entry of the cited document."""
    clause_cache: dict[str, list] = {}
    entry_cache: dict[str, dict] = {}
    for li in result_dict.get("line_items", []):
        for c in li.get("citations", []):
            doc_id = c.get("doc_id", "")
            entry = entry_cache.setdefault(doc_id, cat.get(doc_id) or {})
            clauses = clause_cache.setdefault(doc_id, cat.clauses(doc_id))
            kind, kind_found = "text", False
            for cl in clauses:
                if str(cl.get("clause")) != str(c.get("clause")):
                    continue
                if not kind_found:
                    kind = cl.get("kind") or "text"
                    kind_found = True
                if c.get("bbox"):
                    break                       # nothing left to fill
                if cl.get("bbox"):              # first match WITH coordinates wins,
                    c["bbox"] = cl["bbox"]      # exactly as before OCR-4
                    c["page"] = cl.get("page", c.get("page"))
                    break
            c["parse_source"] = entry.get("parse_source", "")
            c["kind"] = kind
            c["text_origin"] = _page_origin(entry, c.get("page"))          # ocr (D1)
            c["tier"] = _extraction_tier(c["parse_source"], kind, c["text_origin"])
            # Declared title, so the proof surfaces can name the contract (I4).
            c["title"] = entry.get("declared_title") or entry.get("title") or doc_id
    return result_dict


def _revalidate_citations(case, cat, doc_ids: list[str], led) -> list[dict]:
    """Re-check every ratified rule citing the (re-)ingested documents (TICKETS.md A3).

    A rule freezes its citation (clause, page, bbox, source sha) at draft time. After a
    re-ingest the evidence may have moved — a revised MOU renumbers a section, replaces
    a page, or is simply a different file. Such a rule is marked `stale` (with the
    reason), which excludes it from the engine via load_rules' ratified-only filter,
    and the change is ledgered. Silently keeping the old coordinates would highlight
    the wrong text under a confident citation — the exact failure this product exists
    to prevent. Rules whose evidence still checks out get their citation's source hash
    backfilled, so pre-hashing approvals become bound going forward."""
    library = _raw_ratified(case)
    stale, backfilled, rebound = [], 0, []
    for r in library:
        if r.get("status") != "ratified":
            continue
        cit = r.get("citation") or {}
        d = cit.get("doc_id")
        if d not in doc_ids:
            continue
        entry = cat.get(d) or {}
        problems = []
        sha_then, sha_now = cit.get("doc_sha256", ""), entry.get("pdf_sha256", "")
        if sha_then and sha_now and sha_then != sha_now:
            problems.append("the source PDF changed since ratification")
        # A2: find the cited evidence by its text or its box on the page, not by a
        # label the catalog may never have carried (see core/evidence.py).
        clause = str(cit.get("clause") or "")
        page = cit.get("page")
        found, how = evidence.locate(entry.get("clauses", []), cit)
        if found is None:
            where = f"p.{page} of {d}" if page else d
            if how == "text-changed":
                problems.append(f"the text under cited clause {clause or '?'} on "
                                f"{where} changed since ratification")
            else:
                problems.append(f"cited clause {clause or '?'} no longer exists on "
                                f"{where}")
        if problems:
            r["status"] = "stale"
            r["stale_reason"] = "; ".join(problems)
            stale.append({"rule_id": r.get("id"), "reason": r["stale_reason"]})
            continue
        changed = False
        if not sha_then and sha_now:
            cit["doc_sha256"] = sha_now
            changed = True
        if found.get("text") and not cit.get("quote_sha256"):
            cit["quote_sha256"] = evidence.text_sha(found.get("text"))
            cit["quote"] = evidence.quote_of(found)
            changed = True
        if how == "quote-moved":
            # Same words, new address: follow the evidence and say so on the ledger.
            was = {"page": cit.get("page"), "bbox": cit.get("bbox")}
            cit["page"] = found.get("page")
            cit["bbox"] = found.get("bbox")
            rebound.append({"rule_id": r.get("id"), "doc_id": d, "from": was,
                            "to": {"page": cit["page"], "bbox": cit["bbox"]}})
            changed = True
        if changed:
            r["citation"] = cit
            backfilled += 1
    if stale or backfilled:
        _write_ratified(case, library)
    for s in stale:
        led.append("authoring.stale", s, actor="system")
    for rb in rebound:
        led.append("authoring.rebound", rb, actor="system")
    return stale


def _doc_meta(case, doc_id: str) -> dict:
    s = case.source_by_id(doc_id) or {}
    return {"doc_id": doc_id, "title": _display_title(case, _catalog(case), doc_id),  # C4
            "department": s.get("department"), "doc_type": s.get("doc_type")}


def _source_entry(case, cat, h: dict) -> dict:
    """One chat source chip's payload: document metadata + the hit's citation fields +
    extraction provenance (parse_source / kind / tier — OCR-4). `kind` may be missing
    from a hit produced by a backend that predates it (OpenSearch) — treat as text."""
    entry = cat.get(h["doc_id"]) or {}
    parse_source = entry.get("parse_source", "")
    kind = h.get("kind") or "text"
    origin = _page_origin(entry, h.get("page"))
    return {**_doc_meta(case, h["doc_id"]), "clause": h["clause"], "page": h["page"],
            "bbox": h["bbox"], "text": h["text"], "score": h["score"],
            "row_bbox": rowbands.narrow(case.dir, h["doc_id"], h["page"], h["bbox"], h["text"]),  # C2
            "parse_source": parse_source, "kind": kind, "text_origin": origin,
            "tier": _extraction_tier(parse_source, kind, origin)}


def _is_rate_row(hit: dict) -> bool:
    """A retrieved chunk that is a row of a rate table rather than prose about pay."""
    text = hit.get("text") or ""
    return "|" in text and "$" in text


def _dept_scope(case, cat, dept) -> list[str]:
    """Candidate documents for a department, restricted to what is actually INGESTED.

    The ONE scoping helper for every retrieval path (TICKETS.md B1). It encodes two
    hard-won rules together so no path can drift with only one of them again:
      1. case.yaml declares the intended corpus; the catalog holds what really parsed.
         A document with no clauses in the index is a plan, not a candidate.
      2. Callers must treat an EMPTY scope as "nothing to search" — never pass [] to
         backend.search, which reads it as "no filter" and would leak every other
         department's contracts into the answer.
    """
    ingested = {d["doc_id"] for d in cat.documents()}
    return [d for d in case.docs_for_department(dept) if d in ingested]


_ASKS_EVERYONE_RE = re.compile(r"\b(all|every|each|entire|whole|everyone|roster)\b", re.I)


def _clarify(qid: str, prompt: str, question: str, options: list | None = None) -> dict:
    return {"query_id": qid, "mode": "clarify", "prompt_echo": prompt,
            "question": question, "options": options or []}


def _titles(case, doc_ids: list[str]) -> list[str]:
    return [rulematch.doc_short_title(case.source_by_id(d) or {"id": d}) or d for d in doc_ids]


def _out_of_scope(case, qid: str, prompt: str, scope: list[str], named: list[str],
                  reason: str, coverage: float | None = None) -> dict:
    """The refusal for a question these documents do not answer (DEMO_TICKETS.md B6).
    Names the documents it checked so the visitor knows what WAS searched; never
    offers a department, because no department holds a heat-pump rebate."""
    if named:
        titles = _titles(case, named)
        answer = ("I could not find that in " + " or ".join(titles)
                  + " — it is not covered by that document.")
    else:
        titles = _titles(case, scope)
        answer = (f"That is not covered by the {len(titles)} documents I have: "
                  + "; ".join(titles) + ".")
    return {"query_id": qid, "mode": "out_of_scope", "prompt_echo": prompt,
            "answer": answer, "answer_source": "none", "reason": reason,
            "coverage": coverage, "options": [], "sources": [],
            "considered": [_doc_meta(case, d) for d in scope]}


def _policy_answer(case, led, qid: str, prompt: str, department: str | None = None,
                   lookup: bool = False, doc_scope: list[str] | None = None) -> dict:
    """Policy Q&A over a multi-document CORPUS (DEMO_TICKETS.md B6).

    1. Scope: the document the question names, else the bargaining unit of the
       classification it names, else the stated department, else the corpus. A caller
       that already resolved governance passes `doc_scope` (the entitlement fallback),
       so a quote can only come from the subject's own contract.
    2. Query: the prompt minus the words that named the scope and the question frame.
    3. Search within the scope; pin the cited clause of any ratified rule whose topic
       is in the question, so the quote boxes the same clause the costing answer did.
    4. Relevance floor: when the best hit covers too little of the question, say it is
       not in these documents instead of quoting the least-irrelevant preamble.
    5. Answer strictly from retrieved text, with every document considered and used.

    `lookup=True` serves a figure the documents already PUBLISH — a rate off a salary
    schedule. Same spine; the schedule is always in scope and only rows that carry the
    named classification's words are floated.
    """
    cat = _catalog(case)
    backend = _backend(case)
    ingested = {d["doc_id"] for d in cat.documents()}
    subjects_all = case.subjects()

    # 1. Scope.
    if doc_scope is not None:
        sc = rulematch.Scope([d for d in doc_scope if d in ingested], "unit")
        if lookup:
            for s in case.manifest.get("sources", []):
                if s.get("doc_type") == "salary-schedule" and s["id"] in ingested \
                        and s["id"] not in sc.doc_ids:
                    sc.doc_ids.append(s["id"])
    else:
        sc = rulematch.docs_for(case, ingested, prompt, department, lookup, subjects_all)
    scope = sc.doc_ids
    dept = sc.department or (department if department in case.departments() else None)
    led.append("policy.department", {"department": dept, "stated": bool(department),
                                     "known_departments": case.departments(),
                                     "scope_how": sc.how, "named_docs": sc.named,
                                     "bargaining_units": sc.units},
               actor="chat", query_id=qid)
    led.append("retrieval.candidates",
               {"department": dept, "candidate_docs": scope, "corpus_size": len(ingested),
                "declared": len(case.manifest.get("sources", [])), "scope_how": sc.how},
               actor="chat", query_id=qid)

    # 2. Query: nothing left ('hi', or only a document's name) -> not a question these
    #    documents answer.
    query, stems = rulematch.retrieval_query(prompt, sc.consumed)
    if not stems:
        led.append("policy.out_of_scope", {"reason": "no content terms", "query": query},
                   actor="chat", query_id=qid)
        return _out_of_scope(case, qid, prompt, scope, sc.named, "no content terms")

    # 3. Search within candidates. An empty scope means nothing relevant has been
    #    ingested yet — do NOT call search([]), which the backend reads as "no filter".
    hits = backend.search(query, doc_ids=scope, k=8) if scope else []
    pins: list[dict] = []
    if hits:
        rules = [r for r in case.rules() if r.citation.doc_id in scope]
        hits, pins = rulematch.pin_rule_clauses(
            rules, hits, stems, scope, rulematch.chunk_lookup(backend, cat))
    not_found: list[str] = []
    quote_rows = 3                      # rows the no-key lookup wording may quote
    if lookup:
        word_sets = rulematch.classification_words(sc.subjects)
        rows = [h for h in hits if _is_rate_row(h)]
        if word_sets:
            matching = [h for h in rows if rulematch.row_matches(h, word_sets)]
            if matching:
                hits = matching + [h for h in hits if h not in matching]
                quote_rows = min(quote_rows, len(matching))
            else:
                not_found = sorted({str(s.get("rank") or s.get("name")) for s in sc.subjects})
        else:
            hits.sort(key=lambda h: 0 if _is_rate_row(h) else 1)
    led.append("retrieval.hits",
               {"query": query,
                "hits": [{k: h.get(k) for k in ("doc_id", "clause", "page", "score", "pinned_by")}
                         for h in hits]},
               actor="chat", query_id=qid)

    # 4. Relevance floor, before any clarify: an off-corpus question must not be asked
    #    which department it is about. A lookup that named a classification the
    #    schedule has no row for is answered by name first — that IS the finding.
    cov = rulematch.best_coverage(stems, hits, rulematch.idf_table(backend))
    if not_found:
        led.append("policy.answer", {"answer": "no published rate row", "source": "none",
                                     "lookup": True, "scope_how": sc.how,
                                     "not_found": not_found, "coverage": round(cov, 3)},
                   actor="chat", query_id=qid)
        return {"query_id": qid, "mode": "lookup", "answer_source": "none",
                "answer": (f"I couldn't find a published rate row for {', '.join(not_found)}"
                           f" in {' or '.join(_titles(case, scope)) or 'these documents'}."),
                "sources": [], "department": dept, "scope_how": sc.how,
                "not_found": not_found, "coverage": round(cov, 3),
                "considered": [_doc_meta(case, d) for d in scope]}
    if hits and cov < rulematch.OUT_OF_SCOPE_FLOOR:
        led.append("policy.out_of_scope", {"reason": "below relevance floor",
                                           "coverage": round(cov, 3), "query": query},
                   actor="chat", query_id=qid)
        return _out_of_scope(case, qid, prompt, scope, sc.named, "below relevance floor",
                             round(cov, 3))
    if not hits and (sc.how == "corpus" or sc.named):
        led.append("policy.out_of_scope", {"reason": "no hits", "query": query},
                   actor="chat", query_id=qid)
        return _out_of_scope(case, qid, prompt, scope, sc.named, "no hits", 0.0)

    # If nothing scoped the question and the evidence spans several bargaining units,
    # the answer differs by contract -> ask which document (PRD §3.3 never bluff).
    # Options are document titles, one per unit; the salary schedule is never offered.
    if sc.how == "corpus" and hits:
        by_unit: dict[str, str] = {}
        for h in hits:
            s = case.source_by_id(h["doc_id"]) or {}
            u = s.get("bargaining_unit")
            if u and u not in by_unit:
                by_unit[u] = rulematch.doc_short_title(s) or h["doc_id"]
        if len(by_unit) > 1:
            options = list(by_unit.values())
            led.append("policy.clarify",
                       {"reason": "evidence spans multiple bargaining units",
                        "options": options}, actor="chat", query_id=qid)
            return {"query_id": qid, "mode": "clarify", "prompt_echo": prompt,
                    "question": "That's answered differently by each unit's contract. "
                                "Which one?",
                    "options": options,
                    "considered": [_doc_meta(case, h["doc_id"]) for h in hits]}

    if not hits:
        where = (" or ".join(_titles(case, sc.named)) if sc.named
                 else f"the {dept} documents" if dept
                 else "these documents")
        return {"query_id": qid, "mode": "lookup" if lookup else "policy",
                "answer_source": "none",
                "answer": f"I couldn't find a clause covering that in {where}.",
                "sources": [], "department": dept, "scope_how": sc.how,
                "considered": [_doc_meta(case, d) for d in scope]}

    # 5. Grounded answer + provenance.
    ans = llm.answer_policy(prompt, hits, lookup=lookup)
    if ans.get("source") == "stub":
        # The no-key wording: name the document and page when the ingest found no
        # section label, so a quote never opens with a bare section sign.
        ans["answer"] = rulematch.compose_quote(
            hits, lambda d: _titles(case, [d])[0], lookup=lookup, max_rows=quote_rows)
    led.append("policy.answer", {"answer": ans["answer"], "source": ans["source"],
                                 "lookup": lookup, "scope_how": sc.how,
                                 "pinned": [p["rule_id"] for p in pins],
                                 "coverage": round(cov, 3),
                                 "docs_used": sorted({h["doc_id"] for h in hits[:4]})},
               actor="chat", query_id=qid)
    for h in hits[:4]:
        led.append("citation", {k: h.get(k) for k in ("doc_id", "clause", "page", "bbox")},
                   actor="engine", query_id=qid)
    return {"query_id": qid, "needs_confirmation": False,
            "mode": "lookup" if lookup else "policy",
            "answer": ans["answer"], "answer_source": ans["source"], "department": dept,
            "scope_how": sc.how, "bargaining_units": sc.units, "coverage": round(cov, 3),
            "pinned": pins, "corpus_size": len(ingested),
            "considered": [_doc_meta(case, d) for d in scope],
            "sources": [{**_source_entry(case, cat, h), "pinned_by": h.get("pinned_by")}
                        for h in hits[:4]]}


def _entitlement_answer(case, led, qid: str, prompt: str,
                        department: str | None = None) -> dict:
    """Answer a non-money rule question — "how many bereavement shifts?", "what's the
    grievance deadline?" — with the SAME guarantees as a dollar figure
    (DEMO_TICKETS.md B3).

    An MOU is a rulebook; pay is one chapter. These clauses are equally determinate and
    deserve the deterministic engine + citation, not an LLM paraphrase.

    Who the question is for decides which contract answers it: the named
    classification's bargaining unit (else the named document's), resolved through
    governance to its governing documents. Retrieval runs inside those documents only;
    a ratified non-currency rule is selected when the chunk occupying its cited box is
    among the top hits, or its topic word is in the question. The engine computes the
    typed value. No rule -> quote the subject's own contract (policy), never another
    unit's. Nobody named -> ask who it is for; one department holds two units, so a
    department-wide search would answer a firefighter from the chiefs' contract.
    """
    backend = _backend(case)
    cat = _catalog(case)
    subjects_all = case.subjects()
    params = llm.parse_intent(prompt, _extraction(case), subjects_all)
    if params.get("unverified_numbers"):
        return _clarify(qid, prompt,
                        "I read a number out of that question that it doesn't actually "
                        "state — can you restate it with the amount spelled out?")
    named = set(params.get("subjects") or [])
    named_rows = [s for s in subjects_all if s.get("name") in named]
    units, _rows, how = rulematch.units_for(case, prompt, subjects_all, label=department)
    if named_rows:
        units = sorted({s.get("bargaining_unit") for s in named_rows if s.get("bargaining_unit")})
        how = "classification"
    date_iso = governance.parse_date(params.get("date"), DEFAULT_YEAR) or ""
    led.append("entitlement.scope", {"units": units, "how": how,
                                     "subjects": [s.get("name") for s in named_rows],
                                     "date_iso": date_iso},
               actor="chat", query_id=qid)

    if not units:
        if department and department in case.departments():
            # The visitor already confirmed a department (clarify post-back): quote
            # from that department's documents, never from a wider scope.
            led.append("entitlement.fallback", {"reason": "no subject named"},
                       actor="chat", query_id=qid)
            return _policy_answer(case, led, qid, prompt, department,
                                  doc_scope=_dept_scope(case, cat, department))
        led.append("entitlement.clarify", {"reason": "no subject or document named"},
                   actor="chat", query_id=qid)
        examples = ", ".join(str(s.get("name")) for s in subjects_all[:3])
        return _clarify(qid, prompt,
                        "Who is this for? Name a classification (e.g. "
                        f"{examples}) or the contract (e.g. "
                        f"{', '.join(_titles(case, [s['id'] for s in case.manifest.get('sources', [])[:2]]))}).")

    _ingested = {d["doc_id"] for d in cat.documents()}
    # The classification named the scope; search for the entitlement, not the rank.
    query, stems = rulematch.retrieval_query(prompt, rulematch.rank_stems(named_rows))
    lookup = rulematch.chunk_lookup(backend, cat)
    eng = {"hours": params.get("hours", 0.0), "date": params.get("date", ""),
           "date_iso": date_iso, "holiday_weekday": params.get("holiday_weekday", "")}
    line_items: list[dict] = []
    matches: list[dict] = []
    not_covered: list[dict] = []
    all_scope: list[str] = []
    for unit in units:
        gov = governance.resolve([unit], date_iso or None, case.manifest.get("sources", []))
        scope = [d for d in gov.doc_ids if d in _ingested]
        all_scope += [d for d in scope if d not in all_scope]
        hits = backend.search(query or prompt, doc_ids=scope, k=8) if scope else []
        led.append("entitlement.retrieval",
                   {"unit": unit, "candidates": len(scope), "candidate_docs": scope,
                    "query": query,
                    "hits": [{k: h.get(k) for k in ("doc_id", "clause", "page", "score")}
                             for h in hits]},
                   actor="chat", query_id=qid)
        candidates = [r for r in case.rules()
                      if r.result_type != "currency" and r.citation.doc_id in scope]
        rules, found, ambiguous = rulematch.select_rules(candidates, hits, stems)
        if ambiguous:
            led.append("entitlement.clarify", {"reason": "several topics match",
                                               "options": ambiguous},
                       actor="chat", query_id=qid)
            return _clarify(qid, prompt, "Which of these is the question about? "
                            + ", ".join(ambiguous) + ".", ambiguous)
        if not rules:
            not_covered.append({"unit": unit, "docs": scope})
            continue
        subjects = [s for s in named_rows if s.get("bargaining_unit") == unit] \
            or [s for s in subjects_all if s.get("bargaining_unit") == unit]
        try:
            result = calculate(eng, subjects, rules, case.rounding_places())
        except (ValueError, RuleError, ArithmeticError) as e:   # A4: a rule error is not a 500
            led.append("costing.blocked", {"reason": "rule evaluation error", "unit": unit,
                                           "error": f"{type(e).__name__}: {e}"},
                       actor="engine", query_id=qid)
            led.append("entitlement.fallback", {"reason": f"engine: {e}", "unit": unit},
                       actor="chat", query_id=qid)
            not_covered.append({"unit": unit, "docs": scope})
            continue
        rd = _enrich_citations(cat, result.to_dict())
        items = rd.get("line_items", [])
        if not any(s.get("bargaining_unit") == unit for s in named_rows) and items \
                and len({(li.get("total"), li.get("rule_id")) for li in items}) == 1:
            # Nobody named: the whole unit gets the same answer, so show one row for
            # the unit rather than six identical ones.
            title = _titles(case, scope[:1])[0] if scope else unit
            items = [dict(items[0], subject=f"Any {title} member")]
        for m in found:
            led.append("entitlement.match", {**m, "unit": unit, "scope_how": how},
                       actor="chat", query_id=qid)
        matches += [{**m, "unit": unit} for m in found]
        line_items += items

    if not line_items:
        led.append("entitlement.fallback",
                   {"reason": "no ratified non-currency rule for the retrieved clauses",
                    "units": units}, actor="chat", query_id=qid)
        return _policy_answer(case, led, qid, prompt, department, doc_scope=all_scope)

    total = line_items[0]["total"] if len(line_items) == 1 else \
        round(sum(float(li.get("total") or 0) for li in line_items), case.rounding_places())
    rd = {"total": total, "line_items": line_items}
    led.append("answer.snapshot", {"total": total, "intent": "entitlement"},
               actor="engine", query_id=qid)
    depts = sorted({(case.source_by_id(d) or {}).get("department") or "" for d in all_scope})
    return {"query_id": qid, "needs_confirmation": False, "mode": "entitlement",
            "department": ", ".join(d for d in depts if d) or None,
            "bargaining_units": units, "scope_how": how, "match": matches,
            "not_covered": not_covered, "params": params, "result": rd,
            "corpus_size": len(case.manifest.get("sources", []))}


def _retriever() -> CatalogLLMRetriever:
    return CatalogLLMRetriever()


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
_NOCACHE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/healthz")
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


@app.get("/", response_class=HTMLResponse)
def chat_page():
    return FileResponse(os.path.join(TEMPLATES, "chat.html"), headers=_NOCACHE)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return FileResponse(os.path.join(TEMPLATES, "admin.html"), headers=_NOCACHE)


@app.get("/static/app.js")
def app_js():
    return FileResponse(os.path.join(TEMPLATES, "app.js"),
                        media_type="application/javascript", headers=_NOCACHE)


@app.get("/static/tour.js")
def tour_js():
    """Guided demo tour: one shared step engine for both surfaces (chat starts it,
    /admin?tour=1 resumes it). Served like app.js so both templates can include it."""
    return FileResponse(os.path.join(TEMPLATES, "tour.js"),
                        media_type="application/javascript", headers=_NOCACHE)


@app.get("/static/styles.css")
def styles_css():
    return FileResponse(os.path.join(TEMPLATES, "styles.css"),
                        media_type="text/css", headers=_NOCACHE)


@app.get("/api/case")
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
            # Declared document titles, so the chat page can name a contract wherever
            # a response only carries its id (I4).
            "sources": [_doc_meta(case, s.get("id")) for s in case.manifest.get("sources", [])
                        if s.get("id")]}


# --------------------------------------------------------------------------- #
# CHAT
# --------------------------------------------------------------------------- #
@app.post("/chat")
async def chat(request: Request):
    """Public entrypoint. Wraps the handler so that EVERY AI call made while answering
    lands in the ledger, whichever of the handler's several exits is taken — including
    the ones that refuse, and including the ones that fail.

    A try/finally rather than draining before each return: an exit that forgets to record
    is an answer with no evidence of how it was reached, and the handler grows exits.
    """
    body = await request.json()
    led = _case().ledger()
    # The id is ALWAYS server-issued (A1): it names the snapshot file and threads the
    # ledger, so a client value is only ever a reference to an earlier query.
    qid = qid_mod.mint()
    continues = qid_mod.continuation(led, body.get("query_id"))
    with llm.record() as trail:
        try:
            return await _chat(body, qid, continues)
        finally:
            for call in trail:
                led.append("llm.call", call, actor="llm", query_id=qid)


async def _chat(body: dict, qid: str, continues: str | None = None):
    prompt = (body.get("prompt") or "").strip()
    forced_doc = body.get("doc_id")  # set when the user confirms a document

    case = _case()
    led = case.ledger()
    cat = _catalog(case)

    department = body.get("department")  # set when the user answers "which department?"
    # Every query — follow-ups included — opens with its own chat.prompt, so the trail
    # of a confirmed-document answer starts at a prompt and `continues` links it back.
    led.append("chat.prompt", {"text": prompt, "department": department,
                               "doc_id": forced_doc, "continues": continues},
               actor="chat", query_id=qid)
    if forced_doc:
        # B2: a caller-supplied document never replaces governance on the costing path.
        # The question is still recorded (chat.prompt above) and still routed — it just
        # cannot pick its own contract.
        led.append("costing.doc_id_ignored", {"doc_id": forced_doc,
                   "reason": "documents are chosen by governance per bargaining unit"},
                   actor="chat", query_id=qid)
    # Router: costing vs policy Q&A. Both return an answer WITH clickable proof.
    intent = await run_in_threadpool(llm.classify_intent, prompt)   # A8: off the loop
    led.append("intent.classify", {"intent": intent}, actor="chat", query_id=qid)
    if intent == "policy":
        return await run_in_threadpool(_policy_answer, case, led, qid, prompt, department)
    if intent == "lookup":
        return await run_in_threadpool(_policy_answer, case, led, qid, prompt,
                                       department, lookup=True)
    if intent == "entitlement":
        return await run_in_threadpool(_entitlement_answer, case, led, qid, prompt,
                                       department)

    # --- costing-correctness (B1, B2, B4) --------------------------------------- #
    # A clarifying answer comes back as {prompt, query_id, clarified: {<field>: value}}.
    # Only the field the server asked for is read, and only a value it can verify.
    clarified = body.get("clarified") if isinstance(body.get("clarified"), dict) else {}
    extraction_cfg = _extraction(case)

    # 1. Parse intent (LLM translation layer, with deterministic fallback).
    subjects_all = case.subjects()
    labels_all = [str(s.get("name", "")) for s in subjects_all]
    params = await run_in_threadpool(llm.parse_intent, prompt, extraction_cfg,
                                     subjects_all)
    led.append("llm.parse_intent", params, actor="chat", query_id=qid)

    # A model-extracted number the question doesn't contain never reaches the engine
    # (TICKETS.md B3) — ask, don't multiply by it.
    unverified = params.get("unverified_numbers")
    if unverified:
        led.append("costing.clarify",
                   {"reason": "model-extracted number absent from the question",
                    "unverified": unverified}, actor="chat", query_id=qid)
        return _clarify(qid, prompt,
                        f"I read {unverified.get('hours')} hours out of that, but the "
                        "question doesn't state that number. How long is the shift?")

    # 2. Select the subjects named in the prompt, and derive their bargaining unit(s)
    #    + the shift date — the governance keys. A question that names NOBODY is asked
    #    who it is for (TICKETS.md B4) — silently costing the entire roster turned a
    #    vague question into one large confident total.
    named = set(params.get("subjects") or [])
    chosen_subject = clarified.get("subject")
    if chosen_subject in labels_all:
        named = {chosen_subject}
        params["subjects"] = [chosen_subject]
    subjects = [s for s in subjects_all if s.get("name") in named]
    asks_everyone = bool(_ASKS_EVERYONE_RE.search(prompt))
    if not subjects:
        if asks_everyone:
            subjects = subjects_all
        else:
            led.append("costing.clarify", {"reason": "no subject named"},
                       actor="chat", query_id=qid)
            examples = ", ".join(str(s.get("name")) for s in subjects_all[:3])
            return _clarify(qid, prompt,
                            "Who is this for? Name a classification (e.g. "
                            f"{examples}).")
    # B4: a description that matches several classifications is a question, not a sum —
    # unless a label was typed verbatim or the question asked for a group.
    if len(subjects) > 1 and not asks_everyone and not chosen_subject:
        pl = prompt.lower()
        verbatim = any(str(s.get("name", "")).lower() in pl for s in subjects)
        ranks = {str(s.get("rank", "")).lower() for s in subjects if s.get("rank")}
        plural = any(re.search(rf"\b{re.escape(r)}s\b", pl) for r in ranks if r)
        if not verbatim and not plural:
            opts = [str(s.get("name")) for s in subjects]
            led.append("costing.clarify", {"reason": "several classifications match",
                                           "options": opts},
                       actor="chat", query_id=qid)
            res = _clarify(qid, prompt, "Which classification? That description matches "
                           f"{len(opts)} rows on the roster.", opts)
            res["field"] = "subject"
            return res
    units = sorted({s.get("bargaining_unit") for s in subjects if s.get("bargaining_unit")})
    date_iso = governance.parse_date(params.get("date"), default_year=DEFAULT_YEAR)
    year_stated = bool(re.search(r"\b(19|20)\d{2}\b", str(params.get("date") or "")))
    led.append("data.read",
               {"adapter": case.manifest.get("data", {}).get("adapter"),
                "rows": [s.get("name") for s in subjects],
                "bargaining_units": units, "shift_date": date_iso,
                "fields": list((subjects[0].keys() if subjects else [])),
                "records": subjects, "data_sha256": audit.data_sha256(case)},
               actor="chat", query_id=qid)

    # 3. Hours (B4): the number the question STATES, with roster labels masked so the
    #    '56' in '(56 hr, top step)' is never a shift length. One clean candidate goes
    #    to the engine; anything else is asked about, never guessed.
    hx = costing.extract_hours(prompt, labels_all)
    cands = hx["candidates"]
    hours_max = costing.hours_limit(extraction_cfg)
    hours = None
    picked = clarified.get("hours")
    if picked is not None:
        try:
            pv = float(picked)
        except (TypeError, ValueError):
            pv = None
        if pv is not None and pv in cands and 0 < pv <= hours_max:
            hours = pv
    if hours is None and len(cands) == 1 and not hx["invalid"] and 0 < cands[0] <= hours_max:
        hours = cands[0]
    if hours is None:
        if not cands and not hx["invalid"]:
            led.append("costing.clarify", {"reason": "no hours stated"},
                       actor="chat", query_id=qid)
            res = _clarify(qid, prompt, "How many hours? The question doesn't state the "
                           "length of the shift.")
            res["field"] = "hours"
            return res
        valid = [c for c in cands if 0 < c <= hours_max]
        led.append("costing.clarify",
                   {"reason": "hours ambiguous", "candidates": cands,
                    "invalid": hx["invalid"], "max": hours_max},
                   actor="chat", query_id=qid)
        if valid:
            q = ("Which of these is the length of the shift? I read "
                 + ", ".join(f"{c:g} hours" for c in cands)
                 + (f" (and could not read: {', '.join(hx['invalid'])})" if hx["invalid"] else "")
                 + ".")
        else:
            q = ("How many hours? I could not read a usable shift length from the "
                 "question" + (f" ({', '.join(hx['invalid'])})" if hx["invalid"] else "")
                 + (f"; the longest shift I will cost is {hours_max:g} hours" if cands else "")
                 + ".")
        res = _clarify(qid, prompt, q, [f"{c:g}" for c in valid])
        res["field"] = "hours"
        return res
    params["hours"] = hours

    # 4. Pay type (B1): which branch of pay the question asks for — read from the case's
    #    lexicon, never from the model. Nothing asked -> ask, listing what is approved.
    lexicon = costing.pay_type_lexicon(extraction_cfg)
    pt = costing.detect_pay_type(prompt, lexicon)
    asked = list(pt["asked"])
    if clarified.get("pay_type") in lexicon:
        asked = [clarified["pay_type"]]
    approved_all = costing.base_topics([r for r in case.rules()
                                        if r.citation.doc_id in {
                                            s.get("id") for s in case.manifest.get("sources", [])
                                            if s.get("bargaining_unit") in units}])
    if not asked:
        led.append("costing.clarify", {"reason": "pay type not stated",
                                       "approved_topics": approved_all,
                                       "negated": pt["negated"]},
                   actor="chat", query_id=qid)
        res = _clarify(qid, prompt, "Which kind of pay? Approved for this unit: "
                       + (", ".join(approved_all) or "none yet") + ".", approved_all)
        res["field"] = "pay_type"
        return res
    pay_type = asked[0] if len(asked) == 1 else "+".join(asked)
    params["pay_type"] = pay_type
    interp = costing.interpretation(
        hours, pay_type, [str(s.get("name")) for s in subjects], date_iso, year_stated,
        str(params.get("source") or "stub"),
        checks=[{"field": "hours", "deterministic": hours,
                 "model": params.get("hours") if params.get("source") == "claude" else None,
                 "agree": True},
                {"field": "pay_type", "deterministic": pay_type, "model": None, "agree": True}])
    led.append("chat.interpretation", interp, actor="chat", query_id=qid)

    # 5. Per-unit governance + engine (B2). Each bargaining unit resolves to ITS contract
    #    and ITS ratified rules; an uncovered unit gets a reason, never another unit's rule.
    eng_params = {"hours": hours,
                  "date": params.get("date", ""),
                  "date_iso": date_iso or "",
                  "holiday_weekday": params.get("holiday_weekday", ""),
                  "pay_type": pay_type}
    eng_params.update(queryfacts.engine_extras(case, params))  # J1a: years_of_service etc.
    outcomes = costing.cost_by_unit(case, cat, led, qid, subjects, eng_params, date_iso,
                                    asked, _doc_integrity, _raw_ratified)
    ok = [o for o in outcomes if o["status"] == "ok"]
    refused = [o for o in outcomes if o["status"] != "ok"]
    uncovered = [{"subject": n, "bargaining_unit": o["bargaining_unit"],
                  "status": o["status"], "reason": o["reason"],
                  "governing_docs": o["doc_ids"], "approved_topics": o["approved_topics"]}
                 for o in refused for n in o["subjects"]]
    for o in refused:
        led.append("costing.uncovered",
                   {"bargaining_unit": o["bargaining_unit"], "subjects": o["subjects"],
                    "status": o["status"], "reason": o["reason"], "doc": o["doc_ids"],
                    "stale": o["stale"], "approved_topics": o["approved_topics"]},
                   actor="engine", query_id=qid)

    if not ok:
        gov_docs = sorted({d for o in refused for d in o["doc_ids"]})
        chosen = ", ".join(gov_docs)
        statuses = {o["status"] for o in refused}
        if statuses & {"no_rule_for_pay_type", "no_rule_for_scenario"}:
            # B1: the unit has approved rules, just not for THIS branch of pay. Refuse
            # with the reason, what IS approved, and the closest clause — labelled as
            # text, not as an answer. No snapshot: nothing was computed.
            approved = sorted({t for o in refused for t in o["approved_topics"]})
            reason = "; ".join(o["reason"] for o in refused)
            nearest = _nearest_clauses(case, cat, prompt, gov_docs)
            # The clause(s) that ARE approved for the unit, as chips that open the page:
            # the visitor sees what the unit's rulebook covers instead of this question.
            approved_clauses = []
            for o in refused:
                for ar in o.get("approved_rules", []):
                    c = ar["citation"]
                    approved_clauses.append({**_doc_meta(case, c.get("doc_id", "")),
                                             "rule_id": ar["rule_id"], "topic": ar["topic"],
                                             "clause": c.get("clause", ""),
                                             "page": c.get("page", 0), "bbox": c.get("bbox", []),
                                             "text": ar["human_readable"]})
            led.append("costing.refused",
                       {"asked": asked, "approved_topics": approved, "units": units,
                        "reason": reason, "doc": gov_docs,
                        "nearest": [{"doc_id": n["doc_id"], "page": n["page"]} for n in nearest]},
                       actor="engine", query_id=qid)
            return {"query_id": qid, "needs_confirmation": False, "mode": "refused",
                    "chosen_doc": chosen, "bargaining_units": units, "shift_date": date_iso,
                    "asked": asked, "approved_topics": approved, "reason": reason,
                    "message": ("I can't compute that: " + reason + ". "
                                "The closest clause is shown below as text — it is not a "
                                "computed answer."),
                    "nearest": nearest, "nearest_label": "closest text — not a computed answer",
                    "approved_clauses": approved_clauses,
                    "uncovered": uncovered, "interpretation": interp, "params": params}
        # No unit could be costed for a structural reason (no contract, no rules, stale,
        # provenance). Single unit keeps the long-standing message; several list each.
        o = refused[0]
        led.append("costing.blocked",
                   {"reason": {"no_contract": "no governing contract",
                               "stale": "rules pending re-verification",
                               "no_rules": "no ratified rules",
                               "integrity": "provenance mismatch",
                               "rule_error": "rule evaluation error"}.get(o["status"], o["status"]),
                    "error": o.get("error"),   # A4: the exception text, when a rule failed
                    "doc": gov_docs, "stale": [s for r in refused for s in r["stale"]],
                    "units": units, "per_unit": [{"bargaining_unit": r["bargaining_unit"],
                                                  "status": r["status"], "reason": r["reason"]}
                                                 for r in refused]},
                   actor="engine", query_id=qid)
        # One helper builds every blocked answer (chat-ui I6): contract named by title,
        # one plain sentence, a `next` link to an admin tab that exists.
        if len(refused) == 1:
            kind = {"no_contract": "no_contract", "stale": "stale_rules",
                    "integrity": "provenance", "rule_error": "rule_error"}.get(o["status"],
                                                                               "no_rules")
            detail = o["reason"] if kind == "no_contract" else \
                o.get("error") if kind == "rule_error" else \
                o["reason"] if kind == "provenance" else None
            res = refusal.blocked(case, qid, gov_docs, subjects, kind, detail=detail,
                                  stale=[st for st in o["stale"]])
        else:
            res = refusal.blocked(case, qid, gov_docs, subjects, "not_covered",
                                  detail="; ".join(f"{', '.join(r['subjects'])}: {r['reason']}"
                                                   for r in refused))
        res.update({"bargaining_units": units, "shift_date": date_iso,
                    "uncovered": uncovered, "interpretation": interp})
        return res

    # 6. Ledger: decision logic + math + citations, straight from the engine trace.
    #    (rule.* and citation payloads keep their shape; bargaining_unit is added.)
    for o in ok:
        for li in o["trace_items"]:
            for step in li.trace:
                if step.kind in ("modifier", "selector-considered", "selector-chosen", "math", "flag", "premium"):
                    led.append(f"rule.{step.kind.replace('-', '_')}",
                               {"subject": li.subject, "rule_id": step.rule_id,
                                "detail": step.detail, "value": step.value,
                                "bargaining_unit": o["bargaining_unit"]},
                               actor="engine", query_id=qid)
            for c in li.citations:
                led.append("citation", {"subject": li.subject, **c},
                           actor="engine", query_id=qid)

    # 7. Snapshot + answer. Covered lines only; the uncovered rows ride alongside with
    #    their reasons, and the total is the sum of what was actually computed.
    line_items = [li for o in ok for li in o["line_items"]]
    total = costing.sum_lines(line_items, case.rounding_places())
    rules_used: list = []
    seen_rule_ids: set[str] = set()
    for o in ok:
        for r in o["rules_used"]:
            if r.id not in seen_rule_ids:
                seen_rule_ids.add(r.id)
                rules_used.append(r)
    result_dict = {"total": total, "line_items": line_items, "uncovered": uncovered,
                   "partial": bool(uncovered),
                   "per_unit": [{"bargaining_unit": o["bargaining_unit"], "status": o["status"],
                                 "reason": o["reason"], "governing_docs": o["doc_ids"],
                                 "rules": [r.id for r in o["rules_used"]],
                                 "subjects": o["subjects"]} for o in outcomes]}
    # Schema-2 snapshot bound to the chain (F1). The frozen result is the engine's own
    # shape over the covered subjects and the rules actually used, so a replay recomputes
    # it exactly; the uncovered rows and per-unit detail live in the response only.
    covered_names = {n for o in ok for n in o["subjects"]}
    covered_subjects = [sd for sd in subjects if str(sd.get("name")) in covered_names]
    engine_result = {"total": total,
                     "line_items": [{k: v for k, v in li.items()
                                     if k not in ("bargaining_unit", "governing_docs")}
                                    for li in line_items]}
    audit.record_answer(case, led, qid, eng_params, covered_subjects, rules_used,
                        engine_result, basis_scope=SHIFT_BASES,
                        extra={"partial": bool(uncovered), "uncovered": uncovered})

    result_dict = _enrich_citations(cat, result_dict)
    chosen_docs = []
    for o in ok:
        for d in o["doc_ids"]:
            if d not in chosen_docs:
                chosen_docs.append(d)
    return {"query_id": qid, "needs_confirmation": False, "mode": "costing",
            "chosen_doc": ", ".join(chosen_docs),
            "chosen_docs": [_doc_meta(case, d) for d in chosen_docs],   # chat-ui (I4)
            "routing_path": "governance",
            "bargaining_units": units, "shift_date": date_iso, "params": params,
            "partial": bool(uncovered), "interpretation": interp, "result": result_dict}


@app.get("/chat/audit/{query_id}")
def chat_audit(query_id: str):
    if not qid_mod.is_qid(query_id):
        return JSONResponse({"error": "unknown query id"}, status_code=404)
    case = _case()
    # A follow-up ("which department?" answered) is its own query that `continues` the
    # one it clarifies; the drawer shows the whole conversation, parents first (A1).
    events, continues = qid_mod.trail_with_parents(case.ledger(), query_id)
    # Summarise the AI involvement up front. "How did AI reach this answer" starts with
    # whether AI was involved at all — and with a deterministic fallback behind every
    # touchpoint, that is a real question with a non-obvious answer.
    calls = [e["payload"] for e in events if e.get("type") == "llm.call"]
    ai = {"calls": calls,
          "used_model": any(c.get("source") == "claude" for c in calls),
          "fell_back": [c["fn"] for c in calls if c.get("source") == "fallback"],
          "errors": [c for c in calls if c.get("source") == "error"],
          "total_ms": sum(c.get("ms", 0) for c in calls)}
    return {"query_id": query_id, "events": events, "ai": ai, "continues": continues}


def _resolve_pdf(case, doc_id: str) -> str | None:
    """Map a doc_id to a PDF on disk, or None.

    doc_id arrives from the URL, so it is never joined into a path directly: it must
    match a document the case declares or the catalog holds, and the file it names must
    resolve inside the case directory. Otherwise "../../../etc/passwd" is a doc_id and
    the file-serving route below is an arbitrary-read.
    """
    src = case.source_by_id(doc_id)
    if src:
        pdf_path = src["file"]
        if not os.path.isabs(pdf_path):
            pdf_path = os.path.join(case.dir, pdf_path)
    else:
        entry = _catalog(case).get(doc_id)  # uploaded doc
        if not entry:
            return None
        pdf_path = entry["file"]
    pdf_path = os.path.realpath(pdf_path)
    if os.path.commonpath([pdf_path, os.path.realpath(case.dir)]) != os.path.realpath(case.dir):
        return None
    return pdf_path if os.path.exists(pdf_path) else None


@app.get("/doc/{doc_id}/file")
def doc_file(doc_id: str):
    """Serve the original PDF.

    The rendered-page image proves a citation, but a reader who wants to check the
    contract as a whole — scroll it, search it, print it for a council packet — needs
    the document itself, not a picture of one page of it. Inline so a click opens the
    browser's viewer rather than downloading.
    """
    case = _case()
    pdf_path = _resolve_pdf(case, doc_id)
    if not pdf_path:
        return JSONResponse({"error": "unknown or unavailable document"}, status_code=404)
    ok, expected, actual = _check_source_hash(_catalog(case), doc_id, pdf_path)
    if not ok:
        case.ledger().append("provenance.mismatch",
                             {"doc_id": doc_id, "expected": expected, "actual": actual,
                              "route": "doc_file"}, actor="system")
        return JSONResponse({"error": "source document changed since ingestion — "
                             "re-ingest and re-verify before it can be served"},
                            status_code=409)
    return FileResponse(pdf_path, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'inline; filename="{os.path.basename(pdf_path)}"'})


@app.get("/doc/{doc_id}/page/{page}")
def doc_page(doc_id: str, page: int, bbox: str = "", crop: str = ""):
    """The cited page as a PNG with the clause boxed (citations-polish, C2/I12).

    bbox  'l,t,r,b' in PDF points, either corner order; must be 4 finite numbers.
    crop  '1' cuts the image to the box plus CROP_MARGIN_PT of context above and
          below (full page width, 3x scale) — the readable image the drawer shows
          first; 'l,t,r,b' cuts an explicit region instead. Bad arity or a non-finite
          number in either is a 400, never a confident page with no box on it.
    A page outside 1..page_count is a 404, not a quietly different page.
    """
    case = _case()
    pdf_path = _resolve_pdf(case, doc_id)
    if not pdf_path:
        return JSONResponse({"error": "unknown doc"}, status_code=404)
    ok, expected, actual = _check_source_hash(_catalog(case), doc_id, pdf_path)
    if not ok:
        case.ledger().append("provenance.mismatch",
                             {"doc_id": doc_id, "expected": expected, "actual": actual,
                              "route": "doc_page"}, actor="system")
        return JSONResponse({"error": "source document changed since ingestion — "
                             "the citation cannot be rendered against it"},
                            status_code=409)
    params, problem = _page_render_params(bbox, crop)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    dims = page_dims(pdf_path, page)
    if dims is None:
        return JSONResponse({"error": "render unavailable"}, status_code=503)
    if page < 1 or page > dims[1]:
        return JSONResponse({"error": f"no page {page}: the document has {dims[1]} pages"},
                            status_code=404)
    png = render_page_with_bbox(pdf_path, page, **params)
    if png is None:
        return JSONResponse({"error": "render unavailable"}, status_code=503)
    return Response(content=png, media_type="image/png")


@app.get("/doc/{doc_id}/clauses")
def doc_clauses(doc_id: str, page: int = 1):
    """Every clause extracted from one page, plus the page's dimensions in PDF points
    (OCR_TICKETS.md OCR-1). The X-ray overlay draws each bbox over the rendered page
    image; without width/height the client cannot scale point-space boxes onto the
    image, whose pixel size depends on the render scale.

    `kind` is normalised here — the catalog omits it for normal text, but the overlay
    colour-codes by kind and an absent key would make every consumer re-implement the
    default. Likewise `low_confidence` (OCR-7): the catalog stamps it only on clauses
    from pages docling's OCR scored below LOW_OCR_CONFIDENCE, and the endpoint
    normalises it to an always-present boolean plus a page-level `ocr_confidence`
    score (null on pages that were never OCR'd — every digital page).
    """
    case = _case()
    pdf_path = _resolve_pdf(case, doc_id)
    if not pdf_path:
        return JSONResponse({"error": "unknown doc"}, status_code=404)
    if (blocked := _provenance_gate(case, doc_id, pdf_path, "doc_clauses")) is not None:  # C7
        return blocked
    dims = page_dims(pdf_path, page)
    if dims is None:
        return JSONResponse({"error": "page metrics unavailable"}, status_code=503)
    page, page_count, width, height = dims
    cat = _catalog(case)
    clauses = [{"clause": c.get("clause", ""), "kind": c.get("kind") or "text",
                "page": page, "bbox": c.get("bbox", []), "text": c.get("text", ""),
                "low_confidence": bool(c.get("low_confidence"))}
               for c in cat.clauses(doc_id) if c.get("page") == page]
    # Numeric-cell verification (D2): every table row carries `cells` (per-cell
    # stored / re-read / status) and `cell_status` from <case>/cell_checks.json;
    # [] / null where the page was never verified. Computed by scripts/verify_cells.py,
    # never at request time — the second OCR engine is not a request-path cost.
    cellcheck.annotate_clauses(case.dir, doc_id, page, clauses)
    entry = cat.get(doc_id) or {}
    conf = (entry.get("page_confidence") or {}).get(str(page))
    return {"doc_id": doc_id, "page": page, "page_count": page_count,
            "width": width, "height": height, "clauses": clauses,
            "ocr_confidence": conf,
            "low_confidence": conf is not None and conf < ingest.LOW_OCR_CONFIDENCE,
            "text_origin": _page_origin(entry, page),
            "disputed_cells": sum(1 for c in clauses for x in c["cells"]
                                  if x.get("status") == "disputed")}


# --------------------------------------------------------------------------- #
# ADMIN
# --------------------------------------------------------------------------- #
def _ingest_warning(entry: dict) -> str | None:
    """A doc that extracts nothing is silently unanswerable — surface it rather than
    listing it as ingested. Shared by bulk ingest and upload so both report degraded
    extraction identically."""
    if not entry["clauses"]:
        return "NO CONTENT EXTRACTED — this document is not searchable"
    if entry.get("index_error"):
        return (f"indexing failed ({entry['index_error']}) — catalogued but absent "
                "from search")
    if entry["parse_source"] == "raw-text-fallback":
        return "degraded extraction (page-level citations only)"
    return None


def _ingest_worker(job_id: str):
    """Runs ingestion off the request path so large PDFs (docling parse can take
    minutes on a 50-page MOU) never time out the HTTP request (PRD §8B)."""
    job = _JOBS[job_id]
    try:
        case = _case()
        led = case.ledger()
        cat = _catalog(case)
        tax = _taxonomy(case)
        backend = _backend(case)
        sources = case.manifest.get("sources", [])
        force = bool(job.get("force"))
        job["total"] = len(sources)
        ingested, missing, skipped_unchanged = [], [], []
        rechecked, stale_rules = 0, []
        for i, src in enumerate(sources):
            job["current"] = src["id"]
            job["done"] = i
            pdf_path = src["file"]
            if not os.path.isabs(pdf_path):
                pdf_path = os.path.join(case.dir, pdf_path)
            # A declared document with no file is a gap in the LIBRARY, not a document to
            # catalogue. Collect them and report at the end: one missing PDF must not
            # abort the ingest of the twelve that are present.
            if not os.path.exists(pdf_path):
                missing.append({"doc_id": src["id"],
                                "title": src.get("title", src["id"]),
                                "file": src["file"]})
                continue
            # A2: an unchanged PDF (same bytes as the catalog entry was parsed from) is
            # not re-parsed unless the caller forces it — a docling pass over a 50-page
            # scan costs minutes and gigabytes and can only reproduce what is there.
            prior = cat.get(src["id"]) or {}
            if (not force and prior.get("pdf_sha256")
                    and prior.get("pdf_sha256") == ingest.sha256_file(pdf_path)):
                skipped_unchanged.append(src["id"])
                rechecked += _cited_rule_count(case, src["id"])
                stale_rules += _revalidate_citations(case, cat, [src["id"]], led)
                continue
            entry = ingest.ingest_document(pdf_path, src["id"], src.get("title", src["id"]),
                                           tax, cat, backend=backend)
            led.append("authoring.ingest",
                       {"doc_id": src["id"], "parse_source": entry["parse_source"],
                        "pdf_sha256": entry.get("pdf_sha256", ""),
                        "tags": entry["tags"], "clauses": len(entry["clauses"])},
                       actor="admin")
            # A re-ingest may have moved or replaced the evidence ratified rules cite —
            # re-check them now, not at answer time (TICKETS.md A3).
            rechecked += _cited_rule_count(case, src["id"])
            stale_rules += _revalidate_citations(case, cat, [src["id"]], led)
            # Ingest EXTRACTS and INDEXES; it does NOT draft rules. Drafting the whole MOU
            # up front produced 33 rules per contract to review and an approve-all that
            # could never match one grand total. Rules are now drafted PER SCENARIO, scoped
            # to the clauses a known paystub actually needs (see _draft_scenario). Policy
            # Q&A works from the index immediately; costing rules are authored on demand.
            ingested.append({"doc_id": src["id"], "tags": entry["tags"],
                             "summary": entry["summary"], "clauses": len(entry["clauses"]),
                             "parse_source": entry["parse_source"],
                             "warning": _ingest_warning(entry)})
        # Ingest does NOT touch the review queue — rules are drafted per scenario, and a
        # re-ingest must never wipe drafts already sitting there awaiting approval.
        if missing:
            led.append("authoring.ingest_missing",
                       {"doc_ids": [m["doc_id"] for m in missing]}, actor="admin")
        # RECONCILE (TICKETS.md F4): a document deleted from case.yaml must leave the
        # catalog and the search index too, or the library forever lists (and search
        # forever answers from) a contract the case no longer declares. Uploaded
        # documents are catalog-only by design and are left alone.
        declared = {s["id"] for s in sources}
        for entry in list(cat.documents()):
            did = entry["doc_id"]
            if did in declared or entry.get("uploaded"):
                continue
            cat.remove(did)
            try:
                backend.delete(did)
            except Exception:
                pass
            led.append("authoring.reconciled",
                       {"doc_id": did, "reason": "no longer declared in case.yaml"},
                       actor="admin")
        job["done"] = job["total"]
        job["result"] = {"ingested": ingested, "proposed_rules": [], "missing": missing,
                         "skipped_unchanged": skipped_unchanged,
                         "rules_rechecked": rechecked,
                         "stale_rules": [s["rule_id"] for s in stale_rules]}
        job["status"] = "done"
    except Exception as e:  # surface the failure to the poller
        job["status"] = "error"
        job["error"] = str(e)


@app.post("/admin/ingest")
def admin_ingest(force: bool = False):
    """Kick off async ingestion; returns a job id to poll. See /admin/ingest/status.
    Single-flight with uploads — see _register_job. `?force=true` re-parses PDFs whose
    bytes have not changed since they were last catalogued (A2)."""
    job_id = _register_job(total=0, done=0, current=None, force=force)
    if job_id is None:
        return JSONResponse({"error": "an ingest is already running — poll its status "
                             "or wait for it to finish"}, status_code=409)
    threading.Thread(target=_ingest_worker, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "status": "running"}


@app.get("/admin/ingest/status/{job_id}")
def admin_ingest_status(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return job


def _upload_worker(job_id: str, dest: str, doc_id: str, title: str, fname: str):
    """Runs an uploaded document's ingest off the request path (OCR_TICKETS.md OCR-6):
    a docling parse can take minutes, and the staged job (`stage`: parsing → tagging →
    indexing → done) lets the UI show the extraction happening live instead of a
    request that hangs until timeout."""
    job = _JOBS[job_id]
    try:
        case = _case()
        led = case.ledger()
        cat = _catalog(case)
        entry = ingest.ingest_document(
            dest, doc_id, title, _taxonomy(case), cat, backend=_backend(case),
            progress=lambda stage: job.__setitem__("stage", stage))
        # Uploaded docs are catalog-only (not declared in case.yaml); the marker keeps
        # the post-ingest reconciliation pass from garbage-collecting them.
        entry["uploaded"] = True
        cat.upsert(entry)
        led.append("authoring.upload",
                   {"doc_id": doc_id, "filename": fname,
                    "parse_source": entry["parse_source"],
                    "pdf_sha256": entry.get("pdf_sha256", ""),
                    "tags": entry["tags"], "clauses": len(entry["clauses"])},
                   actor="admin")
        # An upload can replace an existing source file by name — same drift risk as a
        # bulk re-ingest, so ratified rules citing this document are re-checked now.
        _revalidate_citations(case, cat, [doc_id], led)
        # Upload = extract + index, same as bulk ingest. It does NOT draft rules — this
        # was the last surviving bulk-draft path after the scenario-scoped pivot, and it
        # put a whole document's worth of competing rules back into the review queue.
        # Policy Q&A over the new document works immediately; costing rules are
        # authored per scenario.
        job["result"] = {
            "doc_id": doc_id, "title": title, "parse_source": entry["parse_source"],
            "tags": entry["tags"], "summary": entry["summary"],
            "clauses": len(entry["clauses"]), "proposed_rules": [],
            # OCR-7: a freshly scanned upload should announce shaky OCR immediately,
            # with the same signal its scorecard row will carry in the document list.
            "low_confidence_pages": _extraction_stats(entry)["low_confidence_pages"],
            "warning": _ingest_warning(entry),
            "note": ("Document read and indexed — ask about it in chat right away. "
                     "Costing rules are drafted per scenario on the Verification tab.")}
        job["stage"] = "done"
        job["status"] = "done"
    except Exception as e:  # surface the failure to the poller
        job["status"] = "error"
        job["error"] = str(e)


@app.post("/admin/upload")
async def admin_upload(file: UploadFile = File(...)):
    """Upload a PDF: save it into the case, then ingest it ASYNC (parse -> tag ->
    index) under a staged job — poll /admin/ingest/status/{job_id} (OCR-6). Saving
    and validation stay in-request so a bad file fails fast with a real status code."""
    case = _case()
    src_dir = os.path.join(case.dir, "sources")
    os.makedirs(src_dir, exist_ok=True)
    fname = os.path.basename(file.filename or "upload.pdf")
    if not fname.lower().endswith(".pdf"):
        return JSONResponse({"error": "only .pdf files are accepted"}, status_code=400)
    doc_id = _slug(os.path.splitext(fname)[0])
    title = os.path.splitext(fname)[0]

    # Claim the single-flight slot BEFORE touching the destination file: an upload can
    # replace a source PDF by name, and doing that while a running ingest is mid-parse
    # on the same file would bind the catalog to bytes that no longer exist.
    job_id = _register_job(stage="saving", doc_id=doc_id, filename=fname)
    if job_id is None:
        return JSONResponse({"error": "an ingest is already running — wait for it to "
                             "finish before uploading"}, status_code=409)

    def _reject(resp: JSONResponse) -> JSONResponse:
        _JOBS.pop(job_id, None)  # a rejected upload never ran — leave no ghost job
        return resp

    # Stream to a temp file with a hard size cap (TICKETS.md C5): reading the whole
    # upload into RAM let any credential holder OOM the 2GB instance, and writing the
    # destination directly could leave a half-written PDF over a good one.
    dest = os.path.join(src_dir, fname)
    tmp = dest + ".uploading"
    size = 0
    try:
        with open(tmp, "wb") as f:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    return _reject(JSONResponse(
                        {"error": f"file exceeds the "
                                  f"{_MAX_UPLOAD_BYTES // (1 << 20)}MB upload limit"},
                        status_code=413))
                f.write(chunk)
        with open(tmp, "rb") as f:
            if f.read(5) != b"%PDF-":
                return _reject(JSONResponse({"error": "not a PDF (bad magic bytes)"},
                                            status_code=400))
        os.replace(tmp, dest)
    except Exception:
        _JOBS.pop(job_id, None)
        raise
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    _JOBS[job_id]["stage"] = "parsing"
    threading.Thread(target=_upload_worker, args=(job_id, dest, doc_id, title, fname),
                     daemon=True).start()
    return {"job_id": job_id, "status": "running", "doc_id": doc_id, "filename": fname}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "doc"


@app.get("/admin/catalog")
def admin_catalog():
    """Catalog + the declared governance metadata, so the UI can show a document's
    identity hierarchically (department > unit > type > version > effective dates)
    rather than a flat bag of auto-tags."""
    case = _case()
    out = []
    for d in _catalog(case).summaries():
        src = case.source_by_id(d["doc_id"]) or {}
        out.append({**d,
                    "declared_title": src.get("title", ""),
                    "department": src.get("department"),
                    "bargaining_unit": src.get("bargaining_unit"),
                    "doc_type": src.get("doc_type"),
                    "mou_version": src.get("mou_version"),
                    "effective_start": src.get("effective_start"),
                    "effective_end": src.get("effective_end"),
                    "supersedes": src.get("supersedes")})
    return {"documents": out}


@app.get("/admin/proposed")
def admin_proposed():
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


def _check_golden(case, rule_dicts: list[dict], golden: dict) -> tuple[bool, dict]:
    """Run the candidate rules against one known answer (analyst-derived on the shipped
    case; a real paystub once the district supplies one).

    Returns (ok, detail). `detail["status"]` is one of:
      "pass"    — reproduces the known answer.
      "fail"    — produces a DIFFERENT number. Blocks. This is what the gate is for.
      "pending" — produces no number at all: no rule in this library covers the
                  scenario. NOT a failure, and must not block.

    The pending/fail distinction is load-bearing. A golden covers one bargaining unit;
    a corpus has several. Treating "no rule covers this yet" as a failure meant approving
    the police rules was blocked by the public-works golden, and every corpus had to be
    ratified in one atomic all-or-nothing action — which is precisely the workflow the
    review queue exists to avoid. A library that cannot answer a scenario REFUSES at run
    time, which is safe; a library that answers it wrongly is the actual danger. Only the
    latter blocks. Completeness is reported by the Verification tab, not enforced here.
    """
    try:
        rules = case.assign_scope(
            [Rule.from_dict({**r, "status": "ratified", "approver": "candidate"})
             for r in rule_dicts])
        names = set(golden.get("subjects") or [])
        subs = [s for s in case.subjects() if not names or s.get("name") in names]
        # Mirror run time: governance narrows rules to the document(s) that actually
        # govern these subjects, so rules from other units' MOUs can't contaminate the
        # check (a corpus has many MOUs; only one governs a given employee).
        units = sorted({s.get("bargaining_unit") for s in subs if s.get("bargaining_unit")})
        # A golden verifies ONE kind of answer. Only rules of that result_type compete,
        # mirroring run time (a "5 days" bereavement selector must not hijack a money
        # golden). Pay is one chapter of the rulebook, not all of it. The type filter
        # runs BEFORE supersession, in the same order as chat (see the costing path), so
        # an amendment contributing only a non-currency rule cannot trigger supersession
        # here and not there (E2).
        want = golden.get("result_type", "currency")
        rules = [r for r in rules if r.result_type == want]
        if units:
            gov = governance.resolve(units, golden.get("params", {}).get("date_iso"),
                                     case.manifest.get("sources", []))
            if gov.resolved:
                rules = [r for r in rules
                         if (not r.citation.doc_id) or (r.citation.doc_id in gov.doc_ids)]
                rules, _ = governance.apply_supersession(
                    rules, case.manifest.get("sources", []), gov.doc_ids)
        # Seed every declared query param so a rule referencing a legitimate fact the
        # golden didn't set (e.g. date_iso) evaluates to a safe default instead of
        # exploding. Mirrors run time, where chat always supplies all of them.
        params = {"hours": 0.0, "date": "", "date_iso": "", "holiday_weekday": ""}
        params.update(queryfacts.query_defaults(case))  # J1a: every declared question fact
        params.update(golden.get("params", {}))
        # A currency scenario is a shift cost — same basis filter as run time, so approving
        # a whole MOU (uniform allowance, medical, life insurance) does not blow the check
        # up with a year of benefits. Non-currency scenarios (days/hours entitlements) are
        # not periodic pay and are not filtered.
        scope = SHIFT_BASES if want == "currency" else None
        res = calculate(params, subs, rules, case.rounding_places(), basis_scope=scope)
        expected = float(golden["expected_total"])
        actual = float(res.total)
        ok = abs(actual - expected) < 0.005
        # A wrong number is only a FAILURE when the scenario is fully authored. If a
        # governing document contributes no rules yet (a side letter whose replacement
        # rule nobody has drafted), the number is computed from an incomplete library —
        # that is "pending", not "the rules are wrong". Observed live: ratifying the 2025
        # base-MOU rules made the 2026 side-letter scenario read FAIL at 4388.80, because
        # the 6.5% amendment rule did not exist yet. Fail must mean exactly one thing —
        # authored rules that disagree with the paystub — or the Verification tab trains
        # people to ignore red.
        unauthored = []
        if not ok and units and gov.resolved:
            covered = {r.citation.doc_id for r in rules}
            unauthored = [d for d in gov.doc_ids if d not in covered]
        status = "pass" if ok else ("pending" if unauthored else "fail")
        detail = {"scenario": golden.get("name", "golden"), "expected": expected,
                  "actual": actual, "status": status,
                  "per_subject": {li.subject: li.total for li in res.line_items},
                  # Which selector actually WON for each subject. On a failure this is the
                  # actionable fact: "compare the rule against its clause" is only useful
                  # if the reviewer knows WHICH rule fired.
                  "chosen": sorted({li.rule_id for li in res.line_items}),
                  # EVERY rule that contributed to the number — the winning base plus any
                  # differential or premium that fired. Coverage ("which known answer
                  # proves this rule?") is computed from this, so a live premium that
                  # never fires is reported as unproven instead of hiding behind the base
                  # that won (E1).
                  "fired": sorted({t.rule_id for li in res.line_items for t in li.trace
                                   if t.kind in ("modifier", "selector-chosen", "premium")})}
        if unauthored:
            detail["note"] = (f"Not fully authored: {', '.join(unauthored)} governs this "
                              f"scenario but has no approved rules yet. Draft the rules "
                              f"for this scenario to complete it.")
        return (ok or bool(unauthored)), detail
    except NoRuleApplies as e:
        # Nothing in this library covers the scenario. Run time would refuse, which is
        # the correct and safe outcome — so this cannot block a ratification.
        return True, {"scenario": golden.get("name", "golden"), "status": "pending",
                      "expected": golden.get("expected_total"), "actual": None,
                      "error": str(e),
                      "note": "No live rule covers this scenario yet, so Kenny refuses "
                              "to cost it rather than guessing. Approve the rules for "
                              "this unit and this check will run."}
    except Exception as e:
        return False, {"scenario": golden.get("name", "golden"), "status": "fail",
                       "error": str(e),
                       "expected": golden.get("expected_total"), "actual": None}


def _coverage(case, rule_dicts: list[dict]) -> tuple[dict, dict]:
    """(statuses, proved_by) for a candidate library.

    statuses[name]   the _check_golden detail for each known answer.
    proved_by[id]    the names of the PASSING known answers in which rule `id` actually
                     fired. A rule absent from proved_by is proven by nothing: no known
                     answer exercises it, so nothing says it is right (E1).
    """
    statuses: dict[str, dict] = {}
    proved: dict[str, list[str]] = {}
    for g in case.golden_cases():
        _, d = _check_golden(case, rule_dicts, g)
        statuses[g["name"]] = d
        if d.get("status") == "pass":
            for rid in d.get("fired") or []:
                proved.setdefault(rid, []).append(g["name"])
    return statuses, proved


def _gate_verdict(case, selected: list[dict], approver: str, led=None,
                  attempt: str | None = None) -> dict:
    """The known-answer gate, as one decision (E1). Used by /admin/ratify and, with
    `attempt='mutation-test'`, by /admin/try_break so a deliberate error is refused in
    the gate's own words.

    Judges the live library MERGED BY ID with `selected`, before and after. All-or-nothing:
      R1  a known answer is 'fail' after and was not 'fail' before   (pass/pending -> fail)
      R2  a known answer was 'pass' before and is not 'pass' after   (pass -> pending)
      R3  a `_scenario`-tagged target must pass
      R4  every selected rule must fire in a passing known answer   (coverage)
      R5  a live rule outside the selection that was proven before must still be proven
    'Pending' elsewhere (a unit nobody has authored yet) never blocks: incremental,
    unit-by-unit authoring must stay possible. Completeness is the Verification tab's job.

    Returns {"approved": bool, "response": <the HTTP body when blocked>, "proved_by": {...},
             "checks": [detail...]}. Ledger events are written only when `led` is given;
             a mutation test passes led=None and records one summary event itself.
    """
    def _block(reason: str, response: dict, **extra) -> dict:
        if led is not None:
            led.append("authoring.blocked",
                       {"reason": reason, "approver": approver,
                        **({"attempt": attempt} if attempt else {}), **extra},
                       actor="admin")
        return {"approved": False, "response": {"ratified": [], **response},
                "proved_by": {}, "checks": checks, "ledger_checks": ledger_checks}

    checks: list[dict] = []
    ledger_checks: list[tuple[int | None, dict]] = []   # (golden_check seq, detail) for F2
    before_st, proved_before = _coverage(case, _live_dicts(case))
    before = {n: d.get("status") for n, d in before_st.items()}
    candidate = _with_live(case, selected)
    after_st, proved_after = _coverage(case, candidate)
    targets = {r.get("_scenario") for r in selected if r.get("_scenario")}
    for golden in case.golden_cases():
        name = golden["name"]
        detail = after_st[name]
        status = detail.get("status")
        ok = status in ("pass", "pending")
        checks.append(detail)
        if led is not None:
            gev = led.append("authoring.golden_check", {"passed": ok, **detail}, actor="admin")
            ledger_checks.append((gev["seq"], detail))
        was = before.get(name)
        # R1: a wrong number where there was a right one, or none.
        if status == "fail" and was != "fail":
            if was == "pass":
                why = (f"this would BREAK a check that was passing. “{name}” dropped "
                       f"to {detail.get('actual')} (known answer {detail.get('expected')}). "
                       f"Remove the rule that changed it.")
            else:
                why = (f"“{name}” must come to {detail.get('expected')}, but these "
                       f"rules produce {detail.get('actual')}. Compare each rule against "
                       f"its clause.")
            return _block("known answer fails", {"golden_failed": detail,
                                                 "warning": "Nothing was approved — " + why},
                          scenario=name, was=was, now=status)
        # R2: a known answer that reproduced would no longer be answered at all.
        if was == "pass" and status != "pass":
            return _block("uncovers a known answer",
                          {"golden_failed": detail,
                           "warning": f"Nothing was approved — “{name}” was "
                                      f"reproduced and would no longer be answered at all. "
                                      f"A rule you selected replaces the one that proved it."},
                          scenario=name, was=was, now=status)
        # R3: the scenario these rules are FOR must actually reproduce its answer.
        if name in targets and status != "pass":
            actual = detail.get("actual")
            fired = detail.get("chosen") or detail.get("fired") or []
            how = (f"could not be computed ({detail.get('error', 'unknown')})"
                   if actual is None else
                   f"produced {actual}" + (f" — the rule that fired was “{fired[0]}”"
                                           if fired else ""))
            return _block("target scenario did not reproduce",
                          {"golden_failed": detail,
                           "warning": f"Nothing was approved — “{name}” must come "
                                      f"to {detail.get('expected')}, but the drafted rules "
                                      f"{how}. Compare each rule against its clause, or "
                                      f"re-draft the scenario."},
                          scenario=name, was=was, now=status)
    # R4 / R5: coverage. A rule nothing proves cannot go live, and approving must not
    # leave a previously-proven live rule proven by nothing.
    sel_ids = [r.get("id") for r in selected]
    uncovered = [i for i in sel_ids if i not in proved_after]
    orphaned = sorted(i for i in proved_before if i not in proved_after and i not in sel_ids)
    if uncovered or orphaned:
        return _block("not exercised by any known answer",
                      {"uncovered": uncovered, "orphaned": orphaned,
                       "warning": "Nothing was approved — no known answer exercises: "
                                  + ", ".join(uncovered + orphaned)
                                  + ". A rule nothing proves cannot go live. Add a known "
                                    "answer that uses it, or untick it."},
                      uncovered=uncovered, orphaned=orphaned)
    return {"approved": True, "response": None,
            "proved_by": {i: proved_after[i] for i in sel_ids}, "checks": checks,
            "ledger_checks": ledger_checks}


def _extraction_stats(entry: dict) -> dict:
    """How well did we READ this document? (OCR_TICKETS.md OCR-3)

    Pure math over one catalog entry — separated from the endpoint so the scorecard
    arithmetic is testable without the HTTP stack. Returns:
      kinds           histogram of clause kinds (the catalog omits `kind` for normal
                      text, so missing/empty counts as "text" — same normalisation as
                      the X-ray endpoint)
      pages_empty     pages in 1..page_count from which nothing was extracted
      recovered_pages pages whose every clause is recovered-* — the layout-model
                      rescue (docling called the page a Picture; spans were regrouped
                      into rows from raw geometry)
      pdf_sha256_short first 12 hex chars of the PDF's sha ("" when not recorded)
      index_error     passthrough — catalogued but absent from search when set
      low_confidence_pages  pages whose docling OCR score fell below
                      LOW_OCR_CONFIDENCE (OCR-7) — [] for digital documents and for
                      catalogs ingested before confidence capture existed
    """
    kinds: dict[str, int] = {}
    kinds_by_page: dict[int, set] = {}
    for c in entry.get("clauses") or []:
        kind = c.get("kind") or "text"
        kinds[kind] = kinds.get(kind, 0) + 1
        page = c.get("page")
        if isinstance(page, int) and page >= 1:
            kinds_by_page.setdefault(page, set()).add(kind)
    page_count = entry.get("page_count") or 0
    return {
        "kinds": kinds,
        "pages_empty": [p for p in range(1, page_count + 1) if p not in kinds_by_page],
        "recovered_pages": sorted(
            p for p, ks in kinds_by_page.items()
            if ks <= {"recovered-row", "recovered-text"}),
        "pdf_sha256_short": (entry.get("pdf_sha256") or "")[:12],
        "index_error": entry.get("index_error"),
        "low_confidence_pages": sorted(
            int(p) for p, s in (entry.get("page_confidence") or {}).items()
            if isinstance(s, (int, float)) and s < ingest.LOW_OCR_CONFIDENCE),
    }


@app.get("/admin/coverage")
def admin_coverage():
    """How much of each contract has Kenny actually modelled?

    The question an HR Director asks, which a "64 rules drafted" counter cannot answer.
    Per document: clauses extracted, rules live, rules pending review, clauses blocked
    on missing data, clauses that are narrative (nothing to compute, correctly).
    """
    case = _case()
    cat = _catalog(case)
    ratified = case.rules()
    proposed = admin_proposed()
    live_ids = {r.id for r in ratified}
    live_by_doc: dict[str, int] = {}
    for r in ratified:
        live_by_doc[r.citation.doc_id] = live_by_doc.get(r.citation.doc_id, 0) + 1
    pending_by_doc: dict[str, int] = {}
    for r in proposed.get("rules", []):
        if r.get("id") in live_ids:
            continue
        d = (r.get("citation") or {}).get("doc_id") or r.get("_doc_id") or ""
        pending_by_doc[d] = pending_by_doc.get(d, 0) + 1
    blocked_by_doc: dict[str, int] = {}
    narrative_by_doc: dict[str, int] = {}
    for n in proposed.get("needs_data", []):
        d = n.get("doc_id") or ""
        if n.get("category") == "narrative":
            narrative_by_doc[d] = narrative_by_doc.get(d, 0) + 1
        else:
            blocked_by_doc[d] = blocked_by_doc.get(d, 0) + 1

    docs = []
    for entry in cat.documents():
        did = entry["doc_id"]
        src = case.source_by_id(did) or {}
        docs.append({
            # The declared name is what every surface calls the document (C4); the
            # cover's own heading travels beside it as `extracted_title`.
            "doc_id": did,
            "title": _display_title(case, cat, did),
            "extracted_title": _cover_title(entry),
            "declared_title": src.get("title", ""),
            "department": src.get("department"), "doc_type": src.get("doc_type"),
            "bargaining_unit": src.get("bargaining_unit"),
            "mou_version": src.get("mou_version"),
            "effective_start": src.get("effective_start"),
            "effective_end": src.get("effective_end"),
            "supersedes": src.get("supersedes"),
            "summary": entry.get("summary", ""), "tags": entry.get("tags", []),
            "clauses": len(entry.get("clauses", [])),
            "parse_source": entry.get("parse_source"),
            **_ocr_doc_fields(entry),  # D1 text origin + D2 disputed-cell pages (ocr chunk)
            "live": live_by_doc.get(did, 0),
            "pending": pending_by_doc.get(did, 0),
            "blocked": blocked_by_doc.get(did, 0),
            "narrative": narrative_by_doc.get(did, 0),
            # Extraction scorecard (OCR-3): how well was the document READ, as
            # opposed to how much of it has been modelled.
            **_extraction_stats(entry),
        })
    return {"documents": docs, "corpus_size": len(case.manifest.get("sources", [])),
            "showcase": _showcase(case)}  # D4 Compare deep link (ocr chunk)


# A gap is only actionable as a FIELD, not as 31 separate clause rows. The LLM's
# "missing" text is prose, so normalise it to the field a data owner would add.
_GAP_FIELDS = [
    ("subject_assignment", ("detective", "investigator", "motorcycle", "swat",
                            "special response", "assignment to", "bureau")),
    ("subject_k9_handler", ("k-9", "k9", "canine")),
    ("subject_paramedic", ("paramedic", "medic unit")),
    ("event", ("holdover", "callback", "call-back", "standby", "court", "recall",
               "mandated to work", "held over", "event")),
    ("subject_step", ("step",)),
    ("subject_seniority_years", ("years of service", "seniority", "length of service")),
    ("subject_acting_assignment", ("acting",)),
    ("subject_hire_date", ("hire date", "appointment date", "probation")),
    ("subject_separation_date", ("separation", "retire")),
    ("subject_coverage_tier", ("coverage tier", "medical plan", "dependent")),
]


def _gap_field(text: str) -> str:
    t = (text or "").lower()
    import re as _re
    m = _re.search(r"subject_[a-z0-9_]+", t)
    if m:
        return m.group(0)
    for field, cues in _GAP_FIELDS:
        if any(c in t for c in cues):
            return field
    return "other"


@app.get("/admin/gaps")
def admin_gaps():
    """Data gaps grouped by the FIELD required — a spec a data owner can act on
    ("add `assignment` and these four premiums start working"), not a clause list."""
    case = _case()
    proposed = admin_proposed()
    groups: dict[str, dict] = {}
    for n in proposed.get("needs_data", []):
        if n.get("category") == "narrative":
            continue
        key = n.get("missing_field") or _gap_field(f"{n.get('missing','')} {n.get('reason','')}")
        g = groups.setdefault(key, {"field": key, "unlocks": []})
        src = case.source_by_id(n.get("doc_id", "")) or {}
        g["unlocks"].append({"doc_id": n.get("doc_id"), "clause": n.get("clause"),
                             "title": src.get("title", n.get("doc_id")),
                             "department": src.get("department"),
                             "reason": n.get("reason", "")})
    # Named fields first (they are the actionable ones); the unclassified catch-all
    # sorts last however big it is — "add `other`" is not a request anyone can action.
    out = sorted(groups.values(),
                 key=lambda g: (g["field"] == "other", -len(g["unlocks"])))
    return {"gaps": out, "total_clauses_blocked": sum(len(g["unlocks"]) for g in out)}


def _scenario_query(scenario: dict) -> str:
    """Retrieval query for the pay branch a scenario exercises. An explicit `branch_query`
    on the scenario wins; otherwise fall back to its name plus generic pay vocabulary."""
    if scenario.get("branch_query"):
        return scenario["branch_query"]
    base = "pay premium rate differential hours worked base"
    return f"{scenario.get('name', '')} {base}"


@app.post("/admin/draft_scenario")
async def admin_draft_scenario(request: Request):
    """Draft ONLY the rules a known scenario (a paystub) needs, and verify them.

    This is the rebuilt authoring model. Instead of drafting a whole 140-clause MOU into
    33 rules and hoping approve-all matches one grand total, a scenario names a real
    known answer; Kenny retrieves the handful of clauses that answer it, drafts just
    those, and checks them against the paystub. Review collapses from ~33 rules to ~5,
    and a small focused drafting task is one the model gets right (roles, pay_basis,
    compounding differentials) where the whole-MOU task was fragile.
    """
    body = await request.json()
    name = body.get("scenario")
    case = _case()
    led = case.ledger()
    scenario = next((g for g in case.golden_cases() if g.get("name") == name), None)
    if scenario is None:
        return JSONResponse({"error": f"no scenario named {name!r}"}, status_code=404)

    # 1. Which document(s) govern the scenario's people on its date.
    subs = [s for s in case.subjects() if s["name"] in set(scenario.get("subjects") or [])]
    units = sorted({s.get("bargaining_unit") for s in subs if s.get("bargaining_unit")})
    gov = governance.resolve(units, scenario.get("params", {}).get("date_iso"),
                             case.manifest.get("sources", []))
    doc_ids = gov.doc_ids or None

    # 2. Retrieve only the clauses this scenario's pay branch needs.
    cat = _catalog(case)
    ingested = {d["doc_id"] for d in cat.documents()}
    scope = [d for d in (doc_ids or list(ingested)) if d in ingested]
    if not scope:
        return JSONResponse({"error": "governing documents are not ingested yet — run "
                             "Ingest first"}, status_code=400)
    hits = _backend(case).search(_scenario_query(scenario), doc_ids=scope, k=8)
    want = {(h["doc_id"], str(h["clause"])) for h in hits}
    # Never re-draft a clause that already has a LIVE rule. A scenario completes the
    # library incrementally: the 2026 side-letter scenario needs ONE new rule (the 6.5%
    # amendment), not a re-draft of the base rules the 2025 scenario already ratified —
    # a re-draft carries the same ids, would overwrite the approved rules on ratify, and
    # the regression guard then (correctly) blocks the whole approval. Draft only the gap.
    live = {(r.citation.doc_id, str(r.citation.clause)) for r in case.rules()}
    want -= live
    clauses_by_doc: dict[str, list] = {}
    for d, cl in want:
        for c in cat.clauses(d):
            if str(c.get("clause")) == cl:
                clauses_by_doc.setdefault(d, []).append(c)

    # 3. Draft ONLY those clauses (per document, so ids namespace correctly). The drafter
    #    also reports clauses it correctly REFUSED to draft (event pay the roster cannot
    #    see) — that is the Data-gaps feed, kept per scenario.
    drafted: list[dict] = []
    needs: list[dict] = []
    with llm.record() as trail:
        for d, clauses in clauses_by_doc.items():
            rules = await run_in_threadpool(llm.draft_rules, clauses, d, case.known_facts(),
                                            case.field_values(), case.bool_facts())
            doc_sha = (cat.get(d) or {}).get("pdf_sha256", "")
            for r in rules:
                r["_doc_id"] = d
                if not str(r.get("id", "")).startswith(d + ":"):
                    r["id"] = f"{d}:{r.get('id')}"
                # Bind the citation to the exact file the reviewer will be shown
                # (TICKETS.md A1): if the PDF later changes, the mismatch is detectable.
                if doc_sha:
                    cit = r.get("citation") or {}
                    cit["doc_sha256"] = doc_sha
                    r["citation"] = cit
            drafted.extend(rules)
            needs.extend(getattr(llm.draft_rules, "last_needs_data", []) or [])
    for call in trail:
        led.append("llm.call", {**call, "scenario": name}, actor="llm")

    # Keep only rules of the scenario's own type — a currency paystub authors currency
    # rules, not the vacation-accrual clause that happened to sit near a holiday clause.
    # Cross-type clauses that surfaced in retrieval are answered by policy Q&A, not costed.
    want_type = scenario.get("result_type", "currency")
    drafted = [r for r in drafted if r.get("result_type", "currency") == want_type]

    # 4. Verify the drafted set against the paystub, and only offer it if it reproduces.
    ok, detail = _check_golden(case, _with_live(case, drafted), scenario)

    # 5. Merge into the review queue (replace this scenario's prior drafts + gaps).
    prior = admin_proposed()
    keep = [r for r in prior.get("rules", []) if r.get("_scenario") != name]
    keep_needs = [n for n in prior.get("needs_data", []) if n.get("_scenario") != name]
    for r in drafted:
        r["_scenario"] = name
    for n in needs:
        n["_scenario"] = name
    _write_proposed(case, keep + drafted, keep_needs + needs)
    led.append("authoring.draft_scenario",
               {"scenario": name, "clauses": sorted(f"{d}:{c}" for d, c in want),
                "rule_ids": [r["id"] for r in drafted], "verify": detail.get("status")},
               actor="admin")

    return {"scenario": name, "considered_clauses": sorted(f"{d}§{c}" for d, c in want),
            "drafted": drafted, "verify": detail}


@app.get("/admin/verification")
def admin_verification():
    """The known answers, what each computes, and which live rules each one proves.

    Ratification is only meaningful because the library must reproduce a known answer —
    so the known answers belong in the UI, not just in case.yaml. `proved_by` maps each
    live rule id to the PASSING known answers in which it fired; `unverified` lists the
    live rules that fired in none. Each rule is checked only at its known answer's
    inputs — that is what "proved" means here, no more (E1, E7).
    """
    case = _case()
    rules = case.rules()
    goldens = case.golden_cases()
    ingested = {d["doc_id"] for d in _catalog(case).documents()}
    results = []
    live = _live_dicts(case)
    statuses, proved = _coverage(case, live)
    exercised = set(proved)
    for g in goldens:
        detail = statuses[g["name"]]
        ok = detail.get("status") in ("pass", "pending")
        want = g.get("result_type", "currency")
        # Which documents this scenario needs, and whether each is ingested yet — so the
        # card can say "needs X (not ingested)" instead of only failing on the button.
        subs = [s for s in case.subjects() if s["name"] in set(g.get("subjects") or [])]
        units = sorted({s.get("bargaining_unit") for s in subs if s.get("bargaining_unit")})
        gov = governance.resolve(units, g.get("params", {}).get("date_iso"),
                                 case.manifest.get("sources", []))
        needs_docs = [{"doc_id": d, "ingested": d in ingested,
                       "title": (case.source_by_id(d) or {}).get("title", d)}
                      for d in (gov.doc_ids or [])]
        results.append({"name": g.get("name"), "result_type": want,
                        "source": g.get("source", ""),
                        "expected": detail.get("expected"), "actual": detail.get("actual"),
                        "passed": ok, "status": detail.get("status", "fail"),
                        "per_subject": detail.get("per_subject"),
                        # every rule that contributed to this number (base + modifiers +
                        # premiums), so the card can say which rules it exercises
                        "fired": detail.get("fired") or [],
                        "note": detail.get("note"),
                        "error": detail.get("error"),
                        "needs_docs": needs_docs,
                        "ready": all(d["ingested"] for d in needs_docs) and bool(needs_docs)})
    unverified = [{"id": r.id, "result_type": r.result_type, "topic": r.topic,
                   "doc_id": r.citation.doc_id, "clause": r.citation.clause}
                  for r in rules if r.id not in exercised]
    # "Pending" is not passing — a scenario nothing covers is unproven, not proven. It
    # simply does not BLOCK approval (see _check_golden).
    return {"goldens": results, "rule_count": len(rules),
            "unverified": unverified, "proved_by": proved,
            "pending": sum(1 for g in results if g["status"] == "pending"),
            "all_passing": (all(g["status"] == "pass" for g in results)
                            if results else None)}


@app.get("/admin/clause")
def admin_clause(doc_id: str, clause: str = "", page: int | None = None,
                 bbox: str = ""):
    """The source text + bbox behind a rule's citation, so a reviewer can check the
    drafted rule against the actual contract language before approving it.

    A numbered contract resolves by clause number. But an OCR'd document has NO clause
    numbers — the rule cites a page and a bbox instead, and matching by clause returned
    404, making View source unusable on exactly the real-world (scanned) documents. So
    when the clause does not resolve, fall back to the page: return the text of the
    clauses on that page and let the citation's own bbox draw the highlight.

    Evidence gate (D2): a citation that lands on a table row whose numeric cells are
    not all verified is refused with 409 and the cells in question — the stored OCR
    text of a disputed cell is not evidence until a person confirms it in Compare.
    The row is found by clause label, or by `bbox` ("l,t,r,b" in PDF points, the
    citation's own box) on the given page. The same check is exposed to the
    ratify/draft path as cellcheck.disputed_for_citation.
    """
    case = _case()
    cat = _catalog(case)
    clauses = cat.clauses(doc_id)
    cit_bbox = _parse_bbox(bbox)
    disputed = cellcheck.disputed_for_citation(case.dir, cat, doc_id, clause=clause,
                                               page=page, bbox=cit_bbox)
    if disputed:
        return JSONResponse({"error": "cited cell is disputed, confirm it in Compare first",
                             "doc_id": doc_id, "clause": clause, "page": page,
                             "cells": disputed}, status_code=409)
    for c in clauses:
        if clause and str(c.get("clause")) == str(clause):
            return {"doc_id": doc_id, "clause": clause, "page": c.get("page"),
                    "bbox": c.get("bbox"), "text": c.get("text", "")}
    if page is not None:
        # Page-level context text; bbox is left null on purpose so the caller draws the
        # highlight from the RULE's own citation bbox (the exact cited passage), not the
        # first clause that happens to sit on the page.
        on_page = [c for c in clauses if c.get("page") == page]
        text = "\n\n".join((c.get("text") or "").strip() for c in on_page)[:1500]
        return {"doc_id": doc_id, "clause": clause, "page": page,
                "bbox": None, "text": text, "resolved_by": "page"}
    return JSONResponse({"error": f"clause {clause!r} not found in {doc_id}"},
                        status_code=404)


@app.get("/admin/validate")
def admin_validate():
    """Static validation of the current proposed rules against the case's real fact
    vocabulary. This is what the ratify gate enforces (PRD §8A)."""
    case = _case()
    proposed = admin_proposed().get("rules", [])
    errors = validate_rules(proposed, case.known_facts())
    return {"known_facts": sorted(case.known_facts()),
            "rule_count": len(proposed), "errors": errors,
            "valid_ids": [r.get("id") for r in proposed if r.get("id") not in errors]}


@app.post("/admin/ratify")
async def admin_ratify(request: Request):
    """Human gate: approve proposed rules -> ratified library (PRD 5.2 / §8A).

    A rule may only be ratified if it PASSES VALIDATION — its expressions must parse
    and may only reference facts that actually exist in the case's data schema. A
    human approving a broken rule is still blocked; approval is necessary, not
    sufficient.
    """
    body = await request.json()
    approver = (body.get("approver") or "").strip()
    if not approver:  # F2: a record of who approved must name someone
        return {"ratified": [], "warning": "Nothing was approved — enter the approver's "
                "name first. The approval record names the person who made it."}
    approved_ids = body.get("rule_ids")  # None -> approve all
    case = _case()
    led = case.ledger()
    proposed = admin_proposed().get("rules", [])

    selected = [r for r in proposed
                if approved_ids is None or r.get("id") in approved_ids]
    # A denial is an audit event too — record what the reviewer rejected and why.
    if approved_ids is not None:
        denied = [r.get("id") for r in proposed if r.get("id") not in approved_ids]
        if denied:
            led.append("authoring.denied",
                       {"rule_ids": denied, "approver": approver,
                        "reason": body.get("deny_reason", "not approved by reviewer")},
                       actor="admin")
    errors = validate_rules(selected, case.known_facts())
    if errors:
        led.append("authoring.rejected",
                   {"errors": errors, "approver": approver}, actor="admin")
        return {"ratified": [], "rejected": errors,
                "warning": "Validation failed — nothing was ratified. These rules "
                           "reference facts that don't exist or can't execute. Fix the "
                           "rule (or the data schema) and re-approve."}

    # KNOWN-ANSWER GATE (E1) — see _gate_verdict for the five rules. A selection is
    # approved as a whole or not at all: no known answer may come out wrong, none that
    # reproduced may stop reproducing, the scenario a draft was made for must pass, and
    # every selected rule must fire in a passing known answer. A unit nobody has authored
    # yet stays 'pending' and never blocks an unrelated approval.
    if not selected:
        # Never let an empty/failed draft wipe a working ratified library.
        return {"ratified": [], "warning": "No proposed rules to ratify — the existing "
                "ratified library was left untouched."}
    verdict = _gate_verdict(case, selected, approver, led)
    if not verdict["approved"]:
        return verdict["response"]
    proved_by = verdict["proved_by"]
    checks = verdict["ledger_checks"]   # (golden_check seq, detail) — cited by F2 approvals

    import time
    newly = []
    for r in selected:
        r = {k: v for k, v in r.items() if k != "_doc_id"}
        r["status"] = "ratified"
        r["approver"] = approver
        r["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        newly.append(r)

    # Ratification ADDS to the library, it does not replace it. Merge the existing live
    # rules with the newly-approved ones, keyed by id (a re-approval updates in place).
    # Writing only the new selection silently dropped every previously-ratified rule:
    # approve A -> library {A}; approve B,C -> library {B,C}, and A was gone. The gate
    # above already judges the UNION (_live_dicts + selected), so the write must persist
    # that same union or the gate and the library disagree.
    existing = _raw_ratified(case)
    merged = {r["id"]: r for r in existing}
    for r in newly:
        merged[r["id"]] = r
    library = list(merged.values())

    # Full approval record: rule text + fingerprint, clause, source hash, known answers
    # (F2) plus the gate's proved_by (E1). Appended first, then approval:{seq, hash} is
    # stamped on the library entry.
    approvals.record_approvals(case, led, newly, approver, checks,
                               extra_by_rule={r.get("id"): {"proved_by": proved_by.get(r.get("id"), [])}
                                              for r in newly})
    _write_ratified(case, library)
    return {"ratified": [r.get("id") for r in newly],
            "library_size": len(library),
            "proved_by": {r.get("id"): proved_by.get(r.get("id"), []) for r in newly}}


@app.get("/admin/ledger")
def admin_ledger():
    led = _case().ledger()
    d = led.verify_detail()
    events = list(led.read()) if d["reason"] != "unreadable_line" else []
    return {"verified": d["ok"], "verify_message": d["message"], "events": events,
            "count": len(events), "head": led.head() if events else None,
            "keyed": d["keyed"], "failed_seq": d["failed_seq"], "reason": d["reason"]}


@app.get("/admin/ledger/export")
def admin_ledger_export():
    return PlainTextResponse(_case().ledger().export(), media_type="text/plain")


@app.get("/admin/history")
def admin_history():
    return {"history": audit.history(_case().ledger())}


@app.get("/admin/taxonomy")
def admin_taxonomy():
    case = _case()
    tax = _taxonomy(case)
    proposed = []
    for d in _catalog(case).documents():
        proposed.extend(d.get("proposed_tags", []))
    return {"taxonomy": tax, "proposed_tags": sorted(set(proposed))}


# --------------------------------------------------------------------------- #
def _write_proposed(case, rules: list[dict], needs_data: list[dict] | None = None) -> None:
    import json
    path = case.path("proposed_rules", "rules/rules_proposed.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"rules": rules, "needs_data": needs_data or []}, f, indent=2)


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
    import json
    import shutil
    import time as _t
    path = case.path("rules")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        stamp = _t.strftime("%Y%m%d-%H%M%S")
        shutil.copy(path, f"{path}.{stamp}.bak")
    with open(path, "w") as f:
        json.dump({"rules": rules}, f, indent=2)


# --- ids-stale-warm ---
# A1 (server-issued ids), A2 (evidence-based stale check), A5 (startup warm-up).
def _cited_rule_count(case, doc_id: str) -> int:
    """How many live rules cite a document — what a re-read re-checks (A2)."""
    return sum(1 for r in _raw_ratified(case)
               if r.get("status") == "ratified"
               and (r.get("citation") or {}).get("doc_id") == doc_id)


# The warm-up thread (core/warm.py) runs from the app lifespan and needs the same
# case/backend/catalog factories the request path uses, registered here so the warm
# module never imports this one.
warm.configure(case=_case, backend=_backend, catalog=_catalog)


# --- costing-correctness ----------------------------------------------------- #
def _nearest_clauses(case, cat, prompt: str, doc_ids: list[str], k: int = 2) -> list[dict]:
    """The closest passages in the governing document(s) to a refused costing question
    (B1). Shown as TEXT next to the refusal, never as an answer. An empty scope or a
    missing index yields [] rather than a search over other units' contracts."""
    if not doc_ids:
        return []
    # Search on the question MINUS its roster labels and parentheticals: 'Firefighter/
    # Paramedic (56 hr, top step)' would otherwise outscore the pay clause being asked
    # about. 'pay' and 'rate' anchor the query to the compensation articles.
    labels = [str(s.get("name", "")) for s in case.subjects()]
    query = re.sub(r"\s+", " ", costing.mask_labels(prompt, labels)).strip()
    query = f"{query} pay rate".strip()
    try:
        hits = _backend(case).search(query, doc_ids=doc_ids, k=k)
    except Exception:
        return []
    out = []
    for h in hits[:k]:
        try:
            out.append(_source_entry(case, cat, h))
        except Exception:
            continue
    return out


# --- gate ---
# 'Try to break it' (DEMO_TICKETS.md E3): mutate one rule in memory, run every known
# answer against each mutant, and report which deliberate errors the known answers catch.
# No model call, nothing written to any rule file; one ledger event records the attempt.
def _format_amount(value, result_type: str) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if result_type == "currency":
        return f"${v:,.2f}"
    if v.is_integer():
        return f"{int(v)} {result_type}".strip()
    return f"{v:g} {result_type}".strip()


def _rule_for_break(case, rule_id: str) -> dict | None:
    """The rule to mutate: the proposed queue's copy first, else the live library's."""
    for r in admin_proposed().get("rules", []):
        if r.get("id") == rule_id:
            return r
    for r in _live_dicts(case):
        if r.get("id") == rule_id:
            return r
    return None


def _try_break(case, rule: dict) -> dict:
    """Run the mutation check for one rule dict. Pure apart from reading the case."""
    from .mutate import mutants as _mutants
    rid = rule.get("id")
    want = rule.get("result_type", "currency")
    govern = [s["id"] for s in case.manifest.get("sources", [])
              if s.get("doc_type") in ("MOU", "amendment")]
    base_lib = _with_live(case, [rule])
    base_st, base_proved = _coverage(case, base_lib)
    rows = []
    for m in _mutants(rule, case.field_values(), govern):
        lib = [m["rule"] if r.get("id") == rid else r for r in base_lib]
        after_st, _ = _coverage(case, lib)
        caught_by = None
        for name, b in base_st.items():
            a = after_st[name]
            if b.get("status") == "pass" and a.get("status") != "pass":
                caught_by = {"scenario": name, "expected": b.get("expected"),
                             "actual": a.get("actual"), "status": a.get("status"),
                             "expected_text": _format_amount(b.get("expected"), want),
                             "actual_text": _format_amount(a.get("actual"), want)}
                break
        # The gate's own refusal, so the screen shows what Approve would have said.
        verdict = _gate_verdict(case, [m["rule"]], "mutation-test", None,
                                attempt="mutation-test")
        rows.append({"label": m["label"], "kind": m["kind"], "caught": caught_by is not None,
                     "caught_by": caught_by,
                     "refusal": (verdict["response"] or {}).get("warning")
                     if not verdict["approved"] else None,
                     "when": m["rule"].get("when"), "compute": m["rule"].get("compute")})
    caught = sum(1 for r in rows if r["caught"])
    fires = rid in base_proved
    if not fires or caught == 0:
        verdict_word = "not actually tested"
    elif caught == len(rows):
        verdict_word = "tested"
    else:
        verdict_word = "partly tested"
    return {"rule_id": rid, "result_type": want, "total": len(rows), "caught": caught,
            "verdict": verdict_word, "fires_in": base_proved.get(rid, []),
            "survivors": [r["label"] for r in rows if not r["caught"]],
            "mutants": rows}


@app.post("/admin/try_break")
async def admin_try_break(request: Request):
    """Mutate the named rule (proposed queue first, else live library) in memory and
    report, per mutant, whether a known answer catches it: {rule_id, total, caught,
    verdict, mutants: [{label, kind, caught, caught_by: {scenario, expected, actual,
    status}}]}. verdict is 'tested' (all caught), 'partly tested', or 'not actually
    tested' (none caught, or the rule fires in no passing known answer). Never writes a
    rule file; appends one `authoring.mutation_check` ledger event."""
    body = await request.json()
    rule_id = str(body.get("rule_id") or "")
    case = _case()
    rule = _rule_for_break(case, rule_id)
    if rule is None:
        return JSONResponse({"error": f"no proposed or live rule with id {rule_id!r}"},
                            status_code=404)
    out = _try_break(case, rule)
    case.ledger().append("authoring.mutation_check",
                         {"rule_id": rule_id, "total": out["total"], "caught": out["caught"],
                          "survivors": out["survivors"], "verdict": out["verdict"]},
                         actor="admin")
    return out


@app.get("/admin/mutation_report")
def admin_mutation_report():
    """The mutation check over every live rule, for the foot of the Verification tab:
    {total, caught, survivors: [{rule_id, label}], rules: [{rule_id, total, caught, verdict}]}.
    Read-only; no ledger event (it is a view, not an attempt)."""
    case = _case()
    rules = []
    survivors = []
    for r in _live_dicts(case):
        out = _try_break(case, r)
        rules.append({"rule_id": out["rule_id"], "total": out["total"],
                      "caught": out["caught"], "verdict": out["verdict"]})
        survivors.extend({"rule_id": out["rule_id"], "label": s} for s in out["survivors"])
    return {"total": sum(r["total"] for r in rules), "caught": sum(r["caught"] for r in rules),
            "survivors": survivors, "rules": rules}


# --- ledger --------------------------------------------------------------- #
# DEMO_TICKETS.md F1/F2/F3: replay an answer from its frozen inputs, read who approved
# which rule text, and break the chain on a scratch copy. Nothing here writes to the
# ledger or the rule file.
from . import approvals, tamper_demo  # noqa: E402  (end-of-file block, see ownership plan)

_QID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


@app.get("/chat/replay/{query_id}")
def chat_replay(query_id: str):
    """Recompute a costing answer from its snapshot and compare hashes. Read-only; the
    snapshot file name is taken from the ledger event, never from the URL."""
    if not _QID_RE.match(query_id):
        return JSONResponse({"status": "not_replayable", "query_id": query_id,
                             "reason": "malformed query id"}, status_code=400)
    case = _case()
    return audit.replay(case, case.ledger(), query_id)


@app.get("/admin/approvals")
def admin_approvals():
    """Per live rule: the newest full approval event and whether the live text still
    matches the fingerprint that was approved."""
    case = _case()
    return {"approvals": approvals.report(case, case.ledger(), _raw_ratified(case))}


@app.get("/admin/ledger/tamper-demo")
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


# --- agentic ---------------------------------------------------------------- #
# Skeptic reviews (DEMO_TICKETS.md G1/G6) and the never-500 backstop (A4/G2).
# The app serves STORED reviews produced offline; it makes no model call here.
from . import skeptic as _skeptic  # noqa: E402


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


def _note_imported(case, art: dict) -> None:
    """Append skeptic.imported once per artifact hash the first time THIS instance serves
    a review whose run is not in its own ledger (a baked artifact shipped in git)."""
    led = case.ledger()
    sha = art.get("artifact_sha256")
    run_id = (art.get("provenance") or {}).get("run_id")
    for ev in led.read():
        if ev.get("type") == "skeptic.imported" and ev["payload"].get("artifact_sha256") == sha:
            return
        if ev.get("type") == "skeptic.finish" and ev["payload"].get("run_id") == run_id:
            return
    led.append("skeptic.imported",
               {"rule_id": art.get("rule_id"), "artifact_sha256": sha,
                "producer": (art.get("provenance") or {}).get("producer"),
                "produced_at": (art.get("provenance") or {}).get("produced_at"),
                "fresh": art.get("fresh")}, actor="skeptic")


@app.get("/admin/skeptic")
def admin_skeptic_index():
    case = _case()
    out = {}
    for rid in _skeptic.list_reviews(case):
        art = _skeptic.load_review(case, rid) or {}
        ws = art.get("warnings", [])
        out[rid] = {"verdict": art.get("verdict"), "warnings": len(ws),
                    "high": sum(1 for w in ws if w.get("severity") == "high"),
                    "fresh": art.get("fresh"), "stale_reasons": art.get("stale_reasons", []),
                    "mode": (art.get("provenance") or {}).get("mode"),
                    "producer": (art.get("provenance") or {}).get("producer"),
                    "produced_at": (art.get("provenance") or {}).get("produced_at")}
    return {"reviews": out}


@app.get("/admin/skeptic/trail")
def admin_skeptic_trail(run_id: str):
    led = _case().ledger()
    return {"run_id": run_id,
            "events": [e for e in led.read() if e.get("type", "").startswith("skeptic.")
                       and e.get("payload", {}).get("run_id") == run_id]}


@app.get("/admin/skeptic/{rule_id:path}")
def admin_skeptic_review(rule_id: str):
    case = _case()
    art = _skeptic.load_review(case, rule_id)
    if art is None:
        return JSONResponse(
            {"error": f"no skeptic review for {rule_id}",
             "bake": f"env -u ANTHROPIC_API_KEY .venv/bin/python scripts/skeptic.py start "
                     f"--rule {rule_id} --producer claude-code-session"}, status_code=404)
    _note_imported(case, art)
    return art


# --------------------------------------------------------------------------- #
# --- ocr --- (DEMO_TICKETS.md D1/D2/D4: text origin, cell verification, showcase)
# --------------------------------------------------------------------------- #
def _parse_bbox(s: str) -> list[float] | None:
    """'l,t,r,b' query parameter -> [l, t, r, b] in PDF points, or None."""
    if not s:
        return None
    try:
        parts = [float(x) for x in s.split(",")]
    except ValueError:
        return None
    return parts if len(parts) == 4 else None


def _ocr_doc_fields(entry: dict) -> dict:
    """Per-document origin + verification summary for the Documents tab (D1/D2):
    text_origin ('ocr-layer' | 'digital' | 'image-only' | 'mixed' | None when the
    catalog predates origin capture), the producer string when an OCR engine wrote
    the layer, and the pages whose tables were verified with their disputed counts."""
    case = _case()
    checks = cellcheck.load_checks(case.dir).get("pages", {})
    verified_pages = []
    for rec in checks.values():
        if rec.get("doc_id") != entry.get("doc_id"):
            continue
        disputed = sum(1 for r in rec.get("rows", []) for c in r.get("cells", [])
                       if c.get("status") == "disputed")
        verified_pages.append({"page": rec.get("page"), "disputed": disputed,
                               "rows": len(rec.get("rows", []))})
    verified_pages.sort(key=lambda p: p["page"] or 0)
    return {"text_origin": entry.get("text_origin"),
            "producer": entry.get("producer", ""),
            "verified_pages": verified_pages}


def _showcase(case) -> dict | None:
    """The one page the demo opens Compare on (D4): declared in case.yaml under
    `showcase:` or in cell_checks.json, else derived — the verified page with the
    most disputed cells. None when nothing has been verified."""
    checks = cellcheck.load_checks(case.dir)
    declared = case.manifest.get("showcase") or checks.get("showcase")
    if declared and declared.get("doc") and declared.get("page"):
        return {"doc": declared["doc"], "page": int(declared["page"]),
                "label": declared.get("label") or f"Showcase: p.{declared['page']}"}
    best = None
    for rec in checks.get("pages", {}).values():
        disputed = sum(1 for r in rec.get("rows", []) for c in r.get("cells", [])
                       if c.get("status") == "disputed")
        if disputed and (best is None or disputed > best[0]):
            best = (disputed, rec)
    if not best:
        return None
    rec = best[1]
    return {"doc": rec["doc_id"], "page": rec["page"],
            "label": f"Showcase: p.{rec['page']} — {best[0]} cells the OCR misread"}


@app.get("/admin/cell_checks")
def admin_cell_checks():
    """The stored numeric-cell verification record (D2) — what scripts/verify_cells.py
    computed, with the engine that computed it. Read-only; the Compare view and the
    gate read the same file."""
    case = _case()
    data = cellcheck.load_checks(case.dir)
    pages = []
    for key, rec in data.get("pages", {}).items():
        cells = [c for r in rec.get("rows", []) for c in r.get("cells", [])]
        pages.append({"key": key, "doc_id": rec.get("doc_id"), "page": rec.get("page"),
                      "rows": len(rec.get("rows", [])),
                      "disputed": sum(1 for c in cells if c.get("status") == "disputed"),
                      "unverified": sum(1 for c in cells if c.get("status") == "unverified"),
                      "verified": sum(1 for c in cells if c.get("status") == "verified")})
    return {"engine": data.get("engine"), "generated_at": data.get("generated_at"),
            "generated_by": data.get("generated_by"), "pages": pages,
            "showcase": _showcase(case)}


# --------------------------------------------------------------------------- #
# --- citations-polish --- (DEMO_TICKETS.md C4/C2/I12/C7: declared names, readable
# highlights, armed source hashes)
# --------------------------------------------------------------------------- #
def _display_title(case, cat, doc_id: str) -> str:
    """What every surface calls a document (C4): the name case.yaml declares, else
    the catalog's own cover heading when it is a name and not a date line, else the
    id. Uploads have no declaration, so their extracted heading is kept."""
    src = case.source_by_id(doc_id) or {}
    declared = str(src.get("title") or "").strip()
    if declared:
        return declared
    entry = cat.get(doc_id) if cat is not None else None
    extracted = str((entry or {}).get("title") or "").strip()
    if extracted and not ingest._date_like(extracted):
        return extracted
    return doc_id


def _cover_title(entry: dict | None) -> str:
    """The heading the cover page itself carries, or "" when the catalog stored a
    date line there (a pre-C4 bake) — a date is not something to show as a name."""
    extracted = str((entry or {}).get("title") or "").strip()
    return "" if not extracted or ingest._date_like(extracted) else extracted


def _provenance_gate(case, doc_id: str, pdf_path: str, route: str):
    """409 when the PDF on disk no longer matches the hash the catalog recorded (C7):
    the same check doc_file and doc_page make, for the X-ray/Compare clause route,
    which otherwise lists clauses of a document that is not the one on disk. Returns
    None when the source is intact (or was never hashed)."""
    ok, expected, actual = _check_source_hash(_catalog(case), doc_id, pdf_path)
    if ok:
        return None
    case.ledger().append("provenance.mismatch",
                         {"doc_id": doc_id, "expected": expected, "actual": actual,
                          "route": route}, actor="system")
    return JSONResponse({"error": "source document changed since ingestion — "
                         "its extracted clauses no longer describe the file on disk",
                         "reason": "provenance", "doc_id": doc_id,
                         "expected": expected[:12], "actual": actual[:12]},
                        status_code=409)


def _page_render_params(bbox: str, crop: str) -> tuple[dict, str | None]:
    """Parse the /doc/{id}/page/{page} query into render_page_with_bbox kwargs, or
    name what is wrong with it (-> 400)."""
    params: dict = {"bbox": []}
    if bbox:
        box = pdfview.finite_box(bbox.split(","))
        if box is None:
            return params, "bbox must be four finite comma-separated numbers (l,t,r,b)"
        params["bbox"] = box
    c = (crop or "").strip().lower()
    if c in ("1", "true", "yes", "auto"):
        if params["bbox"]:
            params["crop_margin_pt"] = pdfview.CROP_MARGIN_PT
    elif c:
        region = pdfview.finite_box(c.split(","))
        if region is None:
            return params, "crop must be '1' or four finite comma-separated numbers (l,t,r,b)"
        params["crop"] = region
    return params, None
