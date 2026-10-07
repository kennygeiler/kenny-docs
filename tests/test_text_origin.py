"""Text origin (DEMO_TICKETS.md D1): where a PDF's text actually came from, decided
from the page objects — a scan with an invisible OCR layer must not be labelled a
digital text layer. Plus the back-fill script's two promises on the shipped catalog:
idempotent, and every other key byte-identical."""
import io
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ingest import text_origin  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE = os.path.join(ROOT, "cases", "santacruz")
FIRE_PDF = os.path.join(CASE, "sources", "firefighters_local3535_mou.pdf")
SALARY_PDF = os.path.join(CASE, "sources", "master_salary_schedule.pdf")


def _digital_pdf(path: str) -> None:
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path)
    c.drawString(72, 720, "Overtime shall be paid at one and one half times the regular rate.")
    c.showPage()
    c.save()


def _image_only_pdf(path: str) -> None:
    from PIL import Image
    img = Image.new("RGB", (612, 792), "white")
    img.save(path, "PDF")


def test_digital_pdf_is_digital(tmp_path):
    p = str(tmp_path / "digital.pdf")
    _digital_pdf(p)
    o = text_origin(p)
    assert o["doc"] == "digital" and o["pages"] == {"1": "digital"}
    assert o["producer"] == ""


def test_shipped_salary_schedule_is_digital():
    o = text_origin(SALARY_PDF)
    assert o["doc"] == "digital" and o["producer"] == ""


def test_image_only_pdf(tmp_path):
    p = str(tmp_path / "scan.pdf")
    _image_only_pdf(p)
    o = text_origin(p)
    assert o["doc"] == "image-only" and o["pages"] == {"1": "image-only"}


def test_firefighters_p22_is_an_ocr_layer():
    o = text_origin(FIRE_PDF)
    assert o["pages"]["22"] == "ocr-layer"
    assert o["doc"] == "ocr-layer"
    assert "OCRmyPDF" in o["producer"] and "Tesseract" in o["producer"]
    # the salary appendix at the back of the same file is born-digital — per-page
    # origin is what lets a citation of p.36 keep its honest "text layer" chip
    assert o["pages"]["36"] == "digital"


def test_missing_file_raises(tmp_path):
    with pytest.raises(Exception):
        text_origin(str(tmp_path / "nope.pdf"))


# --------------------------------------------------------------------------- #
# back-fill script
# --------------------------------------------------------------------------- #
ORIGIN_KEYS = ("text_origin", "text_origin_pages", "producer")


def _strip_origin(catalog_path: str) -> None:
    """Rewrite the catalog as it was before the back-fill (same indent=1 layout)."""
    with open(catalog_path) as f:
        data = json.load(f)
    for d in data["documents"]:
        for k in ORIGIN_KEYS:
            d.pop(k, None)
    with open(catalog_path, "w") as f:
        json.dump(data, f, indent=1)


def _is_subsequence(old_lines, new_lines) -> bool:
    """Every old line survives in order. Appending keys to an entry gives the entry's
    former last line a trailing comma — JSON syntax, not a value change — so the
    comparison ignores a trailing comma."""
    i = 0
    strip = lambda s: s[:-1] if s.endswith(",") else s  # noqa: E731
    for line in new_lines:
        if i < len(old_lines) and strip(line) == strip(old_lines[i]):
            i += 1
    return i == len(old_lines)


def test_backfill_is_idempotent_and_leaves_other_keys_byte_identical(tmp_path):
    from scripts.backfill_text_origin import backfill
    case = tmp_path / "santacruz"
    case.mkdir()
    shutil.copy(os.path.join(CASE, "catalog.json"), case / "catalog.json")
    shutil.copytree(os.path.join(CASE, "sources"), case / "sources")
    cat_path = str(case / "catalog.json")
    _strip_origin(cat_path)
    before = open(cat_path, "rb").read()
    assert b'"text_origin"' not in before

    report = backfill(str(case), verbose=False)
    after = open(cat_path, "rb").read()
    assert {r["doc_id"]: r["text_origin"] for r in report} == {
        "firefighters_local3535_mou": "ocr-layer", "admin_group_mou": "ocr-layer",
        "management_mou": "ocr-layer", "chief_officers_mou": "ocr-layer",
        "master_salary_schedule": "digital"}
    assert all(r["changed"] for r in report)
    # every byte of the old file survives, in order: the back-fill only INSERTS lines
    old_lines = before.decode().splitlines()
    new_lines = after.decode().splitlines()
    assert _is_subsequence(old_lines, new_lines)
    # and the only keys that differ are the three origin keys, appended last
    old_docs = {d["doc_id"]: d for d in json.loads(before)["documents"]}
    for d in json.loads(after)["documents"]:
        extra = [k for k in d if k not in old_docs[d["doc_id"]]]
        assert extra == list(ORIGIN_KEYS)
        assert {k: v for k, v in d.items() if k not in ORIGIN_KEYS} == old_docs[d["doc_id"]]
        assert list(d)[:-3] == list(old_docs[d["doc_id"]])      # same key order
    # the shipped catalog IS this script's output, byte for byte
    assert after == open(os.path.join(CASE, "catalog.json"), "rb").read()

    report2 = backfill(str(case), verbose=False)
    assert not any(r["changed"] for r in report2)
    assert open(cat_path, "rb").read() == after
