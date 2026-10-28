# Sources

The five PDFs in [`cases/santacruz/sources/`](cases/santacruz/sources/) are public records
published by the **Central Fire District of Santa Cruz County** on its Salaries & Benefits
page (centralfiresc.org/2161/Salaries-Benefits). They are redistributed here for
demonstration only. This repo makes no claim to them.

The four MOUs are the district's scans with an OCR text layer added by the repo author (OCRmyPDF 17.8.0
with Tesseract 5.5.2, 17 July 2026). They are not the district's original bytes. The salary
schedule is born-digital (exported from Excel) and is the district's file as published.

| File | Title | Pages | Text | SHA-256 |
|---|---|---|---|---|
| `firefighters_local3535_mou.pdf` | Firefighters Local 3535 MOU 2025–2028 (with Salary Schedule) | 40 | scan + OCR layer | `b30076962d29c424864fbe8b05a048911356f7b807b3472f83104aeb921ee800` |
| `admin_group_mou.pdf` | Admin Group MOU 2024–2026 (with three Side Letters) | 30 | scan + OCR layer | `f35b0cf0d46013d73ba34a6e79087d38b51b42555ccbdcb91faf242eb3e0cc3e` |
| `management_mou.pdf` | Management MOU 2024–2026 (with Side Letter) | 18 | scan + OCR layer | `63213c90ef3333a2e717cfd894d03b49d5795f2400b322ae7ac2996764abcf6a` |
| `chief_officers_mou.pdf` | Chief Officers Association MOU 2026–2028 | 29 | scan + OCR layer | `20d85857b6cf4035e7c7f2eccb2c3991a82d2ebb10550c8b9d0a6c51f6bf5e09` |
| `master_salary_schedule.pdf` | Master Salary Schedule (approved 2026-05-14) | 1 | born-digital | `8123d794d45221c5a97dea1c0043faede605e7a8cc773361189b8b7b814e4f42` |

Titles are the ones declared in `cases/santacruz/case.yaml`. Hashes were computed on
2026-10-28 (`shasum -a 256 cases/santacruz/sources/*.pdf`) and match the `pdf_sha256` recorded
in `cases/santacruz/catalog.json`. The OCR tool versions and date are read from each PDF's own metadata.

Because the OCR layer is machine-read, some characters are wrong (for example "1,5" for
"1.5" on Local 3535 p.8). Citation chips say `OCR'd scan` for these four files, and six
numeric cells on Local 3535 p.22 that a second OCR engine read differently are shown in
amber (ARCHITECTURE §11.1).

## Data derived from the sources

`cases/santacruz/data/salary_schedule.csv` is every row of `master_salary_schedule.pdf`
(27 titles × MINIMUM/MAXIMUM hourly = 54 rows), read from the PDF's own text layer by
`scripts/extract_salary_schedule.py` (no OCR, no model) with page, row and cell boxes and
the verbatim line. The schedule prints no intermediate steps and no annual figure; its
MINIMUM/MAXIMUM equal the first and last CY2026 step (probationary or step I, and step III)
of the Local 3535 MOU's Appendix "A" (p.35). `data/schedule_refs.csv` binds each roster
classification to its row. `data/roster_staged.csv` holds nine more Local 3535 entry/top
rows (bound and drift-tested); they are not loaded yet because adding them makes a bare
"firefighter" match four rows. The schedule has no row for Fire Captain Specialist.

The repo itself has no licence yet; see STATUS.md.
