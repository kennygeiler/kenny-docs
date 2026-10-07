"""Hard-document fixtures (DEMO_TICKETS.md D6): four image-only PDFs generated
locally from firefighters p.22 — skewed low-res scan, phone photo, rotated page,
hand-written form. The ingest's text-origin detection must call every one of them
'image-only' (a scan nobody OCR'd), the README must describe each, and the three
font-free fixtures must regenerate byte-identical. No OCR runs here; the suite
stays fast and hermetic."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ingest import text_origin  # noqa: E402
from scripts import make_hard_fixtures as mk  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "hard_docs")
NAMES = ("skew_lowres", "phone_photo", "rotated90", "form_handwritten")


def _page_image(path: str) -> np.ndarray:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    try:
        pg = pdf[0]
        imgs = [o for o in pg.get_objects() if o.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE]
        assert len(imgs) == 1
        return np.asarray(imgs[0].get_bitmap().to_pil())
    finally:
        pdf.close()


def test_fixtures_are_committed_and_named_in_the_readme():
    with open(os.path.join(FIX, "README.md")) as f:
        readme = f.read()
    for name in NAMES:
        assert os.path.exists(os.path.join(FIX, f"{name}.pdf")), name
        assert f"`{name}.pdf`" in readme, name
    for tier_word in ("image-only", "Expected extraction tier", "amber"):
        assert tier_word in readme


@pytest.mark.parametrize("name", NAMES)
def test_every_fixture_is_image_only(name):
    """core.ingest's text_origin reads the page objects: one image, zero text objects."""
    o = text_origin(os.path.join(FIX, f"{name}.pdf"))
    assert o == {"doc": "image-only", "pages": {"1": "image-only"}, "producer": ""}


@pytest.mark.parametrize("name", NAMES)
def test_every_fixture_is_one_page_one_image_of_the_expected_shape(name):
    arr = _page_image(os.path.join(FIX, f"{name}.pdf"))
    h, w = arr.shape[:2]
    expect = {"skew_lowres": (409, 528), "phone_photo": (1227, 1584),
              "rotated90": (1584, 1227), "form_handwritten": (969, 699)}[name]
    assert (w, h) == expect, (name, w, h)
    if name == "rotated90":
        assert w > h          # landscape: the page was turned
    if name == "skew_lowres":
        assert w * 3 < 1240   # a third of the 2x render


def test_regeneration_is_byte_identical_for_the_font_free_fixtures(tmp_path):
    made = mk.build(str(tmp_path), verbose=False)
    for name in NAMES:
        if name in mk.FONT_FIXTURES:
            continue
        with open(os.path.join(FIX, f"{name}.pdf"), "rb") as f:
            committed = f.read()
        with open(made[name]["pdf"], "rb") as f:
            fresh = f.read()
        assert fresh == committed, f"{name}.pdf regenerated differently"
    # the form depends on the machine's fonts: same geometry is all we promise
    assert _page_image(made["form_handwritten"]["pdf"]).shape == \
        _page_image(os.path.join(FIX, "form_handwritten.pdf")).shape
    assert "font" in made["form_handwritten"]


def test_degradations_differ_from_the_clean_page_and_from_each_other():
    """The fixtures are not four copies of the same render."""
    clean = np.asarray(mk.render_page())
    imgs = {n: _page_image(os.path.join(FIX, f"{n}.pdf")) for n in NAMES}
    assert imgs["phone_photo"].shape == clean.shape
    assert np.abs(imgs["phone_photo"].astype(int) - clean.astype(int)).mean() > 20  # shadow + warp
    assert imgs["rotated90"].shape[:2] == clean.shape[1::-1]
    assert imgs["skew_lowres"].shape != clean.shape
    assert imgs["form_handwritten"].shape != clean.shape
