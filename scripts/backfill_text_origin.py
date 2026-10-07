#!/usr/bin/env python
"""Back-fill `text_origin`, `text_origin_pages` and `producer` onto a shipped catalog (D1).

The catalog was baked before ingest recorded where a document's text came from, so
every citation chip called the four OCR'd scans a "text layer". This reads each
catalog document's PDF with pypdfium2 only (about a second for the corpus — no
docling, no model) and adds exactly those three keys to each entry. Every other
key of every entry is left byte-identical and the file keeps its own indentation
(the shipped catalog is indent=1; Catalog.save would rewrite all 30K lines at
indent=2, which is a diff nobody can review). Running it twice is a no-op.
tests/test_text_origin.py proves both.

    python scripts/backfill_text_origin.py --case cases/santacruz
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.catalog import Catalog  # noqa: E402
from core.ingest import text_origin  # noqa: E402

ORIGIN_KEYS = ("text_origin", "text_origin_pages", "producer")


def _detect_indent(path: str) -> int:
    """The indent the file was written with (json.dump's own layout: the first key
    sits on line 2, indented by exactly the indent width)."""
    try:
        with open(path) as f:
            f.readline()
            second = f.readline()
        n = len(second) - len(second.lstrip(" "))
        return n or 2
    except OSError:
        return 2


def _save_in_place(cat: Catalog, indent: int) -> None:
    """Catalog.save's atomic write, at the file's own indent."""
    tmp = cat.path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"documents": cat.documents()}, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, cat.path)


def _pdf_for(case_dir: str, entry: dict) -> str | None:
    pdf_path = entry.get("file") or ""
    if not os.path.isabs(pdf_path):
        pdf_path = os.path.join(case_dir, pdf_path)
    if os.path.exists(pdf_path):
        return pdf_path
    # The entry records the path it was ingested from (another machine, another
    # root); the case files the PDF under sources/<doc_id>.pdf.
    alt = os.path.join(case_dir, "sources", f"{entry['doc_id']}.pdf")
    return alt if os.path.exists(alt) else None


def backfill(case_dir: str, verbose: bool = True) -> list[dict]:
    """Add the origin fields to every catalog document whose PDF is on disk.
    Returns one record per document: {doc_id, text_origin, producer, changed}."""
    path = os.path.join(case_dir, "catalog.json")
    cat = Catalog(path)
    indent = _detect_indent(path)
    report, dirty = [], False
    for entry in cat.documents():
        doc_id = entry["doc_id"]
        pdf_path = _pdf_for(case_dir, entry)
        if pdf_path is None:
            if verbose:
                print(f"{doc_id}: PDF not found; skipped")
            report.append({"doc_id": doc_id, "text_origin": None,
                           "producer": "", "changed": False})
            continue
        origin = text_origin(pdf_path)
        new = {"text_origin": origin["doc"], "text_origin_pages": origin["pages"],
               "producer": origin["producer"]}
        changed = any(entry.get(k) != v for k, v in new.items())
        if changed:
            # Mutate the SAME dict (new keys append after the existing ones) so the
            # entry keeps every other key in its original order and value.
            entry.update(new)
            dirty = True
        if verbose:
            print(f"{doc_id}: {origin['doc']}"
                  + (f" ({origin['producer']})" if origin["producer"] else "")
                  + ("" if changed else " — unchanged"))
        report.append({"doc_id": doc_id, "text_origin": origin["doc"],
                       "producer": origin["producer"], "changed": changed})
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
