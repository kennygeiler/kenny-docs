"""Numeric-cell verification for OCR'd tables (DEMO_TICKETS.md D2).

Table cells from an OCR text layer are stored verbatim. Firefighters p.22 prints
10.15 hours per pay period; the catalog stores "0) £5". Nothing downstream could see
it. This module gives every numeric cell a status a reader can act on:

  verified    the stored value parses and a SECOND engine (RapidOCR, torch backend —
              the engine docling's auto-OCR resolves to on this machine) read the same
              number off the page image
  disputed    the stored text does not parse, or the re-read disagrees, or a column
              check fails; the re-read value is kept beside the stored one — never
              written over it (a human confirms in Compare)
  unverified  the re-read produced no token for the cell and no check failed

Three layers, each usable without the one above it:
  parse_cell / check_table   pure code: parse, column format/constant agreement,
                             per-table arithmetic invariants
  reread_table               RapidOCR over a scale-3 crop of the table box (scale 2
                             drops the decimal points: '738' for 7.38)
  verify_page                align re-read tokens to the stored grid, decide statuses

Results are precomputed by scripts/verify_cells.py into <case>/cell_checks.json and
read back by the /doc/{id}/clauses endpoint, the Compare view and the evidence gate
(`disputed_for_citation`). The OCR engine is a lazy singleton: nothing here imports
torch or rapidocr at module import, under pytest, or at request time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import statistics
import threading
from typing import Any

log = logging.getLogger("kenny.cellcheck")

CELL_CHECKS_FILE = "cell_checks.json"
REREAD_SCALE = 3.0
STATUS_ORDER = {"verified": 0, "unverified": 1, "disputed": 2}


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
_RANGE_RE = re.compile(r"^(\d+)\s*[-–—]\s*(\d+)$")
_OPEN_RANGE_RE = re.compile(r"^(\d+)\s*\+$")
_PERCENT_RE = re.compile(r"^([\d.,]+)\s*%$")
_CURRENCY_RE = re.compile(r"^\$\s*([\d.,]+)$")
_NUMBER_RE = re.compile(r"^[\d.,]+$")
_DIGIT_RE = re.compile(r"\d")


def _number(s: str) -> float | None:
    """Digits with '.' and ',' separators -> float, or None. Decides which separator
    is the decimal one: the LAST one when both appear; a lone comma followed by one
    or two digits is a decimal comma ('7,38' is how Tesseract read 7.38, '994,79'
    likewise), three digits after it is a thousands comma ('1,015')."""
    if not s or not _NUMBER_RE.match(s):
        return None
    if s.count(".") > 1:
        return None
    if "." in s and "," in s:
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            s = parts[0] + "." + parts[1]
        elif all(len(p) == 3 for p in parts[1:]) and parts[0]:
            s = "".join(parts)
        else:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_cell(s: str | None) -> tuple[float | None, str]:
    """(value, fmt) for one stored cell. fmt in {int, decimal, currency, percent,
    range, text}. A range's value is its lower bound ('1-5' -> 1.0, '17 +' -> 17.0).
    Anything that does not parse cleanly returns (None, 'text') — '0) £5', '$02.31'
    parses (to 2.31) and is left for the column check to catch."""
    t = " ".join((s or "").replace(" ", " ").split()).strip()
    if not t:
        return None, "text"
    m = _RANGE_RE.match(t)
    if m:
        return float(m.group(1)), "range"
    m = _OPEN_RANGE_RE.match(t)
    if m:
        return float(m.group(1)), "range"
    m = _PERCENT_RE.match(t)
    if m:
        v = _number(m.group(1))
        return (v, "percent") if v is not None else (None, "text")
    m = _CURRENCY_RE.match(t)
    if m:
        v = _number(m.group(1))
        return (v, "currency") if v is not None else (None, "text")
    v = _number(t)
    if v is not None:
        return v, ("int" if "." not in t and not _is_decimal_comma(t) else "decimal")
    return None, "text"


def _is_decimal_comma(t: str) -> bool:
    parts = t.split(",")
    return len(parts) == 2 and 1 <= len(parts[1]) <= 2


def has_digits(s: str | None) -> bool:
    return bool(_DIGIT_RE.search(s or ""))


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
# per-table specs: which columns are constants, which arithmetic must hold
# --------------------------------------------------------------------------- #
# A spec applies when every `match` regex finds a header. `invariants` are
# (a_col, factor, b_col, tol): cells[a] * factor must be within tol of cells[b].
# `constant` columns must hold ONE value down the table. Column indices count the
# stored grid's columns (the label column is 0).
TABLE_SPECS: list[dict[str, Any]] = [
    {
        "name": "vacation-accrual",
        "match": [r"Hours Accrued", r"Annual shift", r"Maximum annual"],
        # hours x 26 pay periods = annual accrual; annual / 24 = shift equivalent
        # (one 24-hour shift); annual x 1.5 = maximum accrual.
        "invariants": [(1, 26.0, 3, 1.0), (3, 1 / 24, 2, 0.5), (3, 1.5, 4, 0.5)],
        "constant": [],
    },
    {
        "name": "education-incentive-columns",
        # 1ST/2ND/3RD ED and the Chief Officer certification are per-pay-period
        # constants down a salary step table; Tesseract read $92.31 as $02.31.
        "match": [r"(1ST|iST|1st)\s*ED", r"2ND\s*ED"],
        "invariants": [],
        "constant": [r"(1ST|iST|1st)\s*ED", r"2ND\s*ED", r"3RD\s*ED", r"CERT"],
    },
]


def table_spec(headers: list[str] | None) -> dict[str, Any] | None:
    if not headers:
        return None
    for spec in TABLE_SPECS:
        if all(any(re.search(p, h, re.I) for h in headers) for p in spec["match"]):
            return spec
    return None


def _constant_columns(headers: list[str] | None, spec: dict | None) -> set[int]:
    if not headers or not spec:
        return set()
    return {i for i, h in enumerate(headers)
            if any(re.search(p, h, re.I) for p in spec["constant"])}


def check_table(rows: list[list[str]], headers: list[str] | None = None,
                spec: dict | None = None) -> list[list[list[str]]]:
    """Pure-code flags for a stored grid, no OCR: flags[row][col] is a list drawn
    from 'unparsable' (digits present, no clean parse), 'column_disagreement' (the
    column's parsable cells disagree in format, or in value where the column is a
    declared constant — every cell of that column is flagged, nothing is
    auto-corrected: majority voting would pick the wrong $02.31 on admin p.29 if
    three rows had it) and 'invariant' (a per-table arithmetic rule fails; both
    operand cells are flagged)."""
    spec = spec if spec is not None else table_spec(headers)
    ncols = max([len(headers or [])] + [len(r) for r in rows] + [0])
    flags: list[list[list[str]]] = [[[] for _ in range(ncols)] for _ in rows]
    parsed = [[parse_cell(c) if j < len(r) else (None, "text")
               for j, c in enumerate(list(r) + [""] * (ncols - len(r)))] for r in rows]
    # A column is numeric when at least half of its non-empty cells parse; in such
    # a column a cell that does not parse is unparsable whether it kept digits
    # ('0) £5', 'A468') or lost them all ('fd' for 11).
    numeric_cols: set[int] = set()
    for j in range(ncols):
        filled = [i for i in range(len(rows)) if (rows[i][j] if j < len(rows[i]) else "").strip()]
        if filled and sum(1 for i in filled if parsed[i][j][0] is not None) * 2 >= len(filled):
            numeric_cols.add(j)
    for i, r in enumerate(rows):
        for j in range(ncols):
            cell = r[j] if j < len(r) else ""
            v, fmt = parsed[i][j]
            if v is None and cell.strip() and (has_digits(cell) or j in numeric_cols):
                flags[i][j].append("unparsable")
    constants = _constant_columns(headers, spec)
    for j in range(ncols):
        col = [(i, parsed[i][j]) for i in range(len(rows)) if parsed[i][j][0] is not None]
        if len(col) < 2:
            continue
        fmts = {fmt for _, (_, fmt) in col if fmt != "int"}  # int is a decimal with no fraction
        disagree = len(fmts) > 1
        if j in constants and len({round(v, 6) for _, (v, _) in col}) > 1:
            disagree = True
        if disagree:
            for i in range(len(rows)):
                if (rows[i][j] if j < len(rows[i]) else "") and \
                        "column_disagreement" not in flags[i][j]:
                    flags[i][j].append("column_disagreement")
    for a, factor, b, tol in (spec or {}).get("invariants", []):
        for i in range(len(rows)):
            if a >= ncols or b >= ncols:
                continue
            va, vb = parsed[i][a][0], parsed[i][b][0]
            if va is None or vb is None:
                continue
            if not _close(va * factor, vb, tol):
                for j in (a, b):
                    if "invariant" not in flags[i][j]:
                        flags[i][j].append("invariant")
    return flags


# --------------------------------------------------------------------------- #
# the stored grid: a server-side port of admin.html tableModel()
# --------------------------------------------------------------------------- #
_SUMMARY_RE = re.compile(r"^(.*?)\s*\(table:\s*(.+);\s*\d+\s+rows?\)$", re.S)
_HV_RE = re.compile(r"^(.{1,60}?):\s(.*)$", re.S)


def grid_from_rows(trows: list[dict]) -> dict | None:
    """Rebuild one table's grid from its flattened table-row clauses, EXACTLY as the
    Compare view does (admin.html tableModel), so a cell index computed here lands
    on the same <td> there. Returns {caption, headers, rows: [{clause, cells}],
    cols} or None when there is nothing to grid."""
    if not trows:
        return None
    caption, headers, body = None, None, list(trows)
    m = _SUMMARY_RE.match(trows[0].get("text") or "")
    if m:
        caption = m.group(1).strip() or None
        headers = [h for h in re.split(r",\s*", m.group(2)) if h]
        body = trows[1:]
    if not body:
        return None
    if caption is None and len(body) > 1:
        cand = (body[0].get("text") or "").split(" — ")[0]
        if cand and all((c.get("text") or "").startswith(cand + " — ") for c in body):
            caption = cand

    def strip(t: str) -> str:
        return t[len(caption) + 3:] if caption is not None and t.startswith(caption + " — ") else t

    raw = [{"clause": c, "cells": strip(c.get("text") or "").split(" | ")} for c in body]
    if not headers:
        n = max(len(r["cells"]) for r in raw)
        inf: list = [None] * n
        seen = [False] * n
        ok = True
        for r in raw:
            for i, cell in enumerate(r["cells"]):
                mm = _HV_RE.match(cell)
                h = mm.group(1) if mm else None
                if not seen[i]:
                    inf[i], seen[i] = h, True
                elif inf[i] != h:
                    ok = False
        if ok and all(inf):
            headers = inf
    if headers:
        rows = []
        for r in raw:
            out = [""] * len(headers)
            pos = 0
            for cell in r["cells"]:
                j = next((k for k, h in enumerate(headers)
                          if not out[k] and cell.startswith(h + ": ")), -1)
                if j >= 0:
                    out[j] = cell[len(headers[j]) + 2:]
                    pos = j + 1
                elif pos < len(out) and not out[pos]:
                    out[pos] = cell
                    pos += 1
                else:
                    out.append(cell)
            rows.append({"clause": r["clause"], "cells": out})
    else:
        rows = raw
    cols = max([len(headers) if headers else 1] + [len(r["cells"]) for r in rows])
    return {"caption": caption, "headers": headers, "rows": rows, "cols": cols}


def table_groups(clauses: list[dict]) -> list[list[dict]]:
    """Table-row clauses of one page grouped by shared bbox, in catalog order —
    docling gives every row of a table the whole-table box, which is what makes
    the group recoverable without per-row geometry."""
    groups: dict[str, list[dict]] = {}
    for c in clauses:
        if c.get("kind") != "table-row":
            continue
        key = ",".join(f"{v:.3f}" for v in (c.get("bbox") or [])) or "?"
        groups.setdefault(key, []).append(c)
    return list(groups.values())


def text_sha(s: str | None) -> str:
    return hashlib.sha256(" ".join((s or "").split()).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# second engine
# --------------------------------------------------------------------------- #
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def _engine():
    """RapidOCR on the torch backend — the same construction docling's auto-OCR
    resolves to here (docling/models/stages/ocr/auto_ocr_model.py: no ocrmac, no
    onnxruntime, torch present -> RapidOcrModel(backend='torch'), which pins the
    English PP-OCRv4 mobile models). Built on first use only."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            import torch  # noqa: F401  (docling's own precondition for this backend)
            from rapidocr import EngineType, RapidOCR
            from rapidocr.utils.typings import LangDet, LangRec, ModelType, OCRVersion
            params = {
                "Det.engine_type": EngineType.TORCH,
                "Cls.engine_type": EngineType.TORCH,
                "Rec.engine_type": EngineType.TORCH,
                "Det.lang_type": LangDet.EN, "Rec.lang_type": LangRec.EN,
                "Det.ocr_version": OCRVersion.PPOCRV4, "Det.model_type": ModelType.MOBILE,
                "Cls.ocr_version": OCRVersion.PPOCRV4, "Cls.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV4, "Rec.model_type": ModelType.MOBILE,
            }
            _ENGINE = RapidOCR(params=params)
        return _ENGINE


def engine_available() -> bool:
    """True when rapidocr + torch import (no engine is built)."""
    try:
        import importlib.util
        return all(importlib.util.find_spec(m) is not None for m in ("rapidocr", "torch"))
    except Exception:
        return False


def engine_info() -> dict:
    info = {"name": "rapidocr", "backend": "torch", "models": "en PP-OCRv4 mobile",
            "scale": REREAD_SCALE}
    try:
        from importlib.metadata import version
        info["version"] = version("rapidocr")
    except Exception:
        pass
    return info


def reread_table(pdf_path: str, page: int, bbox: list[float],
                 scale: float = REREAD_SCALE) -> list[dict]:
    """Re-read one table box with the second engine. Renders the crop through
    pypdfium2 under pdfview._RENDER_LOCK (pdfium is not thread-safe), runs RapidOCR
    and maps every token's pixel box back to PDF points, bottom-left origin, in the
    catalog's [l, t, r, b] convention. Returns [{text, score, bbox}]."""
    import numpy as np
    from . import pdfview

    l, t, r, b = bbox
    with pdfview._RENDER_LOCK:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            pg = pdf[max(0, page - 1)]
            width, height = pg.get_size()
            # crop = amounts cut off (left, bottom, right, top), in points
            img = pg.render(scale=scale, crop=(l, b, width - r, height - t)) \
                .to_pil().convert("RGB")
        finally:
            pdf.close()
    out = _engine()(np.array(img))
    tokens: list[dict] = []
    if out is None or out.txts is None:
        return tokens
    for box, txt, score in zip(out.boxes, out.txts, out.scores):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        tokens.append({
            "text": str(txt), "score": round(float(score), 3),
            "bbox": [round(l + min(xs) / scale, 2), round(t - min(ys) / scale, 2),
                     round(l + max(xs) / scale, 2), round(t - max(ys) / scale, 2)],
        })
    return tokens


# --------------------------------------------------------------------------- #
# align the re-read to the stored grid
# --------------------------------------------------------------------------- #
def _lines(tokens: list[dict]) -> list[list[dict]]:
    """Cluster tokens into text lines by vertical centre (top of page first)."""
    toks = [t for t in tokens if t.get("bbox") and len(t["bbox"]) == 4]
    if not toks:
        return []
    heights = [abs(t["bbox"][1] - t["bbox"][3]) for t in toks]
    tol = max(1.0, 0.6 * statistics.median(heights))
    toks.sort(key=lambda t: -(t["bbox"][1] + t["bbox"][3]) / 2)
    lines: list[list[dict]] = []
    for t in toks:
        yc = (t["bbox"][1] + t["bbox"][3]) / 2
        if lines:
            last = lines[-1]
            lyc = statistics.mean((u["bbox"][1] + u["bbox"][3]) / 2 for u in last)
            if abs(yc - lyc) <= tol:
                last.append(t)
                continue
        lines.append([t])
    for ln in lines:
        ln.sort(key=lambda u: u["bbox"][0])
    return lines


def _xc(t: dict) -> float:
    return (t["bbox"][0] + t["bbox"][2]) / 2


def _values_agree(stored: tuple[float | None, str], token_text: str) -> tuple[bool, float | None]:
    """Does a re-read token say the same number as a stored cell? Returns
    (agree, token_value). A token read without its decimal point ('738' for a stored
    7.38) agrees when the digit strings match — the dot is the thinnest glyph on a
    scan and the engine drops it before it drops a digit."""
    tv, tfmt = parse_cell(token_text)
    sv, sfmt = stored
    if tv is None or sv is None:
        return False, tv
    if sfmt == "range" or tfmt == "range":
        return (sfmt == tfmt and _close(sv, tv, 0.0)), tv
    if _close(sv, tv, 0.005):
        return True, tv
    sd = re.sub(r"\D", "", f"{sv:.2f}").lstrip("0")
    td = re.sub(r"\D", "", token_text).lstrip("0")
    if tfmt == "int" and sd and sd == td:
        return True, sv
    return False, tv


def _is_numeric_token(text: str) -> bool:
    """A token that is a number, or mostly digits ('$. 22,631.50' counts, a header
    such as '1STED' or '2ND ED' does not — one digit in a word is a word)."""
    if parse_cell(text)[0] is not None:
        return True
    body = re.sub(r"\s", "", text or "")
    return bool(body) and len(re.sub(r"\D", "", body)) * 2 >= len(body)


def _numeric_tokens(line: list[dict]) -> list[dict]:
    return [t for t in line if _is_numeric_token(t["text"]) and t.get("score", 0) >= 0.5]


def align(grid: dict, tokens: list[dict], flags: list[list[list[str]]]) -> list[dict]:
    """Match re-read tokens to the stored grid's cells and decide each cell's status.
    Rows: by label (column 0 parses to the same range/number) when that works for at
    least half the rows, else by order among the re-read lines that carry >= 2
    numeric tokens. Columns: first exact value matches consume tokens ('verified');
    each column's anchor x is the median of its matched tokens; then every
    unmatched stored cell takes the nearest unmatched token to its anchor
    ('disputed', re-read value kept beside the stored one). A cell with no anchor
    and no token is 'unverified' unless a pure-code flag already disputes it."""
    rows = grid["rows"]
    headers = grid.get("headers") or []
    ncols = grid["cols"]
    lines = [_numeric_tokens(ln) for ln in _lines(tokens)]
    data_lines = [ln for ln in lines if len(ln) >= 2]

    # --- row mapping
    mapping: dict[int, list[dict]] = {}
    by_label = 0
    for i, r in enumerate(rows):
        label = parse_cell(r["cells"][0] if r["cells"] else "")
        if label[0] is None:
            continue
        hits = [ln for ln in data_lines if ln and _values_agree(label, ln[0]["text"])[0]]
        if len(hits) == 1:
            mapping[i] = hits[0]
            by_label += 1
    if by_label * 2 < len(rows):
        mapping = {i: data_lines[i] for i in range(min(len(rows), len(data_lines)))}

    # --- pass 1: exact matches, and column anchors from them
    matched: list[list[dict | None]] = [[None] * ncols for _ in rows]
    used: set[int] = set()
    anchors_src: dict[int, list[float]] = {}
    for i, r in enumerate(rows):
        line = mapping.get(i)
        if not line:
            continue
        for j in range(ncols):
            cell = r["cells"][j] if j < len(r["cells"]) else ""
            stored = parse_cell(cell)
            if stored[0] is None:
                continue
            for t in line:
                if id(t) in used:
                    continue
                ok, _ = _values_agree(stored, t["text"])
                if ok:
                    matched[i][j] = t
                    used.add(id(t))
                    anchors_src.setdefault(j, []).append(_xc(t))
                    break
    anchors = {j: statistics.median(xs) for j, xs in anchors_src.items()}
    # How far from its column anchor a token may sit and still be that column's:
    # a third of the narrowest gap between neighbouring anchors (never under 12pt).
    xs_sorted = sorted(anchors.values())
    gaps = [b - a for a, b in zip(xs_sorted, xs_sorted[1:])]
    reach = max(12.0, 0.35 * min(gaps)) if gaps else 12.0

    # --- pass 2: nearest unmatched token to the column anchor
    for i, r in enumerate(rows):
        line = mapping.get(i)
        if not line:
            continue
        for j in range(ncols):
            if matched[i][j] is not None or j not in anchors:
                continue
            cell = r["cells"][j] if j < len(r["cells"]) else ""
            if not cell.strip():
                continue
            free = [t for t in line if id(t) not in used]
            if not free:
                continue
            # a token belongs to this column only if it sits within reach of our
            # anchor and no other column's anchor is closer (keeps a neighbouring
            # column's number out)
            best = min(free, key=lambda t: abs(_xc(t) - anchors[j]))
            dist = abs(_xc(best) - anchors[j])
            others = [a for k, a in anchors.items() if k != j]
            if dist > reach or (others and min(abs(_xc(best) - a) for a in others) < dist):
                continue
            matched[i][j] = best
            used.add(id(best))

    # --- statuses
    out: list[dict] = []
    for i, r in enumerate(rows):
        cells = []
        for j in range(ncols):
            cell = r["cells"][j] if j < len(r["cells"]) else ""
            header = headers[j] if j < len(headers) else ""
            stored = parse_cell(cell)
            fl = list(flags[i][j]) if i < len(flags) and j < len(flags[i]) else []
            t = matched[i][j]
            rec: dict[str, Any] = {"col": j, "header": header, "stored": cell,
                                   "value": stored[0], "flags": fl,
                                   "reread": None, "reread_value": None,
                                   "score": None, "bbox": None}
            if not cell.strip():
                rec["status"] = "verified" if t is None else "disputed"
                if t is not None:
                    rec.update(reread=t["text"], reread_value=parse_cell(t["text"])[0],
                               score=t["score"], bbox=t["bbox"])
                    rec["flags"] = fl + ["missing"]
                cells.append(rec)
                continue
            if t is not None:
                agree, tv = _values_agree(stored, t["text"])
                rec.update(reread=t["text"], reread_value=tv, score=t["score"],
                           bbox=t["bbox"])
                if agree and "unparsable" not in fl:
                    rec["status"] = "verified"
                else:
                    rec["status"] = "disputed"
            elif fl:
                rec["status"] = "disputed"
            elif stored[0] is None and not has_digits(cell):
                rec["status"] = "verified"       # a text label, nothing to verify
            else:
                rec["status"] = "unverified"
            cells.append(rec)
        status = max((c["status"] for c in cells), key=lambda s: STATUS_ORDER[s],
                     default="unverified")
        out.append({"clause": r["clause"], "cells": cells, "cell_status": status})
    return out


def verify_page(entry: dict, pdf_path: str, page: int, tokens_for=None) -> dict:
    """Verify every table on one page of a catalogued document. `tokens_for(bbox)`
    may replace the engine (tests); default is reread_table. Returns the record
    scripts/verify_cells.py stores: {doc_id, page, pdf_sha256?, rows: [...]} where
    each row carries its `ordinal` among the page's table-row clauses and the
    text_sha that binds it to the stored text."""
    on_page = [c for c in entry.get("clauses", []) if c.get("page") == page]
    trows = [c for c in on_page if c.get("kind") == "table-row"]
    ordinal = {id(c): k for k, c in enumerate(trows)}
    rows_out: list[dict] = []
    for group in table_groups(on_page):
        grid = grid_from_rows(group)
        if not grid:
            continue
        flags = check_table([r["cells"] for r in grid["rows"]], grid.get("headers"))
        bbox = group[0].get("bbox") or []
        if tokens_for is not None:
            tokens = tokens_for(bbox)
        elif len(bbox) == 4:
            tokens = reread_table(pdf_path, page, bbox)
        else:
            tokens = []
        for rec in align(grid, tokens, flags):
            c = rec.pop("clause")
            rec["ordinal"] = ordinal[id(c)]
            rec["text_sha"] = text_sha(c.get("text"))
            rec["caption"] = grid.get("caption")
            rows_out.append(rec)
    return {"doc_id": entry.get("doc_id"), "page": page,
            "pdf_sha256": entry.get("pdf_sha256", ""), "rows": rows_out}


# --------------------------------------------------------------------------- #
# stored results: <case>/cell_checks.json
# --------------------------------------------------------------------------- #
def checks_path(case_dir: str) -> str:
    return os.path.join(case_dir, CELL_CHECKS_FILE)


def load_checks(case_dir: str) -> dict:
    try:
        with open(checks_path(case_dir)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"pages": {}}


def save_checks(case_dir: str, data: dict) -> None:
    path = checks_path(case_dir)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def page_key(doc_id: str, page: int) -> str:
    return f"{doc_id}:{page}"


def annotate_clauses(case_dir: str, doc_id: str, page: int, clauses: list[dict]) -> None:
    """Stamp `cells` and `cell_status` onto the page's clause dicts (the
    /doc/{id}/clauses payload) from the stored checks. A row whose stored text no
    longer matches the text that was verified gets nothing — a re-ingest must not
    inherit another extraction's verdict. Non-table clauses get cell_status None."""
    rec = load_checks(case_dir).get("pages", {}).get(page_key(doc_id, page))
    by_ord = {r["ordinal"]: r for r in (rec or {}).get("rows", [])}
    k = 0
    for c in clauses:
        c.setdefault("cells", [])
        c.setdefault("cell_status", None)
        if c.get("kind") != "table-row":
            continue
        r = by_ord.get(k)
        k += 1
        if r and r.get("text_sha") == text_sha(c.get("text")):
            c["cells"] = r["cells"]
            c["cell_status"] = r["cell_status"]


def _iou(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != 4 or len(b) != 4:
        return 0.0
    al, at, ar, ab = a[0], max(a[1], a[3]), a[2], min(a[1], a[3])
    bl, bt, br, bb = b[0], max(b[1], b[3]), b[2], min(b[1], b[3])
    iw = max(0.0, min(ar, br) - max(al, bl))
    ih = max(0.0, min(at, bt) - max(ab, bb))
    inter = iw * ih
    union = (ar - al) * (at - ab) + (br - bl) * (bt - bb) - inter
    return inter / union if union > 0 else 0.0


def disputed_for_citation(case_dir: str, cat, doc_id: str, clause: str = "",
                          page: int | None = None, bbox: list[float] | None = None,
                          text: str | None = None) -> list[dict]:
    """The evidence gate (D2): every table-row cell behind a citation that is NOT
    verified. Resolves the cited rows by clause label, else by text, else by page
    + bbox overlap (IoU >= 0.8 — docling's whole-table box, shared by every row of
    the table, so a citation of the table disputes on any of its rows). Empty list
    means the citation is safe to accept. Callable by the ratify/draft gate without
    touching the catalog or the ledger."""
    clauses = cat.clauses(doc_id)
    if page is not None:
        clauses = [c for c in clauses if c.get("page") == page]
    trows = [c for c in clauses if c.get("kind") == "table-row"]
    if not trows:
        return []
    if clause:
        hit = [c for c in trows if str(c.get("clause")) == str(clause)]
    elif text:
        want = text_sha(text)
        hit = [c for c in trows if text_sha(c.get("text")) == want]
    elif bbox and page is not None:
        hit = [c for c in trows if _iou(c.get("bbox") or [], bbox) >= 0.8]
    else:
        hit = []
    if not hit:
        return []
    wanted = {id(c) for c in hit}
    out: list[dict] = []
    for pg in sorted({c.get("page") for c in hit if c.get("page") is not None}):
        # Copies: annotate_clauses stamps cells onto the dicts it is given, and the
        # catalog's own clause dicts must never carry (or persist) a verdict.
        on_page = [{**c, "_hit": id(c) in wanted}
                   for c in cat.clauses(doc_id) if c.get("page") == pg]
        annotate_clauses(case_dir, doc_id, pg, on_page)
        for c in on_page:
            if not c["_hit"] or c.get("kind") != "table-row":
                continue
            for cell in c.get("cells") or []:
                if not cell_ok(cell):
                    out.append({"page": pg, "col": cell["col"], "header": cell["header"],
                                "stored": cell["stored"], "reread": cell.get("reread"),
                                "score": cell.get("score"), "status": cell["status"],
                                "flags": cell.get("flags", [])})
    return out


# --------------------------------------------------------------------------- #
# vacation-accrual (J1b / D2 "confirm value"): a human confirms a disputed cell, and a
# rule may cite ONE cell of a table row — {page, row, col, quoted_text, stored_text}
# on its citation — so the ratify gate verifies that cell, not the whole table box.
# --------------------------------------------------------------------------- #
def cell_ok(cell: dict) -> bool:
    """A cell is evidence when the second engine verified it, or a person confirmed
    its value on the page image (cells[i].confirmed, written by confirm_cell)."""
    return cell.get("status") == "verified" or bool(cell.get("confirmed"))


def cell_value(cell: dict) -> str:
    """The value a rule may rely on: the confirmed value when a person wrote one,
    else the stored text (which, for a verified cell, the re-read agreed with)."""
    conf = cell.get("confirmed") or {}
    return str(conf.get("value")) if conf.get("value") not in (None, "") else str(cell.get("stored") or "")


def _same_value(a: str, b: str) -> bool:
    """'10.15' == '10,15' == '10.15 ' ; '12' == '12.0'. Numbers compare as numbers,
    anything else after stripping spaces and punctuation."""
    va, vb = parse_cell(a)[0], parse_cell(b)[0]
    if va is not None and vb is not None:
        return _close(va, vb, 0.005)
    norm = lambda s: re.sub(r"[\s.,;:'\"()£$]", "", s or "").lower()  # noqa: E731
    return norm(a) == norm(b)


def find_cell(case_dir: str, doc_id: str, page: int, row: int, col: int) -> dict | None:
    """The stored verification record for one cell: `row` is the row's ordinal among
    the page's table-row clauses (the header row is 0), `col` its column in the grid
    the Compare view draws. None when the page was never verified or the cell is
    not in the grid."""
    rec = load_checks(case_dir).get("pages", {}).get(page_key(doc_id, page))
    for r in (rec or {}).get("rows", []):
        if r.get("ordinal") == row:
            for c in r.get("cells", []):
                if c.get("col") == col:
                    return c
    return None


def confirm_cell(case_dir: str, doc_id: str, page: int, row: int, col: int,
                 value: str, by: str, note: str = "") -> dict:
    """Record that `by` read `value` for this cell on the page image. Writes
    cells[i].confirmed = {value, by, at, note, agrees_with_reread} into
    cell_checks.json and returns the updated cell. Never rewrites `stored` or
    `reread`: the OCR text and the second engine's reading stay beside the human's.
    Raises KeyError when the cell has no record, ValueError on an empty value/name."""
    import datetime as dt
    by = (by or "").strip()
    value = str(value if value is not None else "").strip()
    if not by:
        raise ValueError("a confirmation names the person who read the page")
    if not value:
        raise ValueError("a confirmation carries the value read on the page")
    data = load_checks(case_dir)
    rec = data.get("pages", {}).get(page_key(doc_id, page))
    if not rec:
        raise KeyError(f"{doc_id} p.{page} has no cell verification record")
    for r in rec.get("rows", []):
        if r.get("ordinal") != row:
            continue
        for c in r.get("cells", []):
            if c.get("col") != col:
                continue
            c["confirmed"] = {
                "value": value, "by": by,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "note": note or "",
                "agrees_with_reread": (_same_value(value, c.get("reread") or "")
                                       if c.get("reread") else None),
                "agrees_with_stored": _same_value(value, c.get("stored") or ""),
            }
            save_checks(case_dir, data)
            return c
    raise KeyError(f"{doc_id} p.{page} row {row} col {col} is not in the verified grid")


def _cell_label(doc_id: str, page: int, row: int, col: int, cell: dict | None = None) -> str:
    head = (cell or {}).get("header") or f"col {col}"
    return f"{doc_id} p.{page} row {row} '{head}'"


def gate_rule(case_dir: str, cat, rule: dict) -> list[str]:
    """Why this rule's cited evidence is not yet usable — [] when it is.

    A citation that names a cell ({page, row, col} under citation.cell) is judged on
    that cell alone: it must be verified by the second engine or confirmed by a
    person; the value the rule quotes (citation.cell.quoted_text) and the number it
    computes (a bare numeric `compute`) must both equal that cell's value. A citation
    without a cell that lands on table rows is judged on every cell of those rows
    (disputed_for_citation) — the whole-table box cannot say which row it meant."""
    cit = rule.get("citation") or {}
    doc_id = cit.get("doc_id") or ""
    cell_ref = cit.get("cell") or None
    errs: list[str] = []
    if cell_ref:
        try:
            page = int(cell_ref.get("page", cit.get("page")))
            row = int(cell_ref["row"])
            col = int(cell_ref["col"])
        except (KeyError, TypeError, ValueError):
            return ["citation.cell must carry integer page, row and col"]
        cell = find_cell(case_dir, doc_id, page, row, col)
        label = _cell_label(doc_id, page, row, col, cell)
        if cell is None:
            return [f"cited cell {label} has no verification record — run "
                    f"scripts/verify_cells.py --pages {doc_id}:{page} first"]
        if not cell_ok(cell):
            reread = cell.get("reread")
            errs.append(
                f"cited cell {label} reads {cell.get('stored')!r} in the text layer"
                + (f"; the second engine read {reread!r}" if reread else "")
                + f" ({cell.get('status')}). Confirm the value on the page image "
                  f"(POST /admin/cell_confirm) before approving.")
            return errs
        have = cell_value(cell)
        quoted = str(cell_ref.get("quoted_text") or "").strip()
        if quoted and not _same_value(quoted, have):
            errs.append(f"rule quotes {quoted!r} but cell {label} holds {have!r} "
                        f"({'confirmed by ' + cell['confirmed']['by'] if cell.get('confirmed') else 'verified'})")
        comp = str(rule.get("compute") or "").strip()
        if comp and parse_cell(comp)[0] is not None and not _same_value(comp, have):
            errs.append(f"rule computes {comp!r} but cell {label} holds {have!r}")
        return errs
    page = cit.get("page")
    bbox = cit.get("bbox") or None
    if page is None or not bbox:
        return []
    bad = disputed_for_citation(case_dir, cat, doc_id, clause="", page=int(page), bbox=list(bbox))
    if bad:
        first = bad[0]
        errs.append(f"citation lands on a table whose cells are not all verified "
                    f"({len(bad)} cell(s), e.g. '{first.get('header')}' reads "
                    f"{first.get('stored')!r}, {first.get('status')}). Cite one cell "
                    f"(citation.cell {{page, row, col}}) and confirm it in Compare first.")
    return errs


def gate_rules(case_dir: str, cat, rules: list[dict]) -> dict[str, list[str]]:
    """{rule_id: [errors]} over a selection — empty dict means every cited cell is
    evidence. The ratify gate calls this after the known-answer checks, so a wrong
    number is still caught as a wrong number, and only a RIGHT number resting on an
    unconfirmed OCR cell is refused here."""
    out: dict[str, list[str]] = {}
    for r in rules:
        e = gate_rule(case_dir, cat, r)
        if e:
            out[str(r.get("id") or "<no id>")] = e
    return out
