"""Highlights you can read (DEMO_TICKETS.md C2 / I12).

The old renderer drew a 4px outline INWARD on the unpadded box, so the red line ran
through the first line of every cited clause. Now the box is normalised, padded, filled,
and stroked wholly outside; a crop mode returns the passage at a readable size; and a
table-row citation gets the row's own band, not the whole table's box."""
import io
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import pdfview, rowbands  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE = os.path.join(ROOT, "cases", "santacruz")
FIRE = "firefighters_local3535_mou"
FIRE_PDF = os.path.join(CASE, "sources", FIRE + ".pdf")
# The $640.80 citation: firefighters_local3535_mou:overtime_premium_rate, p.8.
BOX = [141.418, 505.662, 511.182, 441.459]
SCALE = 2.0


def _img(png: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(png)).convert("RGB")


def _is_red(px) -> bool:
    r, g, b = px[:3]
    return r > 170 and g < 80 and b < 80


def _page_height(pdf_path: str, page: int) -> float:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_path)
    h = pdf[page - 1].get_size()[1]
    pdf.close()
    return h


@pytest.fixture(scope="module")
def flagship():
    png = pdfview.render_page_with_bbox(FIRE_PDF, 8, BOX, scale=SCALE)
    assert png is not None
    return _img(png), _page_height(FIRE_PDF, 8)


def test_outline_is_outside_the_box(flagship):
    img, h = flagship
    l, t, r, b = BOX
    x0, y0 = l * SCALE, (h - t) * SCALE
    x1, y1 = r * SCALE, (h - b) * SCALE
    # (1) no outline-coloured pixel inside the stored box grown by 2px: the stroke
    # cannot be on the glyphs
    grown = [(x, y) for y in range(int(y0) - 2, int(y1) + 3)
             for x in range(int(x0) - 2, int(x1) + 3)]
    assert not any(_is_red(img.getpixel(p)) for p in grown)
    # (2) outline pixels on all four sides outside it
    px0, py0, px1, py1 = pdfview.highlight_rect_px(BOX, h, SCALE, img.width, img.height)
    mx, my = int((px0 + px1) / 2), int((py0 + py1) / 2)
    assert _is_red(img.getpixel((mx, int(py0) - 3)))
    assert _is_red(img.getpixel((mx, int(py1) + 2)))
    assert _is_red(img.getpixel((int(px0) - 3, my)))
    assert _is_red(img.getpixel((int(px1) + 2, my)))
    # the stroke starts at least 2pt (PAD_PT minus the ink overhang) outside the box
    assert px0 <= x0 - 2 * SCALE and py0 <= y0 - 2 * SCALE


def test_inverted_bbox_renders(flagship):
    img, _ = flagship
    swapped = pdfview.render_page_with_bbox(FIRE_PDF, 8, [BOX[2], BOX[3], BOX[0], BOX[1]],
                                            scale=SCALE)
    assert swapped is not None
    assert _img(swapped).tobytes() == img.tobytes()


def test_crop_returns_region():
    h = _page_height(FIRE_PDF, 8)
    full = _img(pdfview.render_page_with_bbox(FIRE_PDF, 8, BOX, scale=SCALE))
    crop = _img(pdfview.render_page_with_bbox(FIRE_PDF, 8, BOX,
                                              crop_margin_pt=pdfview.CROP_MARGIN_PT))
    page_w_px = full.width / SCALE * pdfview.CROP_SCALE
    assert abs(crop.width - page_w_px) <= 2
    want_h = ((BOX[1] - BOX[3]) + 2 * pdfview.PAD_PT + 2 * pdfview.CROP_MARGIN_PT) \
        * pdfview.CROP_SCALE + 2 * pdfview.STROKE_PX
    assert abs(crop.height - want_h) <= 3
    assert crop.height < full.height
    assert any(_is_red(crop.getpixel((x, y))) for y in range(crop.height)
               for x in range(0, crop.width, 7))
    # explicit crop rectangle: the aspect ratio is the rectangle's
    region = [100, 600, 500, 400]
    exp = _img(pdfview.render_page_with_bbox(FIRE_PDF, 8, BOX, crop=region))
    assert abs(exp.width / exp.height - 400 / 200) < 0.02
    assert h > 0


def test_finite_box_rejects_bad_input():
    assert pdfview.finite_box(["1", "2", "3"]) is None
    assert pdfview.finite_box(["nan", "nan", "nan", "nan"]) is None
    assert pdfview.finite_box(["1", "inf", "3", "4"]) is None
    assert pdfview.finite_box(["x", "2", "3", "4"]) is None
    assert pdfview.finite_box(["1", "2", "3", "4"]) == [1.0, 2.0, 3.0, 4.0]


# --------------------------------------------------------------------------- #
# table rows: the row's own band
# --------------------------------------------------------------------------- #
def _p22_rows() -> list[dict]:
    with open(os.path.join(CASE, "catalog.json")) as f:
        entry = next(d for d in json.load(f)["documents"] if d["doc_id"] == FIRE)
    return [c for c in entry["clauses"] if c.get("page") == 22 and c.get("kind") == "table-row"]


def test_p22_rows_get_their_own_band():
    rows = _p22_rows()
    assert len(rows) >= 6
    table = rows[1]["bbox"]
    bands = []
    for row in rows[1:]:
        band = rowbands.row_band(CASE, FIRE, 22, row["text"], row["bbox"])
        assert band is not None, row["text"][:60]
        assert band[0] == round(table[0], 2) and band[2] == round(table[2], 2)
        assert 8 < band[1] - band[3] < 15          # one 9pt row, not a 140pt table
        bands.append(tuple(band))
    assert len(set(bands)) == len(bands)            # every row its own band
    # the synthetic "(table: …)" line has no cells, so it keeps the table box
    assert rowbands.row_band(CASE, FIRE, 22, rows[0]["text"], rows[0]["bbox"]) is None
    assert rowbands.narrow(CASE, FIRE, 22, rows[0]["bbox"], rows[0]["text"]) == rows[0]["bbox"]
    # a page nobody verified keeps the catalog's box
    assert rowbands.narrow(CASE, FIRE, 8, BOX, "whatever") == BOX


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def test_route_crop_and_validation(client):
    box = ",".join(str(v) for v in BOX)
    full = client.get(f"/doc/{FIRE}/page/8", params={"bbox": box})
    assert full.status_code == 200 and full.headers["content-type"] == "image/png"
    crop = client.get(f"/doc/{FIRE}/page/8", params={"bbox": box, "crop": "1"})
    assert crop.status_code == 200 and crop.headers["content-type"] == "image/png"
    assert _img(crop.content).height < _img(full.content).height
    region = client.get(f"/doc/{FIRE}/page/8", params={"bbox": box, "crop": "100,600,500,400"})
    assert region.status_code == 200
    assert client.get(f"/doc/{FIRE}/page/8", params={"bbox": "1,2,3"}).status_code == 400
    assert client.get(f"/doc/{FIRE}/page/8", params={"bbox": "nan,nan,nan,nan"}).status_code == 400
    assert client.get(f"/doc/{FIRE}/page/8", params={"bbox": box, "crop": "1,2,3"}).status_code == 400
    assert client.get(f"/doc/{FIRE}/page/8", params={"bbox": box, "crop": "inf,1,2,3"}).status_code == 400


def test_out_of_range_page_is_404(client):
    assert client.get(f"/doc/{FIRE}/page/99999").status_code == 404
    assert client.get(f"/doc/{FIRE}/page/0").status_code == 404
    assert client.get(f"/doc/{FIRE}/page/1").status_code == 200


def test_source_chip_for_a_table_row_carries_its_band(client):
    """A chat source chip built from a p.22 row hit carries `row_bbox`: the row's
    band when the page was verified, the catalog box otherwise (additive field)."""
    from core.caseio import load_case
    case = load_case(core_app.CASE_DIR)
    cat = core_app._catalog(case)
    rows = _p22_rows()
    hit = {"doc_id": FIRE, "clause": "", "page": 22, "bbox": rows[1]["bbox"],
           "text": rows[1]["text"], "score": 0.9, "kind": "table-row"}
    s = core_app._source_entry(case, cat, hit)
    assert s["bbox"] == rows[1]["bbox"]                       # unchanged
    assert s["row_bbox"] != s["bbox"] and s["row_bbox"][1] - s["row_bbox"][3] < 15
    assert s["title"] == "Firefighters Local 3535 MOU 2025–2028 (with Salary Schedule)"
    prose = {"doc_id": FIRE, "clause": "", "page": 8, "bbox": BOX, "text": "x",
             "score": 0.5, "kind": "text"}
    assert core_app._source_entry(case, cat, prose)["row_bbox"] == BOX


def test_app_js_shows_the_crop_first():
    with open(os.path.join(ROOT, "core", "templates", "app.js")) as f:
        js = f.read()
    assert "function citationFigure(" in js
    assert "&crop=1" in js and "See it on the whole page" in js
    assert "Open the PDF at page" in js and "/file#page=" in js
    assert "s.row_bbox || s.bbox" in js and "c.row_bbox || c.bbox" in js
