"""Evidence-based stale check (DEMO_TICKETS.md A2): re-reading a contract only stales
a rule when its evidence actually moved.

Before: `_revalidate_citations` compared clause LABELS. The shipped rules carry
analyst labels ('XIV', 'Overtime Rate (p.8)') and the OCR'd catalog has almost none,
so an identical re-ingest staled all four live rules and emptied the library.
"""
import copy
import json
import os
import shutil
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import evidence, ingest  # noqa: E402
from core.app import _raw_ratified, _revalidate_citations  # noqa: E402
from core.caseio import load_case  # noqa: E402
from core.catalog import Catalog  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = ("What does an 8-hour overtime shift cost for a "
          "Firefighter/Paramedic (56 hr, top step)?")
OT_RULE = "firefighters_local3535_mou:overtime_premium_rate"
FIRE = "firefighters_local3535_mou"


@pytest.fixture
def case_copy(tmp_path, monkeypatch):
    dst = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), dst)
    monkeypatch.setattr(core_app, "CASE_DIR", str(dst))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    core_app._JOBS.clear()
    return str(dst)


def _load(case_dir):
    case = load_case(case_dir)
    return case, Catalog(case.path("catalog", "catalog.json")), case.ledger()


def _all_docs(case):
    return [s["id"] for s in case.manifest["sources"]]


def _types(led):
    return [e["type"] for e in led.read()]


def _ot_citation(case):
    return next(r for r in _raw_ratified(case) if r["id"] == OT_RULE)["citation"]


def _matched_clause(cat, cit):
    found, how = evidence.locate(cat.clauses(cit["doc_id"]), cit)
    assert found is not None, how
    return found


# --------------------------------------------------------------------------- #
# evidence primitives
# --------------------------------------------------------------------------- #
def test_iou_and_text_sha():
    assert evidence.iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert evidence.iou([0, 10, 10, 0], [0, 0, 10, 10]) == 1.0      # either y-orientation
    assert evidence.iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert evidence.iou([], [0, 0, 1, 1]) == 0.0
    assert evidence.text_sha(" One  and\nONE-half ") == evidence.text_sha("one and one-half")
    assert len(evidence.text_sha("x")) == 16


# --------------------------------------------------------------------------- #
# the shipped library against its own catalog
# --------------------------------------------------------------------------- #
def test_shipped_rules_survive_own_catalog(case_copy):
    case, cat, led = _load(case_copy)
    assert _revalidate_citations(case, cat, _all_docs(case), led) == []
    assert len(case.rules()) == 4
    assert "authoring.stale" not in _types(led)
    for r in _raw_ratified(case):
        assert r["status"] == "ratified"
        assert r["citation"].get("quote_sha256"), r["id"]
        assert r["citation"].get("quote"), r["id"]


def test_reingest_identical_extraction_keeps_library(case_copy, monkeypatch):
    from fastapi.testclient import TestClient
    case, cat, _ = _load(case_copy)
    baked = {d["doc_id"]: copy.deepcopy(d["clauses"]) for d in cat.documents()}
    monkeypatch.setattr(ingest, "parse_pdf",
                        lambda path, doc_id: (baked[doc_id], "", "docling", {}))
    monkeypatch.setenv("SEARCH_BACKEND", "bm25")        # no embedding pass in a test
    c = TestClient(core_app.app)
    job = c.post("/admin/ingest").json()
    deadline = time.time() + 60
    while time.time() < deadline:
        body = c.get(f"/admin/ingest/status/{job['job_id']}").json()
        if body["status"] != "running":
            break
        time.sleep(0.05)
    assert body["status"] == "done", body
    assert body["result"]["stale_rules"] == []
    assert body["result"]["rules_rechecked"] == 4
    assert c.get("/admin/proposed").json()["stale_rules"] == []
    assert c.get("/admin/verification").json()["rule_count"] == 4
    assert c.post("/chat", json={"prompt": GOLDEN}).json()["result"]["total"] == 640.8


def test_missing_passage_goes_stale(case_copy):
    from fastapi.testclient import TestClient
    case, cat, led = _load(case_copy)
    cit = _ot_citation(case)
    gone = _matched_clause(cat, cit)
    entry = cat.get(FIRE)
    entry["clauses"] = [cl for cl in entry["clauses"] if cl is not gone]
    cat.upsert(entry)
    stale = _revalidate_citations(case, cat, _all_docs(case), led)
    assert [s["rule_id"] for s in stale] == [OT_RULE]
    assert "p.8" in stale[0]["reason"] and "no longer exists" in stale[0]["reason"]
    assert [e["payload"]["rule_id"] for e in led.read()
            if e["type"] == "authoring.stale"] == [OT_RULE]
    assert len(case.rules()) == 3
    res = TestClient(core_app.app).post("/chat", json={"prompt": GOLDEN}).json()
    assert res["mode"] == "blocked", res
    assert "re-verification" in res["message"]


def test_jitter_is_not_stale(case_copy):
    case, cat, led = _load(case_copy)
    for entry in cat.documents():
        for cl in entry["clauses"]:
            if cl.get("bbox") and len(cl["bbox"]) == 4:
                cl["bbox"] = [v + 1.0 for v in cl["bbox"]]
        cat.upsert(entry)
    assert _revalidate_citations(case, cat, _all_docs(case), led) == []
    assert len(case.rules()) == 4


def test_text_change_under_bound_quote_is_stale(case_copy):
    case, cat, led = _load(case_copy)
    assert _revalidate_citations(case, cat, [FIRE], led) == []     # binds the quote
    cit = _ot_citation(case)
    assert cit["quote_sha256"]
    target = _matched_clause(cat, cit)
    entry = cat.get(FIRE)
    for cl in entry["clauses"]:
        if cl is target:
            cl["text"] = "two (2.0) times the employee's regular rate of pay"
    cat.upsert(entry)
    stale = _revalidate_citations(case, cat, [FIRE], led)
    assert [s["rule_id"] for s in stale] == [OT_RULE]
    assert "text" in stale[0]["reason"] and "changed" in stale[0]["reason"]


def test_moved_box_same_text_rebinds(case_copy):
    case, cat, led = _load(case_copy)
    assert _revalidate_citations(case, cat, [FIRE], led) == []     # binds the quote
    cit_before = dict(_ot_citation(case))
    target = _matched_clause(cat, cit_before)
    entry = cat.get(FIRE)
    for cl in entry["clauses"]:
        if cl is target:
            x0, y0, x1, y1 = cl["bbox"]
            cl["bbox"] = [x0, y0 - 300, x1, y1 - 300]              # same words, new box
    cat.upsert(entry)
    assert _revalidate_citations(case, cat, [FIRE], led) == []
    cit_after = _ot_citation(case)
    assert cit_after["bbox"] != cit_before["bbox"]
    assert cit_after["bbox"] == target["bbox"]
    assert cit_after["quote_sha256"] == cit_before["quote_sha256"]
    rebound = [e for e in led.read() if e["type"] == "authoring.rebound"]
    assert len(rebound) == 1 and rebound[0]["payload"]["rule_id"] == OT_RULE
    assert rebound[0]["payload"]["from"]["bbox"] == cit_before["bbox"]
    assert len(case.rules()) == 4


def test_unchanged_pdf_is_skipped(case_copy, monkeypatch):
    from fastapi.testclient import TestClient
    case, cat, _ = _load(case_copy)
    entry = cat.get("master_salary_schedule")
    pdf = os.path.join(case.dir, "sources", "master_salary_schedule.pdf")
    entry["pdf_sha256"] = ingest.sha256_file(pdf)
    cat.upsert(entry)
    parsed = []

    def fake_parse(path, doc_id):
        parsed.append(doc_id)
        return [], "", "docling", {}

    monkeypatch.setattr(ingest, "parse_pdf", fake_parse)
    monkeypatch.setenv("SEARCH_BACKEND", "bm25")
    c = TestClient(core_app.app)
    job = c.post("/admin/ingest").json()
    deadline = time.time() + 60
    while time.time() < deadline:
        body = c.get(f"/admin/ingest/status/{job['job_id']}").json()
        if body["status"] != "running":
            break
        time.sleep(0.05)
    assert body["status"] == "done", body
    assert "master_salary_schedule" not in parsed
    assert body["result"]["skipped_unchanged"] == ["master_salary_schedule"]
    assert len(parsed) == 4
    # force=true re-parses everything
    core_app._JOBS.clear()
    parsed.clear()
    job = c.post("/admin/ingest?force=true").json()
    deadline = time.time() + 60
    while time.time() < deadline:
        body = c.get(f"/admin/ingest/status/{job['job_id']}").json()
        if body["status"] != "running":
            break
        time.sleep(0.05)
    assert "master_salary_schedule" in parsed and body["result"]["skipped_unchanged"] == []


def test_citation_round_trips_quote_sha():
    from core.ruledsl import Citation
    c = Citation.from_dict({"doc_id": "d", "clause": "9.1", "page": 3,
                            "bbox": [1, 2, 3, 4], "quote_sha256": "ab" * 8})
    assert c.quote_sha256 == "ab" * 8
    assert c.to_dict()["quote_sha256"] == "ab" * 8
    assert Citation.from_dict({"doc_id": "d"}).to_dict()["quote_sha256"] == ""


def test_admin_reread_asks_before_posting():
    """The first button on the admin page used to POST on a single click."""
    html = open(os.path.join(ROOT, "core", "templates", "admin.html")).read()
    start = html.index("function ingest()")
    end = html.index("function ingestConfirmed()")
    first_step = html[start:end]
    assert "fetch('/admin/ingest'" not in first_step
    assert "ingestConfirm" in first_step and "Cancel" in first_step
    assert "fetch('/admin/ingest'" in html[end:]


# --------------------------------------------------------------------------- #
# back-fill script: idempotent, touches only the quote fields
# --------------------------------------------------------------------------- #
def test_backfill_script_is_idempotent_and_minimal(case_copy):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import backfill_quote_sha
    path = os.path.join(case_copy, "rules", "rules_ratified.json")
    before = json.load(open(path))
    n1 = backfill_quote_sha.run(case_copy)
    assert n1 == 4
    after = json.load(open(path))
    for b, a in zip(before["rules"], after["rules"]):
        a = copy.deepcopy(a)
        assert a["citation"].pop("quote_sha256") and a["citation"].pop("quote")
        assert a == b                                        # every other key identical
    assert list(after.keys()) == list(before.keys())
    bytes1 = open(path, "rb").read()
    assert backfill_quote_sha.run(case_copy) == 0
    assert open(path, "rb").read() == bytes1                 # second run is a no-op
    assert not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".bak")]
