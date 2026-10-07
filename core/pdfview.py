"""Render a source PDF page with a docling bounding box highlighted (PRD 4.4).

This is what makes a citation verifiable: the user sees the exact clause boxed on
the original page. Uses pypdfium2 to rasterize + PIL to draw. Degrades to None if
the libraries or file are unavailable (the UI then shows the citation without the
image).

Coordinate mapping: docling bboxes are in PDF points with a bottom-left origin
(l, t, r, b measured from the bottom). pypdfium2 renders top-left origin pixels, so
y is flipped and both axes scaled by the render scale.

Highlight geometry (DEMO_TICKETS.md C2 / I12): the box is normalised (either corner
order renders), padded by PAD_PT on each side, filled translucently, and then stroked
STROKE_PX wide WHOLLY OUTSIDE the padded box — so the red line never runs through the
first line of the clause or over the first and last letters, as the old inward
4px outline on an unpadded box did. `crop` returns just the region around the box
at CROP_SCALE, which is what makes a 9pt table row legible in a 528px drawer.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import threading

# pypdfium2 is NOT thread-safe, and FastAPI runs sync endpoints in a threadpool. The
# audit drawer requests several citation images at once, so concurrent renders land in
# different threads and fail — every citation silently showing "page render
# unavailable". Serialize rendering, and cache the result so repeat clicks are free.
_RENDER_LOCK = threading.Lock()
_CACHE: dict[str, bytes] = {}
_CACHE_MAX = 64

PAD_PT = 4.0            # clearance between the text and the fill, in PDF points
STROKE_PX = 5           # outline width in pixels, drawn outside the padded box
CROP_SCALE = 3.0        # render scale for a crop (3x: a 9pt row is ~27px tall)
CROP_MARGIN_PT = 54.0   # context above and below the box in an automatic crop
OUTLINE = (200, 40, 40, 255)
FILL = (255, 220, 0, 60)


def render_page_with_bbox(pdf_path: str, page: int, bbox: list[float],
                          scale: float = 2.0, crop: list[float] | None = None,
                          crop_margin_pt: float | None = None) -> bytes | None:
    """PNG bytes of `page` (1-based, clamped) with `bbox` highlighted, or None.

    crop            an explicit [l, t, r, b] region in PDF points (bottom-left origin,
                    like the bbox); the image is cut to it after drawing.
    crop_margin_pt  automatic crop: the full page width by (box height + 2 x margin),
                    clamped to the page. Ignored without a bbox.
    Either crop renders at CROP_SCALE instead of `scale`.
    """
    if not os.path.exists(pdf_path):
        return None
    key = hashlib.sha1(
        f"{pdf_path}|{os.path.getmtime(pdf_path)}|{page}|{bbox}|{scale}|{PAD_PT}|"
        f"{STROKE_PX}|{crop}|{crop_margin_pt}".encode()
    ).hexdigest()
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        import pypdfium2 as pdfium
        from PIL import ImageDraw
    except Exception:
        return None
    with _RENDER_LOCK:
        png = _render(pdfium, ImageDraw, pdf_path, page, bbox, scale, crop, crop_margin_pt)
    if png is not None:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = png
    return png


def page_dims(pdf_path: str, page: int) -> tuple[int, int, float, float] | None:
    """(page, page_count, width, height) for a 1-based page, clamped into range;
    width/height in PDF points. The X-ray overlay needs these to scale catalog bboxes
    onto the rendered image client-side. Same lock as rendering — pypdfium2 is not
    thread-safe (see above)."""
    if not os.path.exists(pdf_path):
        return None
    try:
        import pypdfium2 as pdfium
    except Exception:
        return None
    with _RENDER_LOCK:
        try:
            pdf = pdfium.PdfDocument(pdf_path)
            count = len(pdf)
            idx = min(max(0, (page or 1) - 1), count - 1)
            width, height = pdf[idx].get_size()
            pdf.close()  # don't leak the handle; pdfium warns and holds the file open
            return idx + 1, count, width, height
        except Exception:
            return None


def finite_box(values) -> list[float] | None:
    """Four finite numbers, or None. The route turns None into a 400: 'bbox=1,2,3' and
    'bbox=nan,nan,nan,nan' must not render a confident page with no box on it."""
    try:
        box = [float(v) for v in values]
    except (TypeError, ValueError):
        return None
    if len(box) != 4 or not all(math.isfinite(v) for v in box):
        return None
    return box


def highlight_rect_px(bbox: list[float], page_height_pt: float, scale: float,
                      width_px: int, height_px: int) -> tuple[float, float, float, float]:
    """The padded, normalised, clamped highlight rectangle in image pixels
    (x0, y0, x1, y1), top-left origin. Pure so the tests can predict it."""
    l, t, r, b = bbox
    l, r = sorted((l, r))
    b, t = sorted((b, t))
    pad = _pad_for(t - b)
    x0 = max(0.0, (l - pad) * scale)
    x1 = min(float(width_px), (r + pad) * scale)
    y0 = max(0.0, (page_height_pt - t - pad) * scale)
    y1 = min(float(height_px), (page_height_pt - b + pad) * scale)
    return x0, y0, x1, y1


SHORT_BOX_PT = 20.0     # under this a box is a table row, not a paragraph


def _pad_for(height_pt: float) -> float:
    """PAD_PT, except around a short table row, where 4pt of pad plus the stroke
    would land on the neighbouring rows: there the pad is a hair (0.75pt) and the
    stroke sits on the table's own rule lines."""
    if height_pt <= 0:
        return PAD_PT
    return 0.75 if height_pt < SHORT_BOX_PT else PAD_PT


def _stroke_for(height_pt: float) -> int:
    return 3 if 0 < height_pt < SHORT_BOX_PT else STROKE_PX


def _render(pdfium, ImageDraw, pdf_path, page, bbox, scale, crop=None,
            crop_margin_pt=None) -> bytes | None:
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        idx = max(0, (page or 1) - 1)
        idx = min(idx, len(pdf) - 1)
        pg = pdf[idx]
        page_height_pt = pg.get_size()[1]
        box = finite_box(bbox) if bbox else None
        cropping = crop is not None or (crop_margin_pt is not None and box is not None)
        use_scale = CROP_SCALE if cropping else scale
        bitmap = pg.render(scale=use_scale)
        img = bitmap.to_pil().convert("RGB")
        rect, stroke = None, STROKE_PX
        if box:
            rect = highlight_rect_px(box, page_height_pt, use_scale, img.width, img.height)
            stroke = _stroke_for(abs(box[1] - box[3]))
            x0, y0, x1, y1 = rect
            draw = ImageDraw.Draw(img, "RGBA")
            draw.rectangle([x0, y0, x1, y1], fill=FILL)
            # PIL strokes inward from the rectangle it is given, so grow it by the
            # stroke first: the line then lies entirely outside the padded box.
            draw.rectangle([x0 - stroke, y0 - stroke, x1 + stroke, y1 + stroke],
                           outline=OUTLINE, width=stroke)
        region = None
        if crop is not None:
            cl, ct, cr, cb = crop
            cl, cr = sorted((cl, cr))
            cb, ct = sorted((cb, ct))
            region = (cl * use_scale, (page_height_pt - ct) * use_scale,
                      cr * use_scale, (page_height_pt - cb) * use_scale)
        elif crop_margin_pt is not None and rect is not None:
            m = crop_margin_pt * use_scale
            region = (0.0, rect[1] - stroke - m, float(img.width), rect[3] + stroke + m)
        if region is not None:
            rx0 = int(max(0, math.floor(region[0])))
            ry0 = int(max(0, math.floor(region[1])))
            rx1 = int(min(img.width, math.ceil(region[2])))
            ry1 = int(min(img.height, math.ceil(region[3])))
            if rx1 > rx0 and ry1 > ry0:
                img = img.crop((rx0, ry0, rx1, ry1))
        out = io.BytesIO()
        img.save(out, format="PNG")
        pdf.close()  # don't leak the handle; pdfium warns and holds the file open
        return out.getvalue()
    except Exception:
        return None
