"""Numeric-cell verification (DEMO_TICKETS.md D2): parse, column checks, the
second-engine re-read, alignment, the stored verdicts behind /doc/{id}/clauses, and
the evidence gate — a rule may not cite a disputed cell."""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import cellcheck  # noqa: E402
from core.catalog import Catalog  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE = os.path.join(ROOT, "cases", "santacruz")
FIRE = "firefighters_local3535_mou"
FIRE_PDF = os.path.join(CASE, "sources", f"{FIRE}.pdf")
P22_BBOX = [109.8319091796875, 589.5073699951172, 534.30712890625, 449.7714538574219]


# --------------------------------------------------------------------------- #
# parse_cell
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,value,fmt", [
    ("$02.31", 2.31, "currency"),        # parses — the COLUMN check catches it
    ("$ 1,044.53", 1044.53, "currency"),
    ("7,38", 7.38, "decimal"),           # decimal comma, how Tesseract read 7.38
    ("994,79", 994.79, "decimal"),
    ("10.15", 10.15, "decimal"),
    ("8,510.74", 8510.74, "decimal"),
    ("1,015", 1015.0, "int"),            # thousands comma
    ("12", 12.0, "int"),
    ("2.50%", 2.5, "percent"),
    ("1-5", 1.0, "range"),
    ("11 - 13", 11.0, "range"),
    ("17 +", 17.0, "range"),
    ("17+", 17.0, "range"),
    ("0) £5", None, "text"),
    ("fs 13,85", None, "text"),
    ("A468", None, "text"),
    ("fd", None, "text"),
    ("", None, "text"),
    (None, None, "text"),
])
def test_parse_cell(raw, value, fmt):
    v, f = cellcheck.parse_cell(raw)
    assert f == fmt
    if value is None:
        assert v is None
    else:
        assert v == pytest.approx(value)


# --------------------------------------------------------------------------- #
# the stored p.22 grid and the pure-code checks
# --------------------------------------------------------------------------- #
def _p22_grid():
    cat = Catalog(os.path.join(CASE, "catalog.json"))
    on_page = [c for c in cat.clauses(FIRE) if c.get("page") == 22]
    groups = cellcheck.table_groups(on_page)
    assert len(groups) == 1 and len(groups[0]) == 6
    grid = cellcheck.grid_from_rows(groups[0])
    assert grid and len(grid["rows"]) == 5 and grid["cols"] == 5
    return grid


def test_p22_grid_matches_the_catalog_text():
    grid = _p22_grid()
    labels = [r["cells"][0] for r in grid["rows"]]
    assert labels == ["1-5", "6-10", "11-13", "14-16", "17 +"]
    assert grid["rows"][1]["cells"][1] == "0) £5"
    assert grid["rows"][2]["cells"][4] == "A468"
    assert any("Hours Accrued" in h for h in grid["headers"])


def test_check_table_flags_every_bad_p22_row_and_not_1_to_5():
    grid = _p22_grid()
    flags = cellcheck.check_table([r["cells"] for r in grid["rows"]], grid["headers"])
    bad = {i for i, row in enumerate(flags) if any(row)}
    assert bad == {1, 2, 3, 4}, flags
    assert "unparsable" in flags[1][1] and "unparsable" in flags[1][2]     # 6-10
    assert "unparsable" in flags[2][4]                                      # 11-13 'A468'
    assert "unparsable" in flags[3][1] and "invariant" in flags[3][2]       # 14-16 '45'
    assert "unparsable" in flags[4][1]                                      # 17+
    assert not any(flags[0])                                                # 1-5


def test_invariants_hold_on_the_printed_values():
    rows = [["1-5", "7.38", "8", "192", "288"], ["6-10", "10.15", "11", "264", "396"],
            ["11-13", "12", "13", "312", "468"], ["14-16", "13.85", "15", "360", "540"],
            ["17+", "14.78", "16", "384", "576"]]
    headers = _p22_grid()["headers"]
    assert cellcheck.table_spec(headers)["name"] == "vacation-accrual"
    flags = cellcheck.check_table(rows, headers)
    assert not any(any(r) for r in flags)


def test_column_disagreement_marks_both_02_31_and_92_31():
    headers = ["Monthly", "1STED", "2NDED"]
    rows = [["8,510.74", "$02.31", "$138.46"], ["8,936.28", "$92.31", "$138.46"],
            ["9,383.09", "$02.31", "$138.46"]]
    flags = cellcheck.check_table(rows, headers)
    assert all("column_disagreement" in flags[i][1] for i in range(3))   # never a vote
    assert not any(flags[i][2] for i in range(3))                          # 2NDED agrees
    assert not any(flags[i][0] for i in range(3))                          # Monthly varies, not constant


def test_format_disagreement_in_a_column():
    flags = cellcheck.check_table([["a", "12.5%"], ["b", "$12.50"]], None)
    assert "column_disagreement" in flags[0][1] and "column_disagreement" in flags[1][1]


# --------------------------------------------------------------------------- #
# alignment with a stubbed second read (no engine)
# --------------------------------------------------------------------------- #
def _fake_tokens(values, x0=120.0, y0=560.0, dx=90.0, dy=20.0):
    """Lay token texts out on a grid in PDF points (bottom-left origin)."""
    toks = []
    for i, row in enumerate(values):
        for j, txt in enumerate(row):
            if txt is None:
                continue
            l, t = x0 + j * dx, y0 - i * dy
            toks.append({"text": txt, "score": 0.97, "bbox": [l, t, l + 24, t - 9]})
    return toks


PRINTED = [["1-5", "7.38", "8", "192", "288"], ["6-10", "10.15", "11", "264", "396"],
           ["11-13", "12", "13", "312", "468"], ["14-16", "13.85", "15", "360", "540"],
           ["17+", "14.78", "16", "384", "576"]]


def test_verify_page_marks_6_to_10_hours_disputed_with_reread_10_15():
    cat = Catalog(os.path.join(CASE, "catalog.json"))
    rec = cellcheck.verify_page(cat.get(FIRE), FIRE_PDF, 22,
                                tokens_for=lambda bbox: _fake_tokens(PRINTED))
    rows = {r["ordinal"]: r for r in rec["rows"]}
    assert sorted(rows) == [1, 2, 3, 4, 5]           # ordinal 0 is the summary line
    r15, r610, r1113, r1416 = rows[1], rows[2], rows[3], rows[4]
    assert r15["cell_status"] == "verified"
    assert all(c["status"] == "verified" for c in r15["cells"])
    hours = r610["cells"][1]
    assert hours["stored"] == "0) £5" and hours["status"] == "disputed"
    assert hours["reread"] == "10.15" and hours["reread_value"] == pytest.approx(10.15)
    assert hours["bbox"] and len(hours["bbox"]) == 4
    assert r610["cells"][2]["reread"] == "11" and r610["cells"][2]["status"] == "disputed"
    assert r1113["cells"][4]["stored"] == "A468" and r1113["cells"][4]["reread"] == "468"
    assert r1416["cells"][2]["stored"] == "45" and r1416["cells"][2]["reread"] == "15"
    assert r610["cell_status"] == "disputed"
    assert all(r["text_sha"] for r in rec["rows"])


def test_decimal_dropped_by_the_engine_still_agrees():
    assert cellcheck._values_agree((7.38, "decimal"), "738") == (True, 7.38)
    assert cellcheck._values_agree((7.38, "decimal"), "7.38")[0]
    assert not cellcheck._values_agree((45.0, "int"), "15")[0]
    assert not cellcheck._values_agree((None, "text"), "10.15")[0]


def test_no_token_and_no_flag_is_unverified():
    cat = Catalog(os.path.join(CASE, "catalog.json"))
    rec = cellcheck.verify_page(cat.get(FIRE), FIRE_PDF, 22, tokens_for=lambda b: [])
    row15 = next(r for r in rec["rows"] if r["ordinal"] == 1)
    assert all(c["status"] == "unverified" for c in row15["cells"][1:])
    assert row15["cells"][0]["status"] == "unverified"
    row610 = next(r for r in rec["rows"] if r["ordinal"] == 2)
    assert row610["cells"][1]["status"] == "disputed"       # flagged by pure code


def test_engine_is_never_built_by_import_or_pure_code_paths():
    assert cellcheck._ENGINE is None
    _p22_grid()
    cellcheck.check_table([["1", "2"]], None)
    assert cellcheck._ENGINE is None


# --------------------------------------------------------------------------- #
# the second engine itself (skipped when rapidocr/torch or its models are absent)
# --------------------------------------------------------------------------- #
def _models_present() -> bool:
    try:
        import rapidocr
    except Exception:
        return False
    d = os.path.join(os.path.dirname(rapidocr.__file__), "models")
    return os.path.exists(os.path.join(d, "en_PP-OCRv4_rec_mobile.pth")) and \
        os.path.exists(os.path.join(d, "en_PP-OCRv3_det_mobile.pth"))


@pytest.mark.skipif(not (cellcheck.engine_available() and _models_present()),
                    reason="rapidocr torch backend or its English models unavailable")
def test_reread_p22_with_rapidocr_reads_the_printed_values():
    toks = cellcheck.reread_table(FIRE_PDF, 22, P22_BBOX)
    values = {cellcheck.parse_cell(t["text"])[0] for t in toks}
    assert {10.15, 13.85, 468.0} <= values, sorted(v for v in values if v is not None)
    # token boxes are mapped back into the table box, in PDF points
    for t in toks:
        l, top, r, b = t["bbox"]
        assert P22_BBOX[0] - 1 <= l <= r <= P22_BBOX[2] + 1
        assert P22_BBOX[3] - 1 <= b <= top <= P22_BBOX[1] + 1
    cat = Catalog(os.path.join(CASE, "catalog.json"))
    rec = cellcheck.verify_page(cat.get(FIRE), FIRE_PDF, 22, tokens_for=lambda b: toks)
    rows = {r["ordinal"]: r for r in rec["rows"]}
    assert rows[1]["cell_status"] == "verified"
    assert rows[2]["cells"][1]["status"] == "disputed"
    assert rows[2]["cells"][1]["reread_value"] == pytest.approx(10.15)


# --------------------------------------------------------------------------- #
# the shipped record + HTTP surfaces
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app), str(case)


def test_shipped_cell_checks_cover_the_three_pages():
    data = cellcheck.load_checks(CASE)
    assert {"firefighters_local3535_mou:22", "admin_group_mou:29", "chief_officers_mou:26"} \
        <= set(data["pages"])
    assert data["engine"]["name"] == "rapidocr" and data["engine"]["backend"] == "torch"
    p22 = data["pages"]["firefighters_local3535_mou:22"]
    hours = next(r for r in p22["rows"] if r["ordinal"] == 2)["cells"][1]
    assert hours["stored"] == "0) £5" and hours["reread"] == "10.15"
    p29 = data["pages"]["admin_group_mou:29"]
    first_ed = [c for r in p29["rows"] for c in r["cells"] if c["stored"] == "$02.31"]
    assert len(first_ed) == 3 and all(c["status"] == "disputed" and c["reread"] == "$92.31"
                                      for c in first_ed)
    assert data["showcase"] == {"doc": FIRE, "page": 22,
                                "label": "Vacation accrual table, where the OCR misread 10.15"}


def test_clauses_p22_carries_cell_status(client):
    c, _ = client
    body = c.get(f"/doc/{FIRE}/clauses", params={"page": 22}).json()
    rows = [cl for cl in body["clauses"] if cl["kind"] == "table-row"]
    assert len(rows) == 6
    data_rows = [r for r in rows if r["cells"]]
    assert len(data_rows) == 5 and all(r["cell_status"] for r in data_rows)
    assert [r["cell_status"] for r in data_rows] == \
        ["verified", "disputed", "disputed", "disputed", "disputed"]
    assert body["disputed_cells"] == 6
    assert body["text_origin"] == "ocr-layer"
    # non-table clauses carry the keys too, empty — one shape for every consumer
    prose = [cl for cl in body["clauses"] if cl["kind"] != "table-row"]
    assert prose and all(cl["cells"] == [] and cl["cell_status"] is None for cl in prose)


def test_cell_status_does_not_survive_a_changed_extraction(client):
    c, case_dir = client
    cat_path = os.path.join(case_dir, "catalog.json")
    with open(cat_path) as f:
        data = json.load(f)
    doc = next(d for d in data["documents"] if d["doc_id"] == FIRE)
    row = [cl for cl in doc["clauses"] if cl.get("page") == 22 and cl.get("kind") == "table-row"][2]
    row["text"] = row["text"].replace("0) £5", "10.15")      # a re-ingest fixed the cell
    with open(cat_path, "w") as f:
        json.dump(data, f)
    body = c.get(f"/doc/{FIRE}/clauses", params={"page": 22}).json()
    rows = [cl for cl in body["clauses"] if cl["kind"] == "table-row"]
    assert rows[2]["cells"] == [] and rows[2]["cell_status"] is None
    assert rows[1]["cell_status"] == "verified"             # untouched rows keep theirs


def test_disputed_cell_is_refused_as_evidence(client):
    c, _ = client
    bbox = ",".join(str(v) for v in P22_BBOX)
    res = c.get("/admin/clause", params={"doc_id": FIRE, "page": 22, "bbox": bbox})
    assert res.status_code == 409
    body = res.json()
    assert "disputed" in body["error"] and "Compare" in body["error"]
    stored = {x["stored"] for x in body["cells"]}
    assert {"0) £5", "A468", "fs 13,85", "45", "; 14,78", "fd"} <= stored
    assert any(x["reread"] == "10.15" for x in body["cells"])
    # a prose citation on the same document is still served
    ok = c.get("/admin/clause", params={"doc_id": FIRE, "page": 8})
    assert ok.status_code == 200 and ok.json()["resolved_by"] == "page"
    # a box that overlaps nothing disputed is served too
    ok = c.get("/admin/clause", params={"doc_id": FIRE, "page": 22, "bbox": "110,680,214,670"})
    assert ok.status_code == 200


def test_disputed_cell_is_refused_by_clause_label_too(client):
    c, case_dir = client
    cat_path = os.path.join(case_dir, "catalog.json")
    with open(cat_path) as f:
        data = json.load(f)
    doc = next(d for d in data["documents"] if d["doc_id"] == FIRE)
    rows = [cl for cl in doc["clauses"] if cl.get("page") == 22 and cl.get("kind") == "table-row"]
    rows[2]["clause"] = "XV-6-10"
    rows[1]["clause"] = "XV-1-5"
    with open(cat_path, "w") as f:
        json.dump(data, f)
    assert c.get("/admin/clause", params={"doc_id": FIRE, "clause": "XV-6-10"}).status_code == 409
    assert c.get("/admin/clause", params={"doc_id": FIRE, "clause": "XV-1-5"}).status_code == 200


def test_gate_function_for_the_ratify_path():
    cat = Catalog(os.path.join(CASE, "catalog.json"))
    cells = cellcheck.disputed_for_citation(CASE, cat, FIRE, page=22, bbox=P22_BBOX)
    assert cells and all(x["status"] != "verified" for x in cells)
    assert cellcheck.disputed_for_citation(CASE, cat, FIRE, page=8,
                                           bbox=[100, 700, 500, 600]) == []
    # by the row's own text, as a rule citation quoting the row would carry it
    row = [cl for cl in cat.clauses(FIRE) if cl.get("page") == 22
           and cl.get("kind") == "table-row"][2]
    assert cellcheck.disputed_for_citation(CASE, cat, FIRE, text=row["text"])
    # the catalog's own clause dicts were not stamped
    assert all("cells" not in cl for cl in cat.clauses(FIRE))


def test_admin_cell_checks_endpoint(client):
    c, _ = client
    body = c.get("/admin/cell_checks").json()
    assert body["engine"]["name"] == "rapidocr"
    keys = {p["key"]: p for p in body["pages"]}
    assert keys["firefighters_local3535_mou:22"]["disputed"] == 6
    assert body["showcase"]["page"] == 22
