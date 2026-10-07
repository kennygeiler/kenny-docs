#!/usr/bin/env python
"""Generate the hard-document fixtures (DEMO_TICKETS.md D6) — locally, from a page the
corpus already holds, with no downloads.

Four image-only PDFs under tests/fixtures/hard_docs/ (add --png to also write the
image each was made from), all derived from Firefighters Local 3535 MOU p.22 (the
vacation accrual table) rendered at 2x through pypdfium2:

  skew_lowres.pdf       2.5 degree skew, one-third resolution, slight blur — a fax-grade
                        scan. Expect: image-only, garbage OCR, amber page.
  phone_photo.pdf       perspective warp, left-to-right shadow, seeded sensor noise — a
                        page photographed at a desk. Expect: image-only, prose readable,
                        the table's small decimals at risk under the shadow.
  rotated90.pdf         the page turned 90 degrees — a landscape scan nobody rotated.
                        Expect: image-only, garbage until an orientation pre-pass (D8).
  form_handwritten.pdf  a form-like crop of the table with a hand-written number drawn
                        into a box (a handwriting font when macOS has one, else bold
                        sans). Expect: image-only, printed labels extract, the written
                        value is missing or misread, so the cell gate refuses it.

Everything is deterministic: fixed warp corners, a seeded noise generator, fixed
geometry, pinned PDF dates. The three non-font fixtures regenerate byte-identical
(tests/test_hard_fixtures.py checks this), so the committed files never depend on
the machine's fonts. Only PIL + numpy + pypdfium2 are used (no OpenCV).

    python scripts/make_hard_fixtures.py            # writes tests/fixtures/hard_docs/
    python scripts/make_hard_fixtures.py --out DIR  # elsewhere (the test does this)
    python scripts/make_hard_fixtures.py --png      # also the PNGs, to look at
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SOURCE_PDF = os.path.join(ROOT, "cases", "santacruz", "sources", "firefighters_local3535_mou.pdf")
SOURCE_PAGE = 22
OUT_DIR = os.path.join(ROOT, "tests", "fixtures", "hard_docs")
RENDER_SCALE = 2.0
PDF_DPI = 144                      # 2x of 72 pt: the page keeps its letter size
SEED = 20261007
FIXTURES = ("skew_lowres", "phone_photo", "rotated90", "form_handwritten")
FONT_FIXTURES = ("form_handwritten",)   # depends on the machine's fonts
HAND_FONTS = ("/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
              "/System/Library/Fonts/Supplemental/Chalkboard.ttc",
              "/System/Library/Fonts/Supplemental/Noteworthy.ttc")
PLAIN_FONTS = ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
               "/System/Library/Fonts/Supplemental/Arial.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def render_page(pdf_path: str = SOURCE_PDF, page: int = SOURCE_PAGE,
                scale: float = RENDER_SCALE) -> Image.Image:
    """The source page as an RGB image, rendered under the app's pdfium lock."""
    import pypdfium2 as pdfium
    from core import pdfview
    with pdfview._RENDER_LOCK:
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            img = pdf[page - 1].render(scale=scale).to_pil().convert("RGB")
        finally:
            pdf.close()
    return img


# --------------------------------------------------------------------------- #
# the four degradations
# --------------------------------------------------------------------------- #
def skew_lowres(page: Image.Image) -> Image.Image:
    """2.5 degrees of skew, downsampled to a third, lightly blurred."""
    skewed = page.rotate(2.5, resample=Image.BICUBIC, expand=False, fillcolor="white")
    w, h = skewed.size
    small = skewed.resize((w // 3, h // 3), Image.BILINEAR)
    return small.filter(ImageFilter.GaussianBlur(0.6)).convert("L").convert("RGB")


def _perspective_coeffs(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> list[float]:
    """PIL's 8 perspective coefficients mapping OUTPUT points (dst) to INPUT points
    (src) — the standard least-squares solve."""
    matrix = []
    for (x, y), (u, v) in zip(dst, src):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    a = np.array(matrix, dtype=np.float64)
    b = np.array([c for pt in src for c in pt], dtype=np.float64)
    res = np.linalg.solve(a, b)
    return [float(v) for v in res]


def phone_photo(page: Image.Image) -> Image.Image:
    """Perspective (the far edge narrower), a shadow across the page, sensor noise."""
    w, h = page.size
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    dst = [(int(w * 0.08), int(h * 0.05)), (int(w * 0.90), int(h * 0.02)),
           (int(w * 0.97), int(h * 0.96)), (int(w * 0.03), int(h * 0.99))]
    coeffs = _perspective_coeffs(src, dst)
    bg = Image.new("RGB", (w, h), (96, 88, 80))        # a desk under the sheet
    warped = page.transform((w, h), Image.PERSPECTIVE, coeffs, resample=Image.BICUBIC,
                            fillcolor=(96, 88, 80))
    mask = Image.new("L", (w, h), 255).transform((w, h), Image.PERSPECTIVE, coeffs,
                                                 resample=Image.BICUBIC, fillcolor=0)
    bg.paste(warped, (0, 0), mask)
    arr = np.asarray(bg).astype(np.float32)
    # shadow: bright on the left, dark on the right, with a soft vertical falloff
    xs = np.linspace(1.0, 0.42, w, dtype=np.float32)[None, :, None]
    ys = np.linspace(0.95, 1.0, h, dtype=np.float32)[:, None, None]
    arr = arr * xs * ys
    rng = np.random.default_rng(SEED)
    arr = arr + rng.normal(0.0, 9.0, size=arr.shape).astype(np.float32)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB").filter(ImageFilter.GaussianBlur(0.4))


def rotated90(page: Image.Image) -> Image.Image:
    """The page scanned sideways."""
    return page.rotate(90, expand=True, fillcolor="white")


def _font(paths: tuple[str, ...], size: int) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, str]:
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size), os.path.basename(p)
            except OSError:
                continue
    return ImageFont.load_default(), "PIL default"


def form_handwritten(page: Image.Image) -> tuple[Image.Image, str]:
    """A form-like crop around the accrual table with printed labels and a hand-written
    '8' (hours) and '1.5' (rate multiplier) in boxes. Returns (image, font name)."""
    w, h = page.size
    # the table box on p.22 in PDF points (catalog bbox, bottom-left origin) -> pixels
    l, t, r, b = 109.8, 589.5, 534.3, 449.8
    s = RENDER_SCALE
    ph = h / s
    crop = page.crop((int((l - 30) * s), int((ph - t - 40) * s),
                      int((r + 30) * s), int((ph - b + 40) * s)))
    cw, ch = crop.size
    form = Image.new("RGB", (cw, ch + 260), "white")
    form.paste(crop, (0, 0))
    draw = ImageDraw.Draw(form)
    label, label_name = _font(PLAIN_FONTS, 26)
    hand, hand_name = _font(HAND_FONTS, 64)
    y = ch + 20
    draw.text((30, y), "Hours claimed this pay period:", fill="black", font=label)
    draw.rectangle([520, y - 12, 640, y + 70], outline="black", width=3)
    draw.text((548, y - 6), "8", fill=(20, 30, 120), font=hand)
    y2 = y + 110
    draw.text((30, y2), "Rate multiplier:", fill="black", font=label)
    draw.rectangle([520, y2 - 12, 700, y2 + 70], outline="black", width=3)
    draw.text((540, y2 - 6), "1.5", fill=(20, 30, 120), font=hand)
    draw.text((30, y2 + 90), "Employee initials: ______    Supervisor: ______",
              fill="black", font=label)
    draw.line([(0, ch + 4), (cw, ch + 4)], fill="black", width=2)
    return form, f"{hand_name} (labels: {label_name})"


# --------------------------------------------------------------------------- #
def build(out_dir: str = OUT_DIR, pdf_path: str = SOURCE_PDF, verbose: bool = True,
          png: bool = False) -> dict:
    """Write every fixture as <name>.pdf (image-only; one page, one JPEG image
    XObject, no text objects) into out_dir, plus <name>.png when `png` is set (the
    phone photo's PNG is ~4 MB of noise, so PNGs are for looking, not for git).
    The PDF's creation/modification dates are pinned so a regeneration is
    byte-identical. Returns {name: {pdf, png?, size, bytes, font?}}."""
    import time
    stamp = time.strptime("20261007000000", "%Y%m%d%H%M%S")   # PIL wants a struct_time
    os.makedirs(out_dir, exist_ok=True)
    page = render_page(pdf_path)
    made: dict[str, dict] = {}
    images: dict[str, Image.Image] = {
        "skew_lowres": skew_lowres(page),
        "phone_photo": phone_photo(page),
        "rotated90": rotated90(page),
    }
    form, font_name = form_handwritten(page)
    images["form_handwritten"] = form
    for name in FIXTURES:
        img = images[name]
        pdf = os.path.join(out_dir, f"{name}.pdf")
        # An image-only PDF: one page, one image XObject, no text objects at all.
        img.save(pdf, "PDF", resolution=PDF_DPI, creationDate=stamp, modDate=stamp)
        made[name] = {"pdf": pdf, "size": img.size, "bytes": os.path.getsize(pdf)}
        if png:
            made[name]["png"] = os.path.join(out_dir, f"{name}.png")
            img.save(made[name]["png"], "PNG", optimize=True)
        if name in FONT_FIXTURES:
            made[name]["font"] = font_name
        if verbose:
            print(f"{name}: {img.size[0]}x{img.size[1]} -> {os.path.getsize(pdf):,} B pdf"
                  + (f" (font: {font_name})" if name in FONT_FIXTURES else ""))
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--pdf", default=SOURCE_PDF)
    ap.add_argument("--png", action="store_true", help="also write <name>.png to look at")
    args = ap.parse_args(argv)
    if not os.path.exists(args.pdf):
        print(f"source PDF not found: {args.pdf}", file=sys.stderr)
        return 1
    build(args.out, args.pdf, png=args.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
