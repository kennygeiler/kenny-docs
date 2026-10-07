"""G6: the zero-spend bake path. The CLI is driven as a subprocess on a temp copy of
the case; no model, no key."""
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

CLI = os.path.join(ROOT, "scripts", "skeptic.py")
FF_RULE = "firefighters_local3535_mou:overtime_premium_rate"
FF_DOC = "firefighters_local3535_mou"
FF = "Firefighter/Paramedic (56 hr, top step)"


@pytest.fixture
def case_dir(tmp_path):
    dst = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), dst)
    shutil.rmtree(dst / "reviews", ignore_errors=True)
    for runtime in ("ledger.jsonl", "snapshots"):   # a laptop's own records, not the case
        p = dst / runtime
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    return str(dst)


def _cli(case_dir, *args, key=None):
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["HF_HUB_OFFLINE"] = "1"
    if key is not None:
        env["ANTHROPIC_API_KEY"] = key
    return subprocess.run([sys.executable, CLI, "--case", case_dir, *args],
                          capture_output=True, text=True, env=env, cwd=ROOT)


def test_start_refuses_when_a_key_is_in_the_environment(case_dir):
    out = _cli(case_dir, "start", "--rule", FF_RULE, "--producer", "test", key="sk-dummy")
    assert out.returncode == 2 and "ANTHROPIC_API_KEY" in out.stderr
    assert not os.path.exists(os.path.join(case_dir, "reviews"))
    out = _cli(case_dir, "start", "--rule", FF_RULE, "--producer", "test",
               "--allow-api-key", key="sk-dummy")
    assert out.returncode == 0, out.stderr
    run_id = json.loads(out.stdout)["run_id"]
    run = skeptic.Run.load(load_case(case_dir), run_id)
    assert run.data["api_key_in_env"] is True


def test_scripted_run_produces_a_provenanced_artifact(case_dir, tmp_path):
    out = _cli(case_dir, "start", "--rule", FF_RULE, "--producer", "claude-code-session",
               "--operator", "tester", "--model", "stub-model")
    assert out.returncode == 0, out.stderr
    start = json.loads(out.stdout)
    run_id = start["run_id"]
    assert any(c["cited"] for c in start["cited_page"]["clauses"])
    out = _cli(case_dir, "page", "--run", run_id, "--doc", FF_DOC, "--page", "7")
    assert out.returncode == 0 and json.loads(out.stdout)["page"] == 7
    out = _cli(case_dir, "engine", "--run", run_id, "--rule-set", "live",
               "--subjects", FF, "--hours", "8")
    assert json.loads(out.stdout)["total"] == 640.8
    review = {"verdict": "challenge", "summary": "8 hours pays 640.8", "warnings": [
        {"id": "w1", "severity": "high", "kind": "ignored_condition",
         "claim": "the premium applies past 182 hours in a 24-day work period",
         "evidence": [{"doc_id": FF_DOC, "page": 8,
                       "quote": "in excess of 182 hours In a 24-day work period"}],
         "counter_example": {"call_n": 3, "why": "8 hours -> 640.8 as written"},
         "needs_data": ["hours_in_work_period"]}]}
    f = tmp_path / "review.json"
    f.write_text(json.dumps(review))
    out = _cli(case_dir, "submit", "--run", run_id, "--file", str(f))
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert [w["id"] for w in res["accepted"]] == ["w1"] and res["rejected"] == []
    art = json.load(open(res["artifact"]))
    prov = art["provenance"]
    for k in ("mode", "producer", "harness", "model", "operator", "produced_at",
              "api_key_in_env", "billing", "prompt_sha256", "code_rev", "run_id",
              "tool_calls", "limits"):
        assert k in prov, k
    assert prov["mode"] == "precomputed" and prov["api_key_in_env"] is False
    assert prov["producer"] == "claude-code-session" and prov["model"] == "stub-model"
    assert prov["billing"] == "subscription" and prov["tool_calls"] == 3
    assert art["inputs"]["rule_sha256"] and art["inputs"]["doc_sha256"][FF_DOC]
    led = load_case(case_dir).ledger()
    types = [e["type"] for e in led.read() if e["type"].startswith("skeptic.")]
    assert types.count("skeptic.start") == 1 and types.count("skeptic.tool_call") == 3
    assert types.count("skeptic.finish") == 1
    assert led.verify()[0] is True
    # verify: 0 fresh, 1 after the rule's compute is edited
    assert _cli(case_dir, "verify", "--rule", FF_RULE).returncode == 0
    path = os.path.join(case_dir, "rules", "rules_ratified.json")
    data = json.load(open(path))
    for r in data["rules"]:
        if r["id"] == FF_RULE:
            r["compute"] = "effective_base * 2 * hours"
    json.dump(data, open(path, "w"))
    out = _cli(case_dir, "verify", "--all")
    assert out.returncode == 1 and "rule changed" in out.stdout


def test_submit_rejects_a_quote_from_an_unread_page(case_dir, tmp_path):
    start = json.loads(_cli(case_dir, "start", "--rule", FF_RULE, "--producer", "t").stdout)
    review = {"verdict": "challenge", "summary": "", "warnings": [
        {"id": "w1", "severity": "high", "kind": "adjacent_clause", "claim": "half-time band",
         "evidence": [{"doc_id": FF_DOC, "page": 7,
                       "quote": "the overtime premium for the hours between 182 and 192 Is at half-time"}]}]}
    f = tmp_path / "r.json"
    f.write_text(json.dumps(review))
    res = json.loads(_cli(case_dir, "submit", "--run", start["run_id"], "--file", str(f)).stdout)
    assert res["accepted"] == [] and any("never read" in r["reason"] for r in res["rejected"])
    assert res["verdict"] == "incomplete"
