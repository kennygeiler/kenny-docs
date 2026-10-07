#!/usr/bin/env python
"""Back-fill document titles onto a shipped catalog (DEMO_TICKETS.md C4).

Four of the five shipped documents were catalogued under their cover's TERM line
("December 31, 2028", "January 1, 2024 Through December 31, 2026"), because the old
title heuristic took the last cover line and nothing rejected a date. This re-runs
core.ingest.extract_title (parse-free: the stored clauses and the PDF's metadata are
all it reads) and rewrites `title` ONLY where the stored one is date-like. Every other
key of every entry stays byte-identical and the file keeps its own indentation (the
shipped catalog is indent=1). Running it twice is a no-op. tests/test_titles.py proves
both.

    python scripts/backfill_titles.py --case cases/santacruz
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.catalog import Catalog  # noqa: E402
from core.ingest import _date_like, extract_title  # noqa: E402
from scripts.backfill_text_origin import _detect_indent, _pdf_for, _save_in_place  # noqa: E402


def backfill(case_dir: str, verbose: bool = True) -> list[dict]:
    """Retitle every catalog document whose stored title is a date. Returns one record
    per document: {doc_id, before, after, changed}."""
    path = os.path.join(case_dir, "catalog.json")
    cat = Catalog(path)
    indent = _detect_indent(path)
    report, dirty = [], False
    for entry in cat.documents():
        doc_id = entry["doc_id"]
        before = entry.get("title") or ""
        if not _date_like(before):
            report.append({"doc_id": doc_id, "before": before, "after": before,
                           "changed": False})
            if verbose:
                print(f"{doc_id}: {before!r} — kept")
            continue
        pdf_path = _pdf_for(case_dir, entry) or ""
        fallback = entry.get("declared_title") or doc_id
        after = extract_title(entry.get("clauses") or [], pdf_path, fallback=fallback)
        changed = after != before
        if changed:
            entry["title"] = after      # same key, same position: value only
            dirty = True
        report.append({"doc_id": doc_id, "before": before, "after": after,
                       "changed": changed})
        if verbose:
            print(f"{doc_id}: {before!r} -> {after!r}" + ("" if changed else " — unchanged"))
    if dirty:
        _save_in_place(cat, indent)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--case", required=True, help="case directory holding catalog.json")
    args = ap.parse_args(argv)
    backfill(args.case)
    return 0


if __name__ == "__main__":
    sys.exit(main())
