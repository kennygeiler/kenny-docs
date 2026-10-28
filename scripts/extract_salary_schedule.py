"""Extract every row of the Master Salary Schedule into cases/santacruz/data/salary_schedule.csv
(M1.4 / old ticket I14).

The schedule is one born-digital page (Excel export): 27 titles, each printed with an
effective date, a MINIMUM and a MAXIMUM hourly rate, and a MINIMUM and a MAXIMUM monthly
salary. It prints no intermediate steps and no annual figure (the Local 3535 MOU's
Appendix "A", pp.35-37, prints steps I-III for that unit; the schedule's MINIMUM/MAXIMUM
equal the MOU's first and last CY2026 step). One CSV row per title x step:

    schedule_ref   stable key the roster binds to, e.g. firefighter-paramedic-56-hr:max
    title          the TITLE cell as printed ('FIREFIGHTER/PARAMEDIC- 56 hr')
    basis          56-hr | 40-hr | unstated  (the hours the title itself prints)
    step           minimum | maximum         (the column words; roster says entry | top)
    effective_date ISO date of the row's Effective Date
    hourly         the step's hourly rate
    monthly        the step's monthly salary, blank where the row prints '-' or nothing
    doc_id, page   master_salary_schedule, 1
    row_bbox       the whole printed line, [left, top, right, bottom], PDF points,
                   bottom-left origin (top > bottom) — the catalog's convention
    hourly_bbox    the hourly cell alone, same convention
    quote          the line's text exactly as the PDF's text layer gives it

Read with pypdfium2's text layer, never a model. Cross-checked against the catalog's
table-row chunks (docling) where docling read the row whole; tests/test_salary_schedule.py
re-reads the PDF and asserts every CSV value.

    python scripts/extract_salary_schedule.py            # write the CSV
    python scripts/extract_salary_schedule.py --check    # exit 1 if the CSV would change
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE = os.path.join(ROOT, "cases", "santacruz")
PDF = os.path.join(CASE, "sources", "master_salary_schedule.pdf")
OUT = os.path.join(CASE, "data", "salary_schedule.csv")
DOC_ID = "master_salary_schedule"

FIELDS = ["schedule_ref", "title", "basis", "step", "effective_date", "hourly", "monthly",
          "doc_id", "page", "row_bbox", "hourly_bbox", "quote"]

# Right edges (PDF points) of the four right-aligned money columns on the page.
COLUMNS = {"min_hourly": 426.6, "max_hourly": 530.6, "min_monthly": 634.5, "max_monthly": 738.5}
_DATE_RE = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")
_MONEY_RE = re.compile(r"^\$[\d,]+\.\d{2}$")
_BASIS_RE = re.compile(r"-\s*(\d{2})\s*hr\b", re.I)


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def basis_of(title: str) -> str:
    m = _BASIS_RE.search(title)
    return f"{m.group(1)}-hr" if m else "unstated"


def fmt_box(l: float, t: float, r: float, b: float) -> str:
    return ",".join(f"{v:.2f}" for v in (l, t, r, b))


def money(s: str) -> str:
    return f"{float(s.replace('$', '').replace(',', '')):.2f}"


def _segments(pdf_path: str) -> list[dict]:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        tp = pdf[0].get_textpage()
        segs = []
        for i in range(tp.count_rects()):
            l, b, r, t = tp.get_rect(i)
            text = tp.get_text_bounded(l, b, r, t).strip()
            if text:
                segs.append({"l": l, "b": b, "r": r, "t": t, "text": text})
        return segs
    finally:
        pdf.close()


def line_text(pdf_path: str, page: int, l: float, t: float, r: float, b: float) -> str:
    """The text layer inside a box, whitespace collapsed (used by the CSV and its test)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        tp = pdf[page - 1].get_textpage()
        return re.sub(r"\s+", " ", tp.get_text_bounded(left=l, bottom=b, right=r, top=t)).strip()
    finally:
        pdf.close()


def extract(pdf_path: str = PDF) -> list[dict]:
    segs = _segments(pdf_path)
    # A data line starts with a title segment at the left margin, below the header band.
    titles = [s for s in segs if s["l"] < 60 and s["t"] < 495]
    rows: list[dict] = []
    for ts in sorted(titles, key=lambda s: -s["t"]):
        mid = (ts["t"] + ts["b"]) / 2
        line = [s for s in segs if abs((s["t"] + s["b"]) / 2 - mid) < 5]
        cells: dict[str, dict] = {}
        date = None
        for s in line:
            if s is ts:
                continue
            if _DATE_RE.match(s["text"]):
                date = s["text"]
            elif _MONEY_RE.match(s["text"]):
                col = min(COLUMNS, key=lambda c: abs(COLUMNS[c] - s["r"]))
                assert abs(COLUMNS[col] - s["r"]) < 3, (ts["text"], s)
                assert col not in cells, (ts["text"], col)
                cells[col] = s
            elif s["text"] != "-":
                raise ValueError(f"unexpected text on the {ts['text']!r} line: {s['text']!r}")
        assert date and "min_hourly" in cells and "max_hourly" in cells, ts["text"]
        l = min(s["l"] for s in line)
        r = max(s["r"] for s in line)
        t = max(s["t"] for s in line)
        b = min(s["b"] for s in line)
        row_box = (l - 1, t + 1, r + 1, b - 1)
        quote = line_text(pdf_path, 1, *row_box)
        title = ts["text"]
        for step, hk, mk in (("minimum", "min_hourly", "min_monthly"),
                             ("maximum", "max_hourly", "max_monthly")):
            h = cells[hk]
            rows.append({
                "schedule_ref": f"{slug(title)}:{step[:3]}",
                "title": title, "basis": basis_of(title), "step": step,
                "effective_date": datetime.strptime(date, "%B %d, %Y").date().isoformat(),
                "hourly": money(h["text"]),
                "monthly": money(cells[mk]["text"]) if mk in cells else "",
                "doc_id": DOC_ID, "page": "1",
                "row_bbox": fmt_box(*row_box),
                "hourly_bbox": fmt_box(h["l"], h["t"], h["r"], h["b"]),
                "quote": quote,
            })
    return rows


def catalog_disagreements(rows: list[dict]) -> list[str]:
    """Rows whose docling table-row chunk prints a different hourly/monthly value. A chunk
    docling did not read whole (the defensible-space rows) is skipped, not trusted."""
    with open(os.path.join(CASE, "catalog.json")) as f:
        doc = next(d for d in json.load(f)["documents"] if d["doc_id"] == DOC_ID)
    by_title = {}
    for c in doc["clauses"]:
        m = re.search(r"TITLE:\s*([^|]+?)\s*\|", c.get("text") or "")
        if m:
            by_title[m.group(1).strip()] = c["text"]
    out = []
    for r in rows:
        text = by_title.get(r["title"])
        if text is None:
            out.append(f"{r['title']}: no catalog row")
            continue
        word = r["step"].upper()
        for col, val in ((f"{word} HOURLY RATE", r["hourly"]), (f"{word} MONTHLY SALARY", r["monthly"])):
            m = re.search(re.escape(col) + r":\s*\$([\d,]+\.\d{2})(?=\s*(?:\||$))", text)
            if m and money(m.group(1)) != val:
                out.append(f"{r['title']} {col}: catalog {m.group(1)}, PDF {val}")
    return out


def render(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    rows = extract()
    bad = catalog_disagreements(rows)
    if bad:
        print("catalog disagrees with the PDF:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    text = render(rows)
    if a.check:
        with open(OUT) as f:
            same = f.read() == text
        print("up to date" if same else "salary_schedule.csv is stale")
        return 0 if same else 1
    with open(OUT, "w") as f:
        f.write(text)
    print(f"wrote {len(rows)} rows ({len(rows) // 2} titles) to {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
