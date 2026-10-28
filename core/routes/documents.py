"""Documents: PDF file/page/clauses, ingest and upload jobs, catalog, coverage,
cell checks (x-ray), taxonomy, clause lookup.
"""
from __future__ import annotations

import os
import re
import threading

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import cellcheck, ingest, pdfview
from ..pdfview import page_dims, render_page_with_bbox
from ..services.rulestore import _cited_rule_count, _proposed_queue
from ..services.sources import (
    _check_source_hash,
    _cover_title,
    _display_title,
    _extraction_stats,
    _page_origin,
    _resolve_pdf,
    _revalidate_citations,
)
from ..services.state import (
    _JOBS,
    _MAX_UPLOAD_BYTES,
    _backend,
    _case,
    _catalog,
    _register_job,
    _taxonomy,
)

router = APIRouter()


@router.get("/doc/{doc_id}/file")
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


@router.get("/doc/{doc_id}/page/{page}")
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


@router.get("/doc/{doc_id}/clauses")
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


@router.post("/admin/ingest")
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


@router.get("/admin/ingest/status/{job_id}")
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


@router.post("/admin/upload")
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

    # An upload must never REPLACE a document: a file named like a declared contract
    # used to overwrite sources/<name>.pdf and re-bind its doc_id to new bytes, silently
    # changing what ratified rules cite. Checked inside the single-flight slot.
    clash = _upload_collision(case, fname, doc_id)
    if clash:
        return _reject(JSONResponse({"error": clash}, status_code=409))

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


def _upload_collision(case, fname: str, doc_id: str) -> str | None:
    """Why this upload would clobber an existing document, or None if it is new."""
    base = fname.lower()
    for src in case.manifest.get("sources", []) or []:
        sid = str(src.get("id") or "")
        sfile = os.path.basename(str(src.get("file") or "")).lower()
        if base == sfile or doc_id in (sid, _slug(sid), _slug(os.path.splitext(sfile)[0])):
            return (f"'{fname}' collides with declared source '{sid}' in case.yaml — an "
                    f"upload cannot replace a declared contract. Rename the file, or "
                    f"replace the source through case.yaml and a re-ingest.")
    if _catalog(case).get(doc_id) is not None:
        return (f"a document with id '{doc_id}' is already in the library — rename the "
                f"file to upload it as a new document.")
    if os.path.exists(os.path.join(case.dir, "sources", fname)):
        return f"sources/{fname} already exists — rename the file to upload it."
    return None


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "doc"


@router.get("/admin/catalog")
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


@router.get("/admin/coverage")
def admin_coverage():
    """How much of each contract has Kenny actually modelled?

    The question an HR Director asks, which a "64 rules drafted" counter cannot answer.
    Per document: clauses extracted, rules live, rules pending review, clauses blocked
    on missing data, clauses that are narrative (nothing to compute, correctly).
    """
    case = _case()
    cat = _catalog(case)
    ratified = case.rules()
    proposed = _proposed_queue()
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


@router.get("/admin/clause")
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


@router.get("/admin/taxonomy")
def admin_taxonomy():
    case = _case()
    tax = _taxonomy(case)
    proposed = []
    for d in _catalog(case).documents():
        proposed.extend(d.get("proposed_tags", []))
    return {"taxonomy": tax, "proposed_tags": sorted(set(proposed))}


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


@router.get("/admin/cell_checks")
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
