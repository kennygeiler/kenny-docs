"""Row bands for table-row citations (DEMO_TICKETS.md C2).

The shipped catalog was baked before ingest produced per-row boxes, so every row of a
table carries the WHOLE table's bbox: citing one accrual row boxed the entire table.
The verified-cell record (cases/<case>/cell_checks.json, written by
scripts/verify_cells.py) holds a box for every numeric cell of every verified row,
keyed to the stored row text by hash. From those a row's own band is derived
parse-free: the table's horizontal extent by the cells' vertical extent. Pages that
were never verified keep the table box — no claim beats a wrong claim.
"""
from __future__ import annotations

from . import cellcheck

ROW_PAD_PT = 0.5   # the cell boxes hug the glyphs; a hair of air keeps the fill off them


def row_band(case_dir: str, doc_id: str, page, text: str,
             table_bbox: list[float] | None = None) -> list[float] | None:
    """[l, t, r, b] (PDF points, bottom-left origin — the catalog's orientation) of
    the one row whose stored text is `text` on the verified page, or None."""
    try:
        page = int(page)
    except (TypeError, ValueError):
        return None
    rec = cellcheck.load_checks(case_dir).get("pages", {}).get(cellcheck.page_key(doc_id, page))
    if not rec:
        return None
    want = cellcheck.text_sha(text)
    for row in rec.get("rows", []):
        if row.get("text_sha") != want:
            continue
        boxes = [c.get("bbox") for c in row.get("cells", [])
                 if isinstance(c.get("bbox"), list) and len(c["bbox"]) == 4]
        if not boxes:
            return None
        top = max(max(b[1], b[3]) for b in boxes) + ROW_PAD_PT
        bottom = min(min(b[1], b[3]) for b in boxes) - ROW_PAD_PT
        if table_bbox and len(table_bbox) == 4:
            left, right = min(table_bbox[0], table_bbox[2]), max(table_bbox[0], table_bbox[2])
        else:
            left, right = min(b[0] for b in boxes), max(b[2] for b in boxes)
        return [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)]
    return None


def narrow(case_dir: str, doc_id: str, page, bbox: list[float] | None,
           text: str) -> list[float] | None:
    """The row's own band when one is known, else the box the catalog holds."""
    return row_band(case_dir, doc_id, page, text, bbox) or bbox
