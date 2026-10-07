#!/usr/bin/env python
"""Precompute numeric-cell verification for a case's OCR'd tables (D2).

For each requested page: rebuild the stored grid from the catalog's table-row
clauses, run the pure-code column checks, re-read the table box with RapidOCR
(torch backend, scale 3) and align the two. Results land in <case>/cell_checks.json
keyed "<doc_id>:<page>"; the /doc/{id}/clauses endpoint, the Compare view and the
evidence gate read them from there. The catalog itself is not touched.

    python scripts/verify_cells.py --case cases/santacruz \
        --pages firefighters_local3535_mou:22 admin_group_mou:29 chief_officers_mou:26
    python scripts/verify_cells.py --case cases/santacruz --all     # every table page

Idempotent: re-running replaces the requested pages' records and leaves the rest.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cellcheck  # noqa: E402
from core.catalog import Catalog  # noqa: E402


def _pdf_for(case_dir: str, entry: dict) -> str | None:
    p = entry.get("file") or ""
    if not os.path.isabs(p):
        p = os.path.join(case_dir, p)
    if os.path.exists(p):
        return p
    alt = os.path.join(case_dir, "sources", f"{entry['doc_id']}.pdf")
    return alt if os.path.exists(alt) else None


def table_pages(entry: dict) -> list[int]:
    return sorted({c.get("page") for c in entry.get("clauses", [])
                   if c.get("kind") == "table-row" and c.get("page") is not None})


def verify(case_dir: str, targets: list[tuple[str, int]], verbose: bool = True) -> dict:
    cat = Catalog(os.path.join(case_dir, "catalog.json"))
    data = cellcheck.load_checks(case_dir)
    data.setdefault("pages", {})
    data["generated_by"] = "scripts/verify_cells.py"
    data["engine"] = cellcheck.engine_info()
    data["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for doc_id, page in targets:
        entry = cat.get(doc_id)
        if not entry:
            print(f"{doc_id}: not in catalog; skipped", file=sys.stderr)
            continue
        pdf = _pdf_for(case_dir, entry)
        if not pdf:
            print(f"{doc_id}: PDF not found; skipped", file=sys.stderr)
            continue
        t0 = time.time()
        rec = cellcheck.verify_page(entry, pdf, page)
        data["pages"][cellcheck.page_key(doc_id, page)] = rec
        if verbose:
            n = len(rec["rows"])
            disputed = sum(1 for r in rec["rows"] if r["cell_status"] == "disputed")
            print(f"{doc_id} p.{page}: {n} rows, {disputed} disputed "
                  f"({time.time() - t0:.1f}s)")
            for r in rec["rows"]:
                for c in r["cells"]:
                    if c["status"] != "verified":
                        print(f"   row {r['ordinal']} col {c['col']} [{c['status']}] "
                              f"stored {c['stored']!r} re-read {c['reread']!r} "
                              f"{c['flags'] or ''}")
    cellcheck.save_checks(case_dir, data)
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--case", required=True)
    ap.add_argument("--pages", nargs="*", default=[], metavar="DOC:PAGE")
    ap.add_argument("--all", action="store_true", help="every page holding a table")
    args = ap.parse_args(argv)
    targets: list[tuple[str, int]] = []
    for spec in args.pages:
        doc_id, _, page = spec.rpartition(":")
        targets.append((doc_id, int(page)))
    if args.all:
        cat = Catalog(os.path.join(args.case, "catalog.json"))
        for entry in cat.documents():
            targets += [(entry["doc_id"], p) for p in table_pages(entry)]
    if not targets:
        ap.error("give --pages DOC:PAGE ... or --all")
    verify(args.case, targets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
