"""G1: the skeptic's tools, validator, stored artifact, endpoints. No model, no key;
every test works on a temp copy of the shipped case."""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import skeptic  # noqa: E402
from core.caseio import load_case  # noqa: E402

FF_RULE = "firefighters_local3535_mou:overtime_premium_rate"
FF_DOC = "firefighters_local3535_mou"
FF = "Firefighter/Paramedic (56 hr, top step)"


@pytest.fixture
def case(tmp_path, monkeypatch):
    dst = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), dst)
    shutil.rmtree(dst / "reviews", ignore_errors=True)
    for runtime in ("ledger.jsonl", "snapshots"):   # a laptop's own records, not the case
        p = dst / runtime
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return load_case(str(dst))


@pytest.fixture
def rule(case):
    return skeptic.find_rule(case, FF_RULE)


def test_read_page_marks_the_cited_clause(case, rule):
    out = skeptic.read_page(case, rule, FF_DOC, 8)
    cited = [c for c in out["clauses"] if c["cited"]]
    assert len(cited) == 1 and "in excess of 182 hours" in cited[0]["text"]
    assert cited[0]["ref"] == f"{FF_DOC}#p8.3"


def test_search_outside_the_rules_scope_is_an_error(case, rule):
    assert "error" in skeptic.search_clauses(case, rule, "regular rate", "admin_group_mou")
    hits = skeptic.search_clauses(case, rule, "182 hours 24-day work period", FF_DOC)["hits"]
    assert hits and hits[0]["page"] == 7


def test_run_engine_live_and_variants(case, rule):
    sc = lambda h: {"subjects": [FF], "params": {"hours": h}}  # noqa: E731
    assert skeptic.run_engine(case, rule, "live", sc(8))["total"] == 640.8
    assert skeptic.run_engine(case, rule, "variant", sc(10),
                              {"compute": "effective_base * 0.5 * hours"})["total"] == 267.0
    err = skeptic.run_engine(case, rule, "variant", sc(8), {"when": "hours_in_work_period > 182"})
    assert "hours_in_work_period" in err["error"]
    assert "error" in skeptic.run_engine(case, rule, "live", sc(100000))
    assert "error" in skeptic.run_engine(case, rule, "variant", sc(8), {"compute": "2 ** 9"})
    assert "error" in skeptic.run_engine(case, rule, "variant", sc(8), {"compute": "1+" * 150 + "1"})


def _run(case, rule):
    run = skeptic.Run.start(case, rule, producer="test")
    run.tool("read_page", doc_id=FF_DOC, page=8)
    run.tool("run_engine", rule_set="live", scenario={"subjects": [FF], "params": {"hours": 10}})
    run.tool("run_engine", rule_set="variant",
             scenario={"subjects": [FF], "params": {"hours": 10}},
             variant={"compute": "effective_base * 0.5 * hours"})
    return run


def test_validate_review_drops_what_it_cannot_prove(case, rule):
    run = _run(case, rule)
    review = {"verdict": "challenge", "summary": "ok", "warnings": [
        {"id": "unread", "severity": "high", "kind": "adjacent_clause",
         "claim": "half-time band",
         "evidence": [{"doc_id": FF_DOC, "page": 7,
                       "quote": "the overtime premium for the hours between 182 and 192 Is at half-time"}]},
        {"id": "loose", "severity": "high", "kind": "ignored_condition",
         "claim": "the rule pays 999 dollars",
         "evidence": [{"doc_id": FF_DOC, "page": 8,
                       "quote": "in excess of 182 hours In a 24-day work period"}]},
        {"id": "fake", "severity": "high", "kind": "citation",
         "claim": "a fabricated quote",
         "evidence": [{"doc_id": FF_DOC, "page": 8,
                       "quote": "overtime is paid at triple time on alternate Tuesdays"}]},
        {"id": "ok", "severity": "high", "kind": "adjacent_clause",
         "claim": "10 hours pays 801.0 as written and 267.0 at half-time",
         "evidence": [{"doc_id": FF_DOC, "page": 8,
                       "quote": "one and one half (1,5) times the employee's 'regular rate of pay'"}],
         "counter_example": {"call_n": 3, "baseline_call_n": 2, "engine_total": 123.45,
                             "baseline_total": 1.0, "why": "half-time reading"}},
        {"id": "nocall", "severity": "low", "kind": "rounding",
         "claim": "half hour increments",
         "evidence": [{"doc_id": FF_DOC, "page": 8,
                       "quote": "accumulated in one-half hour Increments"}],
         "counter_example": {"call_n": 99, "why": "x"}},
    ]}
    clean, rejected = skeptic.validate_review(case, rule, review, run)
    ids = [w["id"] for w in clean["warnings"]]
    assert ids == ["ok", "nocall"]
    reasons = " ".join(r["reason"] for r in rejected)
    assert "never read" in reasons and "unverified figure" in reasons and "not found" in reasons
    ok = clean["warnings"][0]
    assert ok["counter_example"]["engine_total"] == 267.0, "total comes from the run log"
    assert ok["counter_example"]["baseline_total"] == 801.0
    assert ok["evidence"][0]["ref"] == f"{FF_DOC}#p8.3" and len(ok["evidence"][0]["bbox"]) == 4
    assert clean["warnings"][1]["counter_example"] is None
    assert any("no logged run_engine call" in r["reason"] for r in rejected)


def test_budget_exhausted_on_the_13th_call(case, rule):
    run = skeptic.Run.start(case, rule, producer="test")
    for i in range(4):
        assert "error" not in run.tool("search_clauses", query="overtime", doc_id=FF_DOC)
    assert run.tool("search_clauses", query="x", doc_id=FF_DOC) == {"error": "budget exhausted"}
    for p in range(6):
        assert "error" not in run.tool("read_page", doc_id=FF_DOC, page=5 + p)
    for _ in range(2):
        assert "error" not in run.tool("run_engine", rule_set="live",
                                        scenario={"subjects": [FF], "params": {"hours": 8}})
    assert len(run.calls) == 12
    assert run.tool("run_engine", rule_set="live",
                    scenario={"subjects": [FF], "params": {"hours": 8}}) == {"error": "budget exhausted"}


def test_submit_load_and_stale_after_rule_edit(case, rule):
    run = _run(case, rule)
    art = skeptic.submit(run, {"verdict": "challenge", "summary": "s", "warnings": [
        {"id": "w1", "severity": "high", "kind": "adjacent_clause", "claim": "801.0 vs 267.0",
         "evidence": [{"doc_id": FF_DOC, "page": 8,
                       "quote": "in excess of 182 hours In a 24-day work period"}],
         "counter_example": {"call_n": 3, "baseline_call_n": 2, "why": "w"}}]})
    assert art["schema"] == "skeptic.v1" and art["provenance"]["producer"] == "test"
    assert art["inputs"]["rule_sha256"] == skeptic.rule_sha256(rule)
    loaded = skeptic.load_review(case, FF_RULE)
    assert loaded["fresh"] is True and loaded["stale_reasons"] == []
    assert skeptic.verify(case, FF_RULE) == []
    path = case.path("rules")
    data = json.load(open(path))
    for r in data["rules"]:
        if r["id"] == FF_RULE:
            r["compute"] = "effective_base * 1.6 * hours"
    json.dump(data, open(path, "w"))
    loaded = skeptic.load_review(case, FF_RULE)
    assert loaded["fresh"] is False and "rule changed" in loaded["stale_reasons"]
    problems = skeptic.verify(case, FF_RULE)
    assert "rule changed" in problems and any("replays to" in p for p in problems)
    # the ledger has the run's events, none with a query_id
    evs = [e for e in case.ledger().read() if e["type"].startswith("skeptic.")]
    types = [e["type"] for e in evs]
    assert "skeptic.start" in types and "skeptic.finish" in types
    assert types.count("skeptic.tool_call") == 3 and "skeptic.warning" in types
    assert all(e["query_id"] is None for e in evs)
    assert case.ledger().verify()[0] is True


def test_endpoints_serve_the_review_and_import_once(case, rule, monkeypatch):
    from core import app as core_app
    from fastapi.testclient import TestClient
    monkeypatch.setattr(core_app, "CASE_DIR", case.dir)
    # a "baked" artifact: produced by a run this instance's ledger does not hold
    run = _run(case, rule)
    skeptic.submit(run, {"verdict": "no_objection", "summary": "s", "warnings": []})
    os.remove(case.path("ledger"))
    c = TestClient(core_app.app)
    before = c.get("/admin/history").json()
    assert c.get("/admin/skeptic").json()["reviews"][FF_RULE]["fresh"] is True
    r1 = c.get(f"/admin/skeptic/{FF_RULE}")
    r2 = c.get(f"/admin/skeptic/{FF_RULE}")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["provenance"]["producer"] == "test"
    evs = c.get("/admin/ledger").json()["events"]
    imported = [e for e in evs if e["type"] == "skeptic.imported"]
    assert len(imported) == 1 and imported[0]["query_id"] is None
    assert c.get("/admin/history").json() == before, "no blank rows in Recent questions"
    assert c.get("/admin/ledger").json()["verified"] is True
    missing = c.get("/admin/skeptic/chief_officers_mou:bereavement_hours")
    assert missing.status_code == 404 and "scripts/skeptic.py" in missing.json()["bake"]
    trail = c.get(f"/admin/skeptic/trail?run_id={run.run_id}").json()
    assert trail["events"] == []   # that run's events were in the baker's ledger, not ours


def test_skeptic_never_imports_the_model_layer():
    code = ("import sys; import core.skeptic; "
            "print('core.llm' in sys.modules, 'anthropic' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, env={**os.environ, "ANTHROPIC_API_KEY": ""})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False"
