"""Documents are called by their declared names everywhere (DEMO_TICKETS.md C4).

Four of the five shipped documents were catalogued under their cover's term line
("December 31, 2028"). The heuristic now rejects dates, the display name is the
declared case.yaml title, and the back-fill retitles the shipped catalog in place."""
import json
import os
import shutil
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import ingest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE = os.path.join(ROOT, "cases", "santacruz")


def _declared() -> dict[str, str]:
    with open(os.path.join(CASE, "case.yaml")) as f:
        return {s["id"]: s["title"] for s in yaml.safe_load(f)["sources"]}


def _catalog() -> list[dict]:
    with open(os.path.join(CASE, "catalog.json")) as f:
        return json.load(f)["documents"]


@pytest.mark.parametrize("text", [
    "December 31, 2028", "DECEMBER 31, 2028", "January 1, 2024 Through December 31, 2026",
    "January 1, 2024 Through", "2026", "12/31/2028", "2028-12-31", "Effective July 1, 2025",
    "untitled", "3535", "",
])
def test_date_like_lines_are_not_names(text):
    assert ingest._date_like(text) or text == ""
    assert ingest._clean_title(text) == ""


@pytest.mark.parametrize("text, cleaned", [
    ("2026 Police Salary Schedule", "2026 Police Salary Schedule"),
    ("Fire Salary Schedule FY26", "Fire Salary Schedule FY26"),
    ("Local 3535", "Local 3535"),
    ("Central Firefighters Local 3535 December 20, 2025 Through",
     "Central Firefighters Local 3535"),
    ("Admin Group MOU — Effective July 1, 2025", "Admin Group MOU"),
])
def test_names_survive_cleaning_and_lose_their_date_tail(text, cleaned):
    assert not ingest._date_like(text)
    assert ingest._clean_title(text) == cleaned


def test_extract_title_never_returns_a_date():
    for entry in _catalog():
        pdf = os.path.join(CASE, "sources", entry["doc_id"] + ".pdf")
        title = ingest.extract_title(entry["clauses"], pdf, fallback="FALLBACK")
        assert not ingest._date_like(title), (entry["doc_id"], title)
        assert title != "FALLBACK"
        if entry["doc_id"].endswith("_mou"):
            assert "central fire" in title.lower(), (entry["doc_id"], title)


def test_metadata_untitled_is_rejected(tmp_path):
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    path = str(tmp_path / "untitled.pdf")
    c = canvas.Canvas(path)
    c.setTitle("untitled")
    c.drawString(72, 720, "nothing on the cover names this")
    c.showPage()
    c.save()
    assert ingest.extract_title([], path, fallback="X") == "X"
    # ...and a cover made only of a date line still falls through to the caller's name
    assert ingest.extract_title(
        [{"label": "text", "text": "December 31, 2028", "page": 1, "clause": ""}],
        path, fallback="X") == "X"


def test_shipped_catalog_titles_are_names():
    for entry in _catalog():
        assert not ingest._date_like(entry["title"]), (entry["doc_id"], entry["title"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app), str(case)


def test_coverage_titles_are_declared(client):
    c, case_dir = client
    declared = _declared()
    docs = {d["doc_id"]: d for d in c.get("/admin/coverage").json()["documents"]}
    for did, title in declared.items():
        assert docs[did]["title"] == title
        assert not ingest._date_like(docs[did]["extracted_title"])
        assert docs[did]["extracted_title"] != ""
    # An uploaded document has no declaration: its own (non-date) heading is its name.
    cat_path = os.path.join(case_dir, "catalog.json")
    with open(cat_path) as f:
        data = json.load(f)
    data["documents"].append({"doc_id": "uploaded_policy", "title": "Tuition Reimbursement Policy",
                              "declared_title": "", "file": "sources/none.pdf",
                              "clauses": [], "page_count": 1, "parse_source": "docling"})
    with open(cat_path, "w") as f:
        json.dump(data, f)
    docs = {d["doc_id"]: d for d in c.get("/admin/coverage").json()["documents"]}
    assert docs["uploaded_policy"]["title"] == "Tuition Reimbursement Policy"
    # and one whose cover gave only a date is called by its id, never by the date
    data["documents"][-1]["title"] = "December 31, 2028"
    with open(cat_path, "w") as f:
        json.dump(data, f)
    docs = {d["doc_id"]: d for d in c.get("/admin/coverage").json()["documents"]}
    assert docs["uploaded_policy"]["title"] == "uploaded_policy"
    assert docs["uploaded_policy"]["extracted_title"] == ""


def test_case_sources_and_chat_chips_use_declared_titles(client):
    c, _ = client
    declared = _declared()
    for s in c.get("/api/case").json()["sources"]:
        assert s["title"] == declared[s["doc_id"]]
    res = c.post("/chat", json={"prompt": "What does the Firefighters Local 3535 MOU "
                                          "say about bereavement leave?"}).json()
    titles = {s["doc_id"]: s["title"] for s in res.get("sources", [])}
    assert titles, res
    for did, title in titles.items():
        assert title == declared[did]
        assert not ingest._date_like(title)
    for o in res.get("options", []):
        assert "score" not in str(o)


# --------------------------------------------------------------------------- #
# back-fill script
# --------------------------------------------------------------------------- #
def _retitle_to_dates(catalog_path: str) -> dict[str, str]:
    """Put the pre-C4 date titles back (same indent=1 layout)."""
    with open(catalog_path) as f:
        data = json.load(f)
    old = {}
    dates = {"firefighters_local3535_mou": "December 31, 2028",
             "admin_group_mou": "January 1, 2024 Through December 31, 2026",
             "management_mou": "December 31, 2026",
             "chief_officers_mou": "DECEMBER 31, 2028"}
    for d in data["documents"]:
        if d["doc_id"] in dates:
            old[d["doc_id"]] = d["title"]
            d["title"] = dates[d["doc_id"]]
    with open(catalog_path, "w") as f:
        json.dump(data, f, indent=1)
    return old


def test_backfill_is_idempotent_and_leaves_other_keys_byte_identical(tmp_path):
    from scripts.backfill_titles import backfill
    case = tmp_path / "santacruz"
    case.mkdir()
    shutil.copy(os.path.join(CASE, "catalog.json"), case / "catalog.json")
    shutil.copytree(os.path.join(CASE, "sources"), case / "sources")
    cat_path = str(case / "catalog.json")
    shipped = _retitle_to_dates(cat_path)
    before = open(cat_path, "rb").read()
    assert b'"title": "December 31, 2028"' in before

    report = backfill(str(case), verbose=False)
    after = open(cat_path, "rb").read()
    changed = {r["doc_id"]: r for r in report if r["changed"]}
    assert set(changed) == set(shipped)
    for did, rec in changed.items():
        assert rec["after"] == shipped[did]
    # only the four title lines differ; every other line is byte-identical, in order
    old_lines, new_lines = before.decode().splitlines(), after.decode().splitlines()
    assert len(old_lines) == len(new_lines)
    diff = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
    assert len(diff) == 4 and all('"title":' in a for a, _ in diff)
    old_docs = {d["doc_id"]: d for d in json.loads(before)["documents"]}
    for d in json.loads(after)["documents"]:
        assert list(d) == list(old_docs[d["doc_id"]])     # same keys, same order
        assert {k: v for k, v in d.items() if k != "title"} == \
            {k: v for k, v in old_docs[d["doc_id"]].items() if k != "title"}
    # the shipped catalog IS this script's output, byte for byte
    assert after == open(os.path.join(CASE, "catalog.json"), "rb").read()

    report2 = backfill(str(case), verbose=False)
    assert not any(r["changed"] for r in report2)
    assert open(cat_path, "rb").read() == after
