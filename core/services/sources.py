"""Source documents and citations: PDF hashes, extraction tiers, citation enrichment,
the stale-rule revalidation that runs after a re-ingest, display titles.
"""
from __future__ import annotations

import os

from .. import evidence, ingest, rowbands
from .rulestore import _RULES_LOCK, _raw_ratified, _write_ratified
from .state import _catalog

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
    with _RULES_LOCK:
        return _revalidate_citations_locked(case, cat, doc_ids, led)


def _revalidate_citations_locked(case, cat, doc_ids: list[str], led) -> list[dict]:
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
