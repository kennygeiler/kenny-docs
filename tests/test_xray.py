"""X-ray clause endpoint (OCR_TICKETS.md OCR-1): page-scoped clause payload with the
page's point dimensions — without width/height the overlay cannot scale bboxes onto
the rendered image."""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cases", "santacruz")
    case = tmp_path / "santacruz"
    shutil.copytree(src, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app), str(case)


def _doc_ids(case_dir):
    with open(os.path.join(case_dir, "catalog.json")) as f:
        return [d["doc_id"] for d in json.load(f)["documents"]]


def test_clauses_are_page_scoped_with_page_metrics(client):
    c, case_dir = client
    doc_id = _doc_ids(case_dir)[0]
    res = c.get(f"/doc/{doc_id}/clauses", params={"page": 1})
    assert res.status_code == 200
    body = res.json()
    assert body["doc_id"] == doc_id
    assert body["page"] == 1
    assert body["page_count"] >= 1
    assert body["width"] > 0 and body["height"] > 0
    assert body["clauses"], "page 1 of an ingested contract must have clauses"
    for cl in body["clauses"]:
        assert cl["page"] == 1
        assert cl["kind"], "kind must be normalised, never absent or empty"
        assert len(cl["bbox"]) == 4
    # The catalog omits `kind` for normal text; the endpoint must default it.
    assert any(cl["kind"] == "text" for cl in body["clauses"])


def test_page_is_clamped_into_range(client):
    c, case_dir = client
    doc_id = _doc_ids(case_dir)[0]
    body = c.get(f"/doc/{doc_id}/clauses", params={"page": 9999}).json()
    assert body["page"] == body["page_count"]
    body = c.get(f"/doc/{doc_id}/clauses", params={"page": 0}).json()
    assert body["page"] == 1


def test_unknown_doc_is_404(client):
    c, _ = client
    assert c.get("/doc/no-such-doc/clauses").status_code == 404


def test_admin_page_ships_the_compare_view(client):
    """OCR-2 is frontend-only over the OCR-1 endpoint, so the meaningful server
    assertion is that the admin page actually ships the Compare surface: the entry
    point, the split-pane containers, and a bumped stylesheet version (stale cached
    CSS would render the split layout as a broken single column)."""
    c, _ = client
    html = c.get("/admin").text
    assert "openCompare(" in html, "every document card needs a Compare entry point"
    assert 'id="xrayText"' in html and 'class="xsplit"' in html
    css = c.get("/static/styles.css").text
    assert ".xsplit" in css and ".xtext" in css and ".xrow" in css


def test_admin_page_ships_the_table_xray(client):
    """OCR-5 is frontend-only over the OCR-1 endpoint: a majority-table page renders
    as a structured HTML table in the Compare pane. The meaningful server assertion
    is that the admin page ships the machinery — the majority-rule row parser, the
    table renderer, the count-strip mode indicator — and a bumped stylesheet version
    (stale cached CSS would render the grid unstyled and unhighlightable)."""
    c, _ = client
    html = c.get("/admin").text
    assert "function tableModel(" in html, "majority-table detection + row-text parsing"
    assert "renderXTable(" in html and 'class="xtable"' in html
    assert "table page —" in html, "count-strip mode indicator"
    assert "styles.css?v=12" in html
    css = c.get("/static/styles.css").text
    assert ".xtable" in css and ".xtable-wrap" in css


# --------------------------------------------------------------------------- #
# D4: Compare opens on a requested page; p.22 is a grid with flagged cells
# --------------------------------------------------------------------------- #
def test_compare_deep_link_opens_requested_page(client):
    c, _ = client
    html = c.get("/admin").text
    assert "openCompare = (docId, page" in html, "Compare takes a page argument"
    assert "function openViewer(mode, docId, page" in html
    assert "function compareDeepLink(" in html
    assert "compare=" in html and "p.get('p')" in html, "#documents?compare=<doc>&p=<n>"
    assert "/^compare=([^:&]+)(?::(\\d+))?$/" in html, "#compare=<doc>:<page>"
    assert "Showcase: p." in html, "one-click button on the Documents tab"
    assert "function tableModels(" in html, "every >=2-row table group gets a grid"
    assert "xcell-disputed" in html and "xcell-unverified" in html and "xreread" in html


def test_p22_renders_a_grid_with_flagged_cells(client):
    c, _ = client
    body = c.get("/doc/firefighters_local3535_mou/clauses", params={"page": 22}).json()
    rows = [cl for cl in body["clauses"] if cl["kind"] == "table-row"]
    assert len(rows) == 6
    assert len({tuple(r["bbox"]) for r in rows}) == 1, "one group -> one grid"
    flagged = [cl for cl in rows if cl["cell_status"] == "disputed"]
    assert len(flagged) == 4
    disputed = [x for cl in flagged for x in cl["cells"] if x["status"] == "disputed"]
    assert {(x["stored"], x["reread"]) for x in disputed} == {
        ("0) £5", "10.15"), ("fd", "11"), ("A468", "468"), ("fs 13,85", "13.85"),
        ("45", "15"), ("; 14,78", "14.78")}
    cov = c.get("/admin/coverage").json()
    assert cov["showcase"]["doc"] == "firefighters_local3535_mou"
    assert cov["showcase"]["page"] == 22 and "10.15" in cov["showcase"]["label"]
    fire = next(d for d in cov["documents"] if d["doc_id"] == "firefighters_local3535_mou")
    assert fire["text_origin"] == "ocr-layer" and "OCRmyPDF" in fire["producer"]
    assert fire["verified_pages"] == [{"page": 22, "disputed": 6, "rows": 5}]
