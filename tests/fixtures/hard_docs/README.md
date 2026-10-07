# Hard documents (DEMO_TICKETS.md D6)

Four image-only PDFs that exercise what the shipped corpus does not: skew, low
resolution, perspective and shadow, rotation, handwriting. All four are generated
**locally** from a page the corpus already holds — Firefighters Local 3535 MOU p.22,
the vacation accrual table whose OCR text layer misreads 10.15 as `0) £5` — by
`scripts/make_hard_fixtures.py` (pypdfium2 render at 2x, PIL + numpy degradations, no
OpenCV, no downloads). Regenerate with:

    env -u ANTHROPIC_API_KEY HF_HUB_OFFLINE=1 .venv/bin/python scripts/make_hard_fixtures.py

Every fixture is deterministic (fixed warp corners, seeded noise, fixed geometry);
`tests/test_hard_fixtures.py` regenerates the three font-free fixtures into a temp
directory and asserts the embedded page image is pixel-identical to the committed
PDF. `form_handwritten` uses a handwriting font when macOS has one (Bradley Hand
Bold) and falls back to a bold sans, so its committed bytes are the record, not a
regeneration target.

Each PDF holds one page and one image XObject and **no text objects at all**, so
`core.ingest.text_origin` reports `image-only` for every page: nothing in these
files is a text layer, everything a parser returns had to be read off the picture.

| Fixture | What it demonstrates | Expected extraction tier | What the parser should say |
|---|---|---|---|
| `skew_lowres.pdf` | A fax-grade scan: 2.5° skew, one-third resolution (409×528 px for a letter page), slight blur. | `image-only` → OCR'd scan, **low confidence** (amber page). | Garbage or near-garbage text; every numeric cell of the table unparsable, so the cell checker (D2) flags them and no rule can cite the table. |
| `phone_photo.pdf` | A page photographed at a desk: perspective warp (far edge narrower), a left-to-right shadow, seeded sensor noise, a dark desk around the sheet. | `image-only` → OCR'd scan, **borderline confidence**. | The prose paragraphs read; the table's small decimals (10.15, 13.85) sit under the shadow and are the cells most likely to come back wrong or empty — exactly the cells D2's second-engine re-read must dispute rather than trust. |
| `rotated90.pdf` | A landscape scan nobody rotated: the whole page turned 90°. | `image-only` → OCR'd scan, **low confidence** until an orientation pre-pass (D8) exists. | Garbage text; the honest outcome tonight is an amber page, not a confident wrong answer. |
| `form_handwritten.pdf` | A form-like crop of the accrual table with printed labels ("Hours claimed this pay period:", "Rate multiplier:") and hand-written values `8` and `1.5` drawn into boxes in a handwriting font. | `image-only` → OCR'd scan; printed labels at normal confidence. | Printed labels extract; the hand-written hours value is missing or misread (`g` for `8`, `[5` for `1.5` in the engine we measured), so the numeric-cell gate refuses it as evidence until a person confirms it (`POST /admin/cell_confirm`). |

"Expected extraction tier" is the `text_origin` the ingest records (D1): these are
all `image-only`, the tier that means "a scan nobody OCR'd" and sends the document
through docling's OCR with per-page confidence (D5). The confidence and cell
outcomes in the right-hand column were measured by hand while the ticket was
written (Tesseract 5.5 and RapidOCR-torch over these exact images) and are the
demo expectations, not something `test_hard_fixtures.py` asserts — that test runs
no OCR, so the suite stays fast and hermetic.

Demo beat: drop `rotated90.pdf` and `form_handwritten.pdf` into Admin → Upload. One
comes back as garbage with an amber page; the other keeps its printed labels, loses
the hand-written 8, and the rule gate refuses the cell.
