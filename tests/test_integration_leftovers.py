"""Wave-2 integration leftovers (DEMO_TICKETS.md A2 step 6, F2 back-fill, J1a approver,
skeptic re-pin): the shipped rule library is bound to the text it cites, every shipped
approver is an honest label, the approval record and the Rule library card carry it,
and the two baked skeptic reviews can be re-verified at a code revision with no model
call and no new content. No key, tmp copies only."""
import copy
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import core.app as core_app  # noqa: E402
from core import approvals, evidence, skeptic  # noqa: E402
from core.caseio import load_case  # noqa: E402
from core.catalog import Catalog  # noqa: E402

CASE = os.path.join(ROOT, "cases", "santacruz")
RULES = os.path.join(CASE, "rules", "rules_ratified.json")
LONGEVITY = "firefighters_local3535_mou:longevity_10yr"
FF_RULE = "firefighters_local3535_mou:overtime_premium_rate"
AD_RULE = "admin_group_mou:overtime_premium_rate"
FF_DOC = "firefighters_local3535_mou"
FF = "Firefighter/Paramedic (56 hr, top step)"
CLI = os.path.join(ROOT, "scripts", "skeptic.py")


def _shipped_rules() -> list[dict]:
    with open(RULES) as f:
        return json.load(f)["rules"]


@pytest.fixture
def case_dir(tmp_path, monkeypatch):
    dst = tmp_path / "santacruz"
    shutil.copytree(CASE, dst)
    for runtime in ("ledger.jsonl", "snapshots"):   # a laptop's own records, not the case
        p = dst / runtime
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    monkeypatch.setattr(core_app, "CASE_DIR", str(dst))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("KENNY_LEDGER_KEY", raising=False)
    return str(dst)


def _env():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["HF_HUB_OFFLINE"] = "1"
    return env


def _cli(case_dir, *args):
    return subprocess.run([sys.executable, CLI, "--case", case_dir, *args],
                          capture_output=True, text=True, env=_env(), cwd=ROOT)


# --------------------------------------------------------------------------- #
# A2 step 6: the SHIPPED library is bound to the clause text it cites
# --------------------------------------------------------------------------- #
def test_every_shipped_rule_is_bound_to_its_cited_clause_text():
    cat = Catalog(load_case(CASE).path("catalog", "catalog.json"))
    for r in _shipped_rules():
        cit = r["citation"]
        assert cit.get("quote_sha256") and cit.get("quote"), r["id"]
        found, how = evidence.locate(cat.clauses(cit["doc_id"]), cit)
        assert found is not None, (r["id"], how)
        assert evidence.text_sha(found["text"]) == cit["quote_sha256"], r["id"]
        assert cit["quote"] == evidence.quote_of(found), r["id"]


def test_quote_backfill_is_a_no_op_on_the_shipped_library(case_dir):
    import backfill_quote_sha
    path = os.path.join(case_dir, "rules", "rules_ratified.json")
    before = open(path, "rb").read()
    assert backfill_quote_sha.run(case_dir) == 0
    assert open(path, "rb").read() == before           # byte-identical


def test_skeptic_rule_hash_ignores_the_quote_binding():
    """The two baked reviews were produced before the library was bound to its text;
    binding it must not stale them, and only the citation's ADDRESS is hashed."""
    rule = next(r for r in _shipped_rules() if r["id"] == FF_RULE)
    bare = copy.deepcopy(rule)
    bare["citation"].pop("quote_sha256")
    bare["citation"].pop("quote")
    assert skeptic.rule_sha256(bare) == skeptic.rule_sha256(rule)
    moved = copy.deepcopy(rule)
    moved["citation"]["page"] = 9
    assert skeptic.rule_sha256(moved) != skeptic.rule_sha256(rule)
    for rid in (FF_RULE, AD_RULE):
        assert skeptic.verify(load_case(CASE), rid) == [], rid


# --------------------------------------------------------------------------- #
# J1a / F2: every shipped approver is honest, and the record carries it everywhere
# --------------------------------------------------------------------------- #
def test_no_shipped_approver_is_an_agent_passed_off_as_a_person():
    for r in _shipped_rules():
        ap = r["approver"]
        assert ap and "claude" not in ap.lower() and "agent" not in ap.lower(), r["id"]
    lon = next(r for r in _shipped_rules() if r["id"] == LONGEVITY)
    assert lon["approver"].startswith("analyst (hand-authored 2026-10-07")
    assert "p.12" in lon["approver"] and "p.38" in lon["approver"]


def test_approvals_backfill_only_adds_and_carries_the_longevity_approver(case_dir):
    """The F2 back-fill adds ledger events and nothing else: the rule file is byte-
    identical, every live rule gets one record, and the longevity record names the
    analyst label the rule file carries. A second run adds nothing."""
    path = os.path.join(case_dir, "rules", "rules_ratified.json")
    before = open(path, "rb").read()
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "backfill_provenance.py"),
                          case_dir, "--approvals"], capture_output=True, text=True, env=_env())
    assert out.returncode == 0, out.stdout + out.stderr
    assert open(path, "rb").read() == before
    led = load_case(case_dir).ledger()
    latest = approvals.latest_approvals(led)
    assert set(latest) == {r["id"] for r in _shipped_rules()}
    pl = latest[LONGEVITY]["payload"]
    assert pl["approver"].startswith("analyst (hand-authored 2026-10-07")
    assert pl["backfilled"] is True and pl["approved_at"] == "2026-10-07T00:00:00Z"
    assert pl["citation"]["page"] == 12 and pl["known_answers"][0]["expected"] == 656.82
    n = len(led.read())
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "backfill_provenance.py"),
                          case_dir, "--approvals"], capture_output=True, text=True, env=_env())
    assert out.returncode == 0 and "appended" not in out.stdout.split("ledger verify")[0]
    assert len(load_case(case_dir).ledger().read()) == n
    assert open(path, "rb").read() == before


def test_rule_library_card_reads_the_longevity_approver(case_dir):
    """The read side the card renders from: /admin/approvals names the analyst for the
    longevity rule and the shipped library matches its own approval."""
    from fastapi.testclient import TestClient
    approvals.backfill(load_case(case_dir))
    rows = {r["rule_id"]: r for r in TestClient(core_app.app).get("/admin/approvals").json()["approvals"]}
    row = rows[LONGEVITY]
    assert row["matches_live"] is True
    assert row["approval"]["approver"].startswith("analyst (hand-authored 2026-10-07")
    assert row["approval"]["backfilled"] is True
    # the card template renders exactly these fields
    html = open(os.path.join(ROOT, "core", "templates", "admin.html")).read()
    assert "Approved by <strong>${esc(ap.approver)}</strong>" in html


# --------------------------------------------------------------------------- #
# skeptic re-pin: verify, then stamp the code rev; no model call, no new content
# --------------------------------------------------------------------------- #
def _scripted_review(case_dir, tmp_path):
    shutil.rmtree(os.path.join(case_dir, "reviews"), ignore_errors=True)
    out = _cli(case_dir, "start", "--rule", FF_RULE, "--producer", "test-session")
    assert out.returncode == 0, out.stderr
    run_id = json.loads(out.stdout)["run_id"]
    out = _cli(case_dir, "engine", "--run", run_id, "--subjects", FF, "--hours", "8")
    assert json.loads(out.stdout)["total"] == 640.8
    f = tmp_path / "review.json"
    f.write_text(json.dumps({"verdict": "no_objection", "summary": "8 h -> 640.8",
                             "warnings": []}))
    out = _cli(case_dir, "submit", "--run", run_id, "--file", str(f))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["artifact"]


def test_repin_stamps_reverified_and_changes_nothing_else(case_dir, tmp_path):
    path = _scripted_review(case_dir, tmp_path)
    before = json.load(open(path))
    out = _cli(case_dir, "repin", "--rule", FF_RULE)
    assert out.returncode == 0, out.stdout + out.stderr
    res = json.loads(out.stdout)
    assert res["ok"] is True and res["stamped"] is True and res["problems"] == []
    after = json.load(open(path))
    rv = after["provenance"].pop("reverified")
    assert rv["code_rev"] and rv["problems"] == [] and rv["at"].endswith("Z")
    assert after == before                                   # nothing else changed
    assert after["provenance"]["code_rev"] == before["provenance"]["code_rev"]
    assert skeptic.verify(load_case(case_dir), FF_RULE) == []
    # the endpoint serves the stamp
    from fastapi.testclient import TestClient
    art = TestClient(core_app.app).get(f"/admin/skeptic/{FF_RULE}").json()
    assert art["provenance"]["reverified"]["code_rev"] == rv["code_rev"]


def test_repin_refuses_a_review_that_does_not_verify(case_dir, tmp_path):
    path = _scripted_review(case_dir, tmp_path)
    rules_path = os.path.join(case_dir, "rules", "rules_ratified.json")
    data = json.load(open(rules_path))
    for r in data["rules"]:
        if r["id"] == FF_RULE:
            r["compute"] = "effective_base * 2 * hours"
    json.dump(data, open(rules_path, "w"))
    before = open(path, "rb").read()
    out = _cli(case_dir, "repin", "--all")
    assert out.returncode == 1
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["ok"] is False and res["stamped"] is False and "rule changed" in res["problems"]
    assert open(path, "rb").read() == before                 # artifact untouched


def test_shipped_reviews_are_reverified_at_a_clean_code_rev():
    """The two baked reviews ship with a re-verification stamp whose code rev is a
    clean commit (no -dirty suffix), recorded after `verify` reproduced every quote
    and engine replay. The produced code_rev is kept as it was."""
    case = load_case(CASE)
    for rid in (FF_RULE, AD_RULE):
        prov = skeptic.load_review(case, rid)["provenance"]
        rv = prov.get("reverified")
        assert rv, f"{rid}: not re-verified (run scripts/skeptic.py repin --all)"
        assert rv["problems"] == [] and not rv["code_rev"].endswith("-dirty"), (rid, rv)
        assert prov["code_rev"] == "d84d5f3-dirty"       # produced where it was produced
