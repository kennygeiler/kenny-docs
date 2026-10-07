"""Evidence-based citation check (DEMO_TICKETS.md A2).

A ratified rule cites a document, a page, a box on that page and (once bound) the
text inside the box. Whether that evidence still exists after a re-ingest used to be
decided by clause-LABEL equality — but the shipped rules carry analyst labels such as
'Overtime Rate (p.8)' and the OCR'd catalog has no real labels at all, so an identical
re-read staled every rule and emptied the library. `locate()` instead finds the cited
clause by what a person would check: the same text on the page (quote hash), or the
same box on the page (IoU), falling back to the label for contracts that number their
sections.
"""
from __future__ import annotations

import hashlib
import re

IOU_MIN = 0.8
QUOTE_CHARS = 200

_WS = re.compile(r"\s+")


def norm_box(b) -> list[float] | None:
    """[l, t, r, b] in any y-orientation -> [min_x, min_y, max_x, max_y], or None."""
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return None
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def iou(a, b) -> float:
    """Intersection over union of two boxes (either orientation); 0 when either is
    missing or degenerate."""
    a, b = norm_box(a), norm_box(b)
    if a is None or b is None:
        return 0.0
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def text_sha(text) -> str:
    """16-hex SHA-256 of the whitespace-collapsed, lower-cased text — the identity of a
    quote that survives re-extraction jitter (line breaks, double spaces, case)."""
    norm = _WS.sub(" ", str(text or "")).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _page(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def locate(clauses: list[dict], cit: dict) -> tuple[dict | None, str]:
    """Find the catalog clause a citation points at.

    Returns (clause, how) with how one of:
      'quote'        same text on the cited page (box may have jittered)
      'quote-moved'  same text found, but on another page or in a box that no longer
                     overlaps the cited one — the caller should rebind page/bbox
      'bbox'         a clause on the cited page overlaps the cited box (IoU >= 0.8)
      'label'        a clause with the same label (numbered contracts)
    or (None, how) with how one of:
      'text-changed' the box is still there but the text under a bound quote differs
      'missing'      nothing on the page matches
    """
    page = _page(cit.get("page"))
    quote_sha = str(cit.get("quote_sha256") or "")
    bbox = cit.get("bbox")
    label = str(cit.get("clause") or "")

    on_page = [c for c in clauses if _page(c.get("page")) == page] if page else []

    # (a) the quoted text, on its page first, then anywhere in the document.
    if quote_sha:
        for c in on_page:
            if c.get("text") and text_sha(c.get("text")) == quote_sha:
                if bbox and iou(c.get("bbox"), bbox) < IOU_MIN:
                    return c, "quote-moved"
                return c, "quote"
        for c in clauses:
            if c.get("text") and text_sha(c.get("text")) == quote_sha:
                return c, "quote-moved"

    # (b) the same box on the same page.
    if bbox and on_page:
        best, best_iou = None, 0.0
        for c in on_page:
            score = iou(c.get("bbox"), bbox)
            if score > best_iou:
                best, best_iou = c, score
        if best is not None and best_iou >= IOU_MIN:
            if quote_sha and text_sha(best.get("text")) != quote_sha:
                return None, "text-changed"
            return best, "bbox"

    # (c) the label, for contracts whose sections are numbered.
    if label:
        same = [c for c in clauses if str(c.get("clause") or "") == label]
        if same:
            if page:
                on = [c for c in same if _page(c.get("page")) == page]
                if on:
                    return on[0], "label"
                return None, "missing"
            return same[0], "label"

    return None, "missing"


def quote_of(clause: dict) -> str:
    return str(clause.get("text") or "")[:QUOTE_CHARS]
