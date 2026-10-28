"""M1.4 / I14: cases/santacruz/data/salary_schedule.csv is the Master Salary Schedule, row
for row, and every roster rate is bound to one of its rows.

Re-reads the born-digital schedule PDF with pypdfium2 (no catalog, no model) and asserts:
every CSV row's quote is the text inside its row box, its hourly figure is the text inside
its cell box, its monthly figure and date are on that line, the CSV covers every printed
title (2 steps each), and the extractor still produces the CSV byte for byte.
"""
import csv
import os
import re
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

CASE = os.path.join(ROOT, "cases", "santacruz")
PDF = os.path.join(CASE, "sources", "master_salary_schedule.pdf")
CSV = os.path.join(CASE, "data", "salary_schedule.csv")
ROSTER = os.path.join(CASE, "data", "roster.csv")

pdfium = pytest.importorskip("pypdfium2")


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _box(s):
    return [float(v) for v in s.split(",")]


@pytest.fixture(scope="module")
def page():
    pdf = pdfium.PdfDocument(PDF)
    yield pdf[0].get_textpage()
    pdf.close()


def _text(tp, box):
    l, t, r, b = box
    return re.sub(r"\s+", " ", tp.get_text_bounded(left=l, bottom=b, right=r, top=t)).strip()


SCHEDULE = _rows(CSV)


def test_csv_covers_every_printed_title_twice(page):
    full = page.get_text_range()
    lines = [ln.strip() for ln in full.splitlines()]
    printed = [ln for ln in lines if re.search(r"\b(January|February|March|April|May|June|July|"
                                               r"August|September|October|November|December) \d", ln)
               and "$" in ln]
    assert len(printed) == 27
    titles = [r["title"] for r in SCHEDULE]
    assert len(SCHEDULE) == 54 and len(set(titles)) == 27
    for ln in printed:
        assert sum(1 for r in SCHEDULE if r["quote"] == re.sub(r"\s+", " ", ln)) == 2, ln
    assert len({r["schedule_ref"] for r in SCHEDULE}) == 54


@pytest.mark.parametrize("row", SCHEDULE, ids=lambda r: r["schedule_ref"])
def test_every_csv_value_is_on_the_pdf(page, row):
    assert row["doc_id"] == "master_salary_schedule" and row["page"] == "1"
    # the row box holds exactly the quoted line, and the line starts with the title
    assert _text(page, _box(row["row_bbox"])) == row["quote"]
    assert row["quote"].startswith(row["title"] + " ")
    # the hourly cell box holds exactly this step's hourly figure
    assert _text(page, _box(row["hourly_bbox"])) == f"${float(row['hourly']):,.2f}"
    l, t, r, b = _box(row["hourly_bbox"])
    rl, rt, rr, rb = _box(row["row_bbox"])
    assert rl <= l and r <= rr and rb <= b and t <= rt
    # MINIMUM is the left-hand hourly column, MAXIMUM the right-hand one
    assert (r < 450) == (row["step"] == "minimum")
    # the line's money figures in column order: min/max hourly, min/max monthly
    money = [float(m.replace(",", "")) for m in re.findall(r"\$([\d,]+\.\d{2})", row["quote"])]
    i = 0 if row["step"] == "minimum" else 1
    assert money[i] == float(row["hourly"])
    if row["monthly"]:
        assert money[2 + i] == float(row["monthly"])
    else:
        assert len(money) == 2           # the row prints '-' or nothing for monthly
    printed_date = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", row["quote"]).group(1)
    assert datetime.strptime(printed_date, "%B %d, %Y").date().isoformat() == row["effective_date"]
    m = re.search(r"-\s*(\d{2}) hr", row["title"])
    assert row["basis"] == (f"{m.group(1)}-hr" if m else "unstated")


def test_extractor_reproduces_the_csv():
    import extract_salary_schedule as ex
    rows = ex.extract()
    assert ex.catalog_disagreements(rows) == []
    with open(CSV) as f:
        assert ex.render(rows) == f.read()


# --------------------------------------------------------------------------- #
# Roster <-> schedule binding (data/schedule_refs.csv)
# --------------------------------------------------------------------------- #
STAGED = os.path.join(CASE, "data", "roster_staged.csv")
REFS = os.path.join(CASE, "data", "schedule_refs.csv")


def _bindings():
    return {r["classification"]: r["schedule_ref"] for r in _rows(REFS)}


@pytest.mark.parametrize("path", [ROSTER, STAGED], ids=["roster", "staged"])
def test_every_roster_row_binds_to_a_schedule_row_with_its_rate(path):
    """A roster base_hourly that drifts from its schedule row fails here, as does a row
    bound to the wrong step (entry = MINIMUM, top = MAXIMUM) or the wrong hours."""
    by_ref = {r["schedule_ref"]: r for r in SCHEDULE}
    refs = _bindings()
    bad = []
    for row in _rows(path):
        ref = refs.get(row["classification"], "")
        s = by_ref.get(ref)
        if s is None:
            bad.append((row["classification"], f"schedule_ref {ref!r} not in salary_schedule.csv"))
            continue
        if f"{float(row['base_hourly']):.2f}" != s["hourly"]:
            bad.append((row["classification"], f"roster {row['base_hourly']} != schedule {s['hourly']}"))
        name = row["classification"].lower()
        want_step = "minimum" if "entry" in name else "maximum"
        if s["step"] != want_step:
            bad.append((row["classification"], f"named {want_step} step, bound to {s['step']}"))
        hr = re.search(r"\b(\d{2}) hr\b", name)
        if hr and s["basis"] != f"{hr.group(1)}-hr":
            bad.append((row["classification"], f"named {hr.group(1)} hr, bound to {s['basis']}"))
        if not s["title"].replace("- ", " ").lower().startswith(row["rank"].lower()):
            bad.append((row["classification"], f"rank {row['rank']!r} bound to {s['title']!r}"))
    assert bad == [], bad


def test_bindings_name_only_known_rows():
    names = {r["classification"] for r in _rows(ROSTER) + _rows(STAGED)}
    assert set(_bindings()) == names
    assert len(_rows(REFS)) == len(names)


def test_staged_rows_are_shaped_like_the_roster_and_not_live():
    with open(ROSTER) as f, open(STAGED) as g:
        assert f.readline() == g.readline()
    live = {r["classification"] for r in _rows(ROSTER)}
    assert live.isdisjoint(r["classification"] for r in _rows(STAGED))


def test_local_3535_classifications_have_entry_and_top_rows_on_both_schedules():
    refs = _bindings()
    rows = [r for r in _rows(ROSTER) + _rows(STAGED) if r["bargaining_unit"] == "firefighters-local-3535"]
    bound = {refs[r["classification"]] for r in rows}
    for title in ("firefighter", "firefighter-paramedic", "fire-captain", "battalion-chief"):
        for basis in ("56-hr", "40-hr"):
            for step in ("min", "max"):
                assert f"{title}-{basis}:{step}" in bound, (title, basis, step)


def test_case_loads_the_binding_and_the_rate_cites_its_row():
    from core import mathline
    from core.caseio import load_case
    case = load_case(CASE)
    b = case.rate_bindings()
    assert set(b) == {r["classification"] for r in _rows(ROSTER) + _rows(STAGED)}
    for s in case.subjects():
        src = mathline.rate_source(case, None, s, float(s["base_hourly"]))
        assert not src.get("unsourced"), (s["name"], src.get("reason"))
        cit = src["citation"]
        row = b[s["name"]]
        assert cit == {"doc_id": "master_salary_schedule", "page": 1, "bbox": row["row_bbox"],
                       "quote": row["quote"], "cell_bbox": row["hourly_bbox"],
                       "clause": f"{row['title']} · {row['step'].upper()} HOURLY RATE"}
    # drift is reported, not hidden
    ff = next(s for s in case.subjects() if s["name"] == "Firefighter/Paramedic (56 hr, top step)")
    bad = mathline.rate_source(case, None, ff, 53.41)
    assert bad["unsourced"] and "roster holds 53.41" in bad["reason"]
