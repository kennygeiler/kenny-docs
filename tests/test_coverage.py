"""Provision coverage (ROADMAP M1.3, M1.5): /api/coverage counts, the chat meter line, the
admin Coverage tab, per-section approval in the Review queue, and the provision named in a
costing refusal. Runs on a tmp copy of the case with tests/fixtures/provisions_fixture.yaml
(the real inventory is cases/santacruz/provisions.yaml, written separately)."""
import os
import re
import shutil
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import app as core_app  # noqa: E402
from core.services import state as core_state  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "provisions_fixture.yaml")
HOLIDAY_Q = "Cost an 8-hour holiday shift for a Firefighter/Paramedic (56 hr, top step)"


def _case_copy(tmp_path, monkeypatch, with_provisions=True):
    case = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    if os.path.exists(case / "provisions.yaml"):
        os.remove(case / "provisions.yaml")   # the real inventory, once it lands
    if with_provisions:
        shutil.copy(FIXTURE, case / "provisions.yaml")
    monkeypatch.setattr(core_state, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return case, TestClient(core_app.app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _case_copy(tmp_path, monkeypatch)[1]


@pytest.fixture
def bare(tmp_path, monkeypatch):
    return _case_copy(tmp_path, monkeypatch, with_provisions=False)[1]


def test_api_counts_every_provision(client):
    cov = client.get("/api/coverage").json()
    assert cov["available"] is True
    (doc,) = cov["documents"]
    assert doc["doc_id"] == "firefighters_local3535_mou"
    assert doc["short_title"] == "Local 3535 MOU"
    assert (doc["total"], doc["live"], doc["awaiting"], doc["not_modelled"], doc["errors"]) \
        == (5, 2, 1, 1, 1)
    # Every fixture provision yields a value; narrative ones (value_kind none) count as
    # "quoted" so the meter's denominator is provisions that can have a number.
    assert (doc["numeric"], doc["quoted"]) == (5, 0)
    by_id = {p["id"]: p for p in doc["provisions"]}
    assert by_id["l3535.overtime_premium"]["state"] == "live"
    assert by_id["l3535.vacation_accrual"]["state"] == "awaiting"
    assert {r["state"] for r in by_id["l3535.vacation_accrual"]["rules"]} == {"proposed"}
    assert by_id["l3535.holiday_pay"]["state"] == "not_modelled"
    assert by_id["l3535.holiday_pay"]["reason"] == "needs the holiday calendar per shift"
    bad = by_id["l3535.uniform"]
    assert bad["state"] == "error" and "uniform_allowance_missing" in bad["error"]
    # the review queue groups by this index
    idx = cov["rule_index"]
    assert idx["firefighters_local3535_mou:vacation_accrual_1_5"]["article"] == "XV. Vacation"
    assert idx["firefighters_local3535_mou:vacation_accrual_1_5"]["pages"] == [12, 13]


def test_hidden_without_provisions_file(bare):
    cov = bare.get("/api/coverage").json()
    assert cov == {"available": False, "documents": [], "rule_index": {}}
    # every surface starts hidden and only shows itself on available: true
    assert re.search(r'id="coverageLine"[^>]*hidden', bare.get("/").text)
    assert re.search(r'id="tab-coverage"[^>]*hidden', bare.get("/admin").text)
    js = bare.get("/static/app.js").text
    assert "!cov.available" in js
    ajs = bare.get("/static/admin.js").text
    assert "$('tab-coverage').hidden = !on" in ajs
    assert "PROV.available ? reviewSections(doc, rules) : rules.map(reviewCard)" in ajs


def test_manifest_key_and_multi_document_shape(tmp_path, monkeypatch):
    case, client = _case_copy(tmp_path, monkeypatch, with_provisions=False)
    with open(FIXTURE) as f:
        one = yaml.safe_load(f)
    other = {"doc_id": "chief_officers_mou", "provisions": [
        {"id": "co.bereavement", "title": "Bereavement", "article": "X", "pages": [5],
         "status": "modelled", "rules": ["chief_officers_mou:bereavement_hours"]}]}
    (case / "inventory").mkdir()
    with open(case / "inventory" / "prov.yaml", "w") as f:
        yaml.safe_dump({"documents": [one, other]}, f)
    with open(case / "case.yaml") as f:
        manifest = yaml.safe_load(f)
    manifest["provisions"] = "inventory/prov.yaml"
    with open(case / "case.yaml", "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False)
    cov = client.get("/api/coverage").json()
    assert [d["doc_id"] for d in cov["documents"]] == ["firefighters_local3535_mou",
                                                       "chief_officers_mou"]
    assert cov["documents"][1]["live"] == 1 and cov["documents"][1]["total"] == 1
    # no label: the declared title without its parenthetical
    assert "(" not in cov["documents"][1]["short_title"]


def test_refusal_names_the_provision(client):
    res = client.post("/chat", json={"prompt": HOLIDAY_Q}).json()
    assert res["mode"] == "refused"
    assert "Holiday pay (Local 3535 MOU p.10) is not modelled: needs the holiday " \
           "calendar per shift." in res["message"]
    assert [p["id"] for p in res["provisions"]] == ["l3535.holiday_pay"]


def test_refusal_unchanged_without_provisions(bare):
    res = bare.post("/chat", json={"prompt": HOLIDAY_Q}).json()
    assert res["mode"] == "refused"
    assert not res.get("provisions") and "not modelled" not in res["message"]


def test_chat_meter_line(client):
    html = client.get("/").text
    assert 'id="coverageLine"' in html
    js = client.get("/static/app.js").text
    assert "fetch('/api/coverage')" in js
    assert "provisions with a number are live" in js and "awaiting approval" in js
    assert "a.href = '/admin#coverage'" in js and "see what's covered" in js
    assert "line.textContent" in js, "built with DOM calls, never innerHTML"


def test_admin_coverage_tab_and_section_approval(client):
    html = client.get("/admin").text
    assert 'id="panel-coverage"' in html and 'id="covSearch"' in html
    js = client.get("/static/admin.js").text
    tabs = re.search(r"const TABS = \[([^\]]*)\]", js).group(1)
    assert "'coverage'" in tabs
    # grouped by article; page links open Compare like the other citation buttons
    assert "data-act=\"compare\"" in js and "const pageLink" in js
    assert "'No article'" in js and "'Other'" in js
    # one action per section: selects that group's rules, then the ordinary ratify()
    assert "data-act=\"approve-section\"" in js and "Approve this section" in js
    body = re.search(r"function approveSection\(ids\) \{(.*?)\n    \}", js, re.S).group(1)
    assert "ratify()" in body and "c.disabled" in body
    assert "fetch('/admin/ratify'" in js and js.count("fetch('/admin/ratify'") == 1
    # data errors are shown, not hidden
    assert "Data errors in provisions.yaml" in js
    css = client.get("/static/styles.css").text
    assert ".cov-item" in css and ".linkbtn" in css
