"""The arithmetic line with an address on every factor (DEMO_TICKETS C1 / I13).

The engine's math step says `effective_base * 1.5 * hours = 640.8`. A person wants
`$53.40/hr × 1.5 × 8 h = $640.80`, and an auditor wants to click each factor:

    $53.40  -> the MAXIMUM HOURLY RATE cell of the FIREFIGHTER/PARAMEDIC- 56 hr row of
               the Master Salary Schedule (page + bbox found by text search in the PDF)
    1.5     -> the MOU clause the rule cites (p.8)
    1.025   -> the longevity clause (p.12), when the differential fired
    8 h     -> the question

`operands()` walks the expression; `expand()` substitutes a fact that a differential
set (effective_base after longevity) with the differential's own operands, so the line
reads 53.40 × 1.025 × 1.5 × 8 rather than 54.735 × 1.5 × 8. `rate_source()` binds the
roster rate to its schedule cell. Pure helpers plus one read of the schedule PDF; the
engine stays untouched and no model is called.
"""
from __future__ import annotations

import ast
import os
import re
from functools import lru_cache
from typing import Any

MAX_DEPTH = 3


# --------------------------------------------------------------------------- #
# operands
# --------------------------------------------------------------------------- #
def _strip_round(node: ast.AST) -> ast.AST:
    """`round(x, 6)` only strips float noise (case rounding policy) — read through it."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "round" and node.args):
        return _strip_round(node.args[0])
    return node


def operands(expr: str | None, inputs: dict[str, Any]) -> list[dict] | None:
    """[{kind: fact|const, name?, value}] for a pure product expression, in source
    order; None when the expression is not a plain product (a sum, a comparison, a
    conditional) — then the caller falls back to the engine's detail string."""
    try:
        tree = _strip_round(ast.parse(str(expr or ""), mode="eval").body)
    except SyntaxError:
        return None
    out: list[dict] = []

    def walk(n: ast.AST) -> bool:
        n = _strip_round(n)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult):
            return walk(n.left) and walk(n.right)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                and not isinstance(n.value, bool):
            out.append({"kind": "const", "value": n.value})
            return True
        if isinstance(n, ast.Name) and n.id in inputs:
            out.append({"kind": "fact", "name": n.id, "value": inputs[n.id]})
            return True
        return False

    return out if walk(tree) else None


def expand(ops: list[dict], setters: dict[str, dict], depth: int = 0) -> list[dict]:
    """Replace a fact that a differential SET with that differential's operands.
    `setters` = {fact_name: {expr, inputs, rule_id, citation}} from modifier steps."""
    if depth >= MAX_DEPTH:
        return ops
    out: list[dict] = []
    for o in ops:
        s = setters.get(o.get("name")) if o["kind"] == "fact" else None
        inner = operands(s["expr"], s["inputs"]) if s else None
        if not inner:
            out.append(o)
            continue
        # The differential's own inputs were read BEFORE it overwrote the fact
        # (engine._inputs), so `effective_base` inside it is the roster rate.
        tagged = [({**i, "rule_id": s.get("rule_id"), "citation": s.get("citation")}
                   if i["kind"] == "const" else i) for i in inner]
        out.extend(expand(tagged, {k: v for k, v in setters.items() if k != o.get("name")},
                          depth + 1))
    return out


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def fmt_money(v: float) -> str:
    return f"${float(v):,.2f}"


def fmt_num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:g}"


def fmt_operand(o: dict) -> str:
    role = o.get("role")
    if role == "rate":
        return fmt_money(o["value"]) + ("/hr" if o.get("unit") == "hour" else "")
    if role == "hours":
        return f"{fmt_num(o['value'])} h"
    return fmt_num(o.get("value"))


def line_text(ops: list[dict], total: float, result_type: str = "currency") -> str:
    rhs = fmt_money(total) if result_type == "currency" else fmt_num(total)
    return " × ".join(fmt_operand(o) for o in ops) + f" = {rhs}"


# --------------------------------------------------------------------------- #
# rate source: the salary-schedule cell behind a roster rate
# --------------------------------------------------------------------------- #
_HR_RE = re.compile(r"\b(\d{2})\s*-?\s*hr\b", re.I)
_PAREN_RE = re.compile(r"\([^)]*\)")


def _norm_title(s: str) -> str:
    s = s.upper().replace("-", " ").replace("–", " ")
    return re.sub(r"\s+", " ", s).strip()


def classification_key(subject: dict) -> str:
    """'Firefighter/Paramedic (56 hr, top step)' -> 'FIREFIGHTER/PARAMEDIC 56 HR', the
    schedule's own TITLE spelling after dashes and spacing are normalised."""
    name = str(subject.get("name") or "")
    rank = str(subject.get("rank") or _PAREN_RE.sub("", name))
    hr = _HR_RE.search(name)
    key = rank + (f" {hr.group(1)} HR" if hr else "")
    return _norm_title(key)


def step_column(subject: dict) -> tuple[str, str]:
    """(column word in the row text, human column label) for the subject's step."""
    name = str(subject.get("name") or "").lower()
    if "entry" in name or "minimum" in name or "step 1" in name:
        return "MINIMUM HOURLY RATE", "MINIMUM HOURLY RATE"
    return "MAXIMUM HOURLY RATE", "MAXIMUM HOURLY RATE"


def schedule_row(cat, doc_id: str, subject: dict) -> dict | None:
    """The catalog table-row chunk whose TITLE is the subject's classification."""
    key = classification_key(subject)
    for c in cat.clauses(doc_id):
        text = c.get("text") or ""
        m = re.search(r"TITLE:\s*([^|]+?)\s*\|", text)
        if not m:
            continue
        if _norm_title(m.group(1)) == key:
            return c
    return None


@lru_cache(maxsize=64)
def _cell_boxes(pdf_path: str, mtime: float, page: int, needle: str) -> tuple:
    """Every occurrence of `needle` on the page as (l, t, r, b) in docling's
    bottom-left-origin convention (t > b), via pypdfium2's text search."""
    try:
        import pypdfium2 as pdfium
    except Exception:
        return ()
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        pg = pdf[max(0, page - 1)]
        tp = pg.get_textpage()
        found = []
        s = tp.search(needle)
        while True:
            r = s.get_next()
            if r is None:
                break
            idx, n = r
            boxes = [tp.get_charbox(i) for i in range(idx, idx + n)]
            l = min(b[0] for b in boxes)
            bottom = min(b[1] for b in boxes)
            rr = max(b[2] for b in boxes)
            top = max(b[3] for b in boxes)
            found.append((round(l, 2), round(top, 2), round(rr, 2), round(bottom, 2)))
        pdf.close()
        return tuple(found)
    except Exception:
        return ()


@lru_cache(maxsize=256)
def _line_text(pdf_path: str, mtime: float, page: int, top: float, bottom: float,
               right: float) -> str:
    """The text in the TITLE column band of one row: page left edge to `right`."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        tp = pdf[max(0, page - 1)].get_textpage()
        text = tp.get_text_bounded(left=0, bottom=bottom - 1, right=right + 1, top=top + 1)
        pdf.close()
        return text or ""
    except Exception:
        return ""


def rate_source(case, cat, subject: dict, value: float) -> dict:
    """Where the subject's hourly rate is printed: {doc_id, title, page, bbox, text,
    row, column, context_bbox} — or {unsourced: True, reason} when no schedule row or
    cell binds. Never guesses: the cell must be on the same line as the row title."""
    schedules = [s for s in case.manifest.get("sources", [])
                 if s.get("doc_type") == "salary-schedule"]
    if not schedules:
        return {"unsourced": True, "reason": "no salary schedule declared in the case"}
    src = schedules[0]
    doc_id = src["id"]
    row = schedule_row(cat, doc_id, subject)
    if row is None:
        return {"unsourced": True, "doc_id": doc_id,
                "reason": f"no schedule row titled {classification_key(subject)!r}"}
    col_word, col_label = step_column(subject)
    text = row.get("text") or ""
    m = re.search(re.escape(col_word) + r":\s*\$?([\d,]+\.\d{2})", text)
    printed = m.group(1).replace(",", "") if m else None
    want = f"{float(value):,.2f}"
    if printed is None or float(printed) != float(value):
        return {"unsourced": True, "doc_id": doc_id, "page": row.get("page"),
                "reason": f"row prints {col_label} {printed}, roster holds {want}"}
    page = int(row.get("page") or 1)
    pdf = src.get("file")
    pdf_path = pdf if os.path.isabs(pdf) else os.path.join(case.dir, pdf)
    try:
        mtime = os.path.getmtime(pdf_path)
    except OSError:
        return {"unsourced": True, "doc_id": doc_id, "page": page,
                "reason": "schedule PDF not on disk"}
    title_m = re.search(r"TITLE:\s*([^|]+?)\s*\|", text)
    row_title = title_m.group(1).strip() if title_m else ""
    title_boxes = _cell_boxes(pdf_path, mtime, page, row_title) if row_title else ()
    cell_boxes = _cell_boxes(pdf_path, mtime, page, "$" + want)
    out = {"doc_id": doc_id, "title": src.get("title") or doc_id, "page": page,
           "row": row_title, "column": col_label, "text": "$" + want,
           "row_bbox": list(row.get("bbox") or [])}
    if not cell_boxes:
        return {**out, "unsourced": True, "reason": f"'${want}' not found on p.{page}"}
    cell = None
    # A title needle is a substring search ('FIRE MARSHAL' is inside 'DEPUTY FIRE
    # MARSHALL'), so the line whose own title text equals the row title is the row.
    for tl, tt, tr, tb in title_boxes:
        line_title = _line_text(pdf_path, mtime, page, tt, tb, tr)
        if _norm_title(line_title) != _norm_title(row_title):
            continue
        mid = (tt + tb) / 2
        same_line = [b for b in cell_boxes if b[3] - 2 <= mid <= b[1] + 2]
        if same_line:
            cell = same_line[0]
            out["context_bbox"] = [tl, max(tt, cell[1]) + 2, cell[2], min(tb, cell[3]) - 2]
            break
    if cell is None and len(cell_boxes) == 1 and not title_boxes:
        cell = cell_boxes[0]
    if cell is None:
        return {**out, "unsourced": True,
                "reason": f"'${want}' appears {len(cell_boxes)}× on p.{page}; "
                          f"none on the {row_title!r} line"}
    out["bbox"] = list(cell)
    return out


# --------------------------------------------------------------------------- #
# the whole line for one engine line item
# --------------------------------------------------------------------------- #
def _setters(trace: list[dict]) -> dict[str, dict]:
    setters: dict[str, dict] = {}
    for t in trace:
        if t.get("kind") != "modifier":
            continue
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*->", t.get("detail") or "")
        if not m:
            continue
        setters[m.group(1)] = {"expr": m.group(2), "inputs": t.get("inputs") or {},
                               "rule_id": t.get("rule_id"), "citation": t.get("citation") or {}}
    return setters


def build(case, cat, subject: dict, line_item: dict, eng_params: dict) -> dict | None:
    """{expr, operands: [{kind, name?, value, role, source}], line, total} for the
    line item's math step, or None when the expression is not a plain product."""
    trace = line_item.get("trace") or []
    math = next((t for t in trace if t.get("kind") == "math"), None)
    if math is None:
        return None
    expr = (math.get("detail") or "").split(" = ")[0]
    ops = operands(expr, math.get("inputs") or {})
    if ops is None:
        return None
    setters = _setters(trace)
    ops = expand(ops, setters)
    base_hourly = subject.get("base_hourly")
    rate_src = None
    for o in ops:
        if o["kind"] == "const":
            cit = o.get("citation") or math.get("citation") or {}
            o["role"] = "multiplier"
            o["rule_id"] = o.get("rule_id") or math.get("rule_id")
            o["source"] = {"kind": "clause", "doc_id": cit.get("doc_id"), "page": cit.get("page"),
                           "bbox": cit.get("bbox"), "clause": cit.get("clause"),
                           "rule_id": o["rule_id"]}
            continue
        name = o.get("name") or ""
        if name in ("effective_base", "subject_base_hourly") or \
                (base_hourly is not None and o.get("value") == base_hourly):
            o["role"] = "rate"
            o["unit"] = "hour"
            if rate_src is None:
                rate_src = rate_source(case, cat, subject, float(o["value"]))
            o["source"] = {"kind": "schedule-cell", "field": "base_hourly",
                           "classification": subject.get("name"), **rate_src}
        elif name == "hours":
            o["role"] = "hours"
            o["source"] = {"kind": "question", "param": "hours"}
        elif name.startswith("subject_"):
            o["role"] = "fact"
            o["source"] = {"kind": "roster", "field": name[len("subject_"):],
                           "classification": subject.get("name")}
        elif name in eng_params:
            o["role"] = "fact"
            o["source"] = {"kind": "question", "param": name}
        else:
            o["role"] = "fact"
            o["source"] = {"kind": "derived"}
    total = math.get("value")
    return {"expr": expr, "operands": ops, "total": total,
            "line": line_text(ops, total, line_item.get("result_type") or "currency")}
