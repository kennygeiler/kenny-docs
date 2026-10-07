"""Extraction-tier chips (OCR-4): every source chip / citation says where its text
came from. The mapping lives server-side in ONE place (core/app.py::_extraction_tier);
app.js only renders the `tier` field it stamps."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core.app import _extraction_tier  # noqa: E402
from core.index import LocalBM25Backend  # noqa: E402
from core.ingest import chunk_clauses  # noqa: E402


# --------------------------------------------------------------------------- #
# the one mapping: (parse_source, kind) -> tier label (None = no chip)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("parse_source,kind,tier", [
    ("docling", "text", "text layer"),
    ("docling", "table-row", "text layer"),
    ("docling", "recovered-row", "recovered layout"),
    ("docling", "recovered-text", "recovered layout"),
    ("raw-text-fallback", "page-text", "page-level"),
    ("raw-text-fallback", "text", "page-level"),
    ("sidecar", "text", "sidecar extract"),
    # no claim beats a wrong claim
    ("docling", "page-text", None),
    ("empty", "text", None),
    ("", "text", None),
    ("", "", None),
])
def test_extraction_tier_mapping(parse_source, kind, tier):
    assert _extraction_tier(parse_source, kind) == tier


# The origin axis (D1): a docling parse of a scan with an invisible OCR layer says
# so; everything else keeps its label (a scan cannot promote a fallback tier, and an
# unknown origin — a catalog that predates origin capture — makes no claim).
@pytest.mark.parametrize("parse_source,kind,origin,tier", [
    ("docling", "text", "ocr-layer", "OCR'd scan"),
    ("docling", "table-row", "ocr-layer", "OCR'd scan"),
    ("docling", "text", "digital", "text layer"),
    ("docling", "text", None, "text layer"),
    ("docling", "text", "mixed", "text layer"),
    ("docling", "recovered-row", "ocr-layer", "recovered layout"),
    ("raw-text-fallback", "text", "ocr-layer", "page-level"),
    ("sidecar", "text", "ocr-layer", "sidecar extract"),
    ("docling", "page-text", "ocr-layer", None),
])
def test_extraction_tier_origin_axis(parse_source, kind, origin, tier):
    assert _extraction_tier(parse_source, kind, origin) == tier


def test_page_origin_prefers_the_page_record_over_the_document():
    entry = {"text_origin": "ocr-layer", "text_origin_pages": {"36": "digital"}}
    assert core_app._page_origin(entry, 22) == "ocr-layer"
    assert core_app._page_origin(entry, 36) == "digital"
    assert core_app._page_origin({"text_origin": "digital"}, 1) == "digital"
    assert core_app._page_origin({}, 1) is None and core_app._page_origin(None, 1) is None


def test_recovered_kind_outranks_parse_source():
    """A docling doc can still contain pages the layout model misread; those clauses'
    recovered-* kind must win over the doc-level docling tier."""
    assert _extraction_tier("docling", "recovered-row") == "recovered layout"


# --------------------------------------------------------------------------- #
# hits carry `kind`; pre-OCR-4 indexes (no kind on chunks) default to "text"
# --------------------------------------------------------------------------- #
def test_hit_carries_kind_and_tolerates_its_absence(tmp_path):
    be = LocalBM25Backend(str(tmp_path / "idx.jsonl"))
    be.index("mou", chunk_clauses([
        {"clause": "9.1", "page": 1, "bbox": [], "kind": "table-row",
         "text": "Sergeant | $53.00 | $58.00"},
        {"clause": "9.2", "page": 1, "bbox": [],
         "text": "Graveyard shift differential of 8%."},   # no kind = normal text
    ]))
    row = be.search("sergeant 53.00", doc_ids=["mou"], k=1)[0]
    assert row["kind"] == "table-row"
    prose = be.search("graveyard differential", doc_ids=["mou"], k=1)[0]
    assert prose["kind"] == "text"
    # a chunk written by a pre-kind bake (the committed santacruz index) also reads
    # back as "text" — the backend must never require the field
    be.index("old", [{"chunk_id": "0", "clause": "1", "page": 1, "bbox": [],
                      "text": "uniform allowance of $500"}])
    assert be.search("uniform allowance", doc_ids=["old"], k=1)[0]["kind"] == "text"


# --------------------------------------------------------------------------- #
# citations of a costing/entitlement result get parse_source / kind / tier
# --------------------------------------------------------------------------- #
class _Cat:
    def __init__(self, entries):
        self._e = entries
    def get(self, doc_id):
        return self._e.get(doc_id)
    def clauses(self, doc_id):
        return (self._e.get(doc_id) or {}).get("clauses", [])


def test_enrich_citations_stamps_provenance():
    cat = _Cat({"mou": {"parse_source": "docling", "clauses": [
        {"clause": "9.1", "page": 3, "bbox": [1, 2, 3, 4], "kind": "recovered-row"},
        {"clause": "9.2", "page": 4, "bbox": [5, 6, 7, 8]},
    ]}})
    rd = core_app._enrich_citations(cat, {"line_items": [{"citations": [
        {"doc_id": "mou", "clause": "9.1", "page": 0, "bbox": []},
        {"doc_id": "mou", "clause": "9.2", "page": 0, "bbox": []},
        {"doc_id": "ghost", "clause": "1", "page": 0, "bbox": []},
    ]}]})
    c1, c2, c3 = rd["line_items"][0]["citations"]
    assert (c1["parse_source"], c1["kind"], c1["tier"]) == \
        ("docling", "recovered-row", "recovered layout")
    assert c1["bbox"] == [1, 2, 3, 4] and c1["page"] == 3     # bbox fill still works
    assert (c2["kind"], c2["tier"]) == ("text", "text layer")
    assert (c3["parse_source"], c3["kind"], c3["tier"]) == ("", "text", None)


# --------------------------------------------------------------------------- #
# end to end: a Santa Cruz policy answer's sources all carry the docling tier
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cases", "santacruz")
    case = tmp_path / "santacruz"
    shutil.copytree(src, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def test_santacruz_mou_sources_say_ocr_scan(client):
    """The four MOUs are scans with an invisible Tesseract layer (D1): every source
    chip drawn from them must say so — "text layer" was the lie the chip told before
    the back-fill recorded the origin."""
    res = client.post("/chat", json={"prompt": "What does the Firefighters Local 3535 "
                                               "MOU say about overtime?"}).json()
    assert res["mode"] == "policy"
    assert res["sources"], "expected retrieved sources for an indexed corpus"
    for s in res["sources"]:
        assert s["parse_source"] == "docling"
        if s["doc_id"].endswith("_mou") and s["page"] <= 34:
            assert s["text_origin"] == "ocr-layer"
            assert s["tier"] == "OCR'd scan", s


def test_salary_schedule_hit_still_says_text_layer(client, monkeypatch):
    """The born-digital salary schedule keeps its honest label, through the same
    source-chip stamp the chat path uses."""
    case = core_app._case()
    cat = core_app._catalog(case)
    hit = {"doc_id": "master_salary_schedule", "clause": "", "page": 1, "bbox": [],
           "text": "Firefighter/Paramedic | $53.40", "score": 1.0, "kind": "table-row"}
    s = core_app._source_entry(case, cat, hit)
    assert s["text_origin"] == "digital" and s["tier"] == "text layer"
    scan = {**hit, "doc_id": "firefighters_local3535_mou", "page": 8, "kind": "text"}
    s = core_app._source_entry(case, cat, scan)
    assert s["text_origin"] == "ocr-layer" and s["tier"] == "OCR'd scan"
    # the digital appendix at the back of the scanned MOU is judged per page
    s = core_app._source_entry(case, cat, {**scan, "page": 36})
    assert s["text_origin"] == "digital" and s["tier"] == "text layer"


def test_costing_citation_carries_the_scan_origin(client):
    res = client.post("/chat", json={"prompt": "What is the cost of an 8-hour overtime "
                                               "shift for a Firefighter/Paramedic "
                                               "(56 hr, top step)?"}).json()
    cits = [c for li in res.get("result", {}).get("line_items", []) for c in li["citations"]]
    assert cits, res
    assert all(c["tier"] == "OCR'd scan" and c["text_origin"] == "ocr-layer" for c in cits), cits


def test_app_js_explains_the_scan_chip(client):
    js = client.get("/static/app.js").text
    assert "\"OCR'd scan\"" in js and "Tesseract" in js
