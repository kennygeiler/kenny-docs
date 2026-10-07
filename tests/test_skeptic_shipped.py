"""G6: the two baked skeptic reviews that ship with the Santa Cruz case reproduce —
every quote is on its page and every engine total replays — and they say who made them."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import skeptic  # noqa: E402
from core.caseio import load_case  # noqa: E402

CASE = os.path.join(ROOT, "cases", "santacruz")
FF_RULE = "firefighters_local3535_mou:overtime_premium_rate"
AD_RULE = "admin_group_mou:overtime_premium_rate"


def test_both_overtime_rules_have_a_review_that_verifies():
    case = load_case(CASE)
    reviews = skeptic.list_reviews(case)
    assert FF_RULE in reviews and AD_RULE in reviews
    for rid in (FF_RULE, AD_RULE):
        assert skeptic.verify(case, rid) == [], rid
        art = skeptic.load_review(case, rid)
        assert art["fresh"] is True
        prov = art["provenance"]
        assert prov["producer"].startswith("Claude Code session")
        assert prov["api_key_in_env"] is False and prov["billing"] == "subscription"
        assert prov["mode"] == "precomputed"


def test_firefighters_review_found_the_threshold_and_the_wider_regular_rate():
    art = skeptic.load_review(load_case(CASE), FF_RULE)
    quotes = [(w["severity"], e["quote"]) for w in art["warnings"] for e in w["evidence"]]
    assert any(sev == "high" and "182 hours" in q for sev, q in quotes)
    assert any("Education Incentive" in q for sev, q in quotes)
    totals = {w["id"]: (w["counter_example"] or {}).get("engine_total") for w in art["warnings"]}
    assert 267.0 in totals.values(), "the half-time counter-example"


def test_every_counter_example_total_replays_through_the_engine():
    case = load_case(CASE)
    for rid in (FF_RULE, AD_RULE):
        art = skeptic.load_review(case, rid)
        rule = skeptic.find_rule(case, rid)
        for w in art["warnings"]:
            ce = w.get("counter_example")
            if not ce:
                continue
            got = skeptic.run_engine(case, rule, ce["rule_set"], ce["scenario"], ce.get("variant"))
            assert got["total"] == ce["engine_total"], (rid, w["id"])


def test_verify_all_cli_exits_zero():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["HF_HUB_OFFLINE"] = "1"
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "skeptic.py"),
                          "--case", CASE, "verify", "--all"],
                         capture_output=True, text=True, env=env, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.count('"ok":true') == 2
