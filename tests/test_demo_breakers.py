"""Demo-breaking findings from the 2026-10-07 review, one test per fix (no API key).

F2  "How much would it COST ... to work 8 HOURS" was refused as entitlement.
F3  "ten years of service" read as no years and silently costed $640.80.
F4  "pay for" in the cost cues sent "What is the callback pay for ...?" to costing.
F5  A bare heading ("D. Longevity", "Sick Leave:") was quoted as the whole answer.
F6  The Audit tab showed "$3.00" for 3 bereavement shifts.
F9  A captured figure ("300.00") in the clause slot was cited as "Per §300.00".
F10 "Fire Inspector" derived department=fire from the rank and matched nobody.
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import audit, costing, llm, rulematch  # noqa: E402
from core.caseio import load_case  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE_DIR = os.path.join(ROOT, "cases", "santacruz")
FF = "Firefighter/Paramedic (56 hr, top step)"


@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE_DIR, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    c = TestClient(core_app.app)
    c.case_dir = str(case)
    return c


def _ask(client, prompt):
    return client.post("/chat", json={"prompt": prompt}).json()


# ---- F2: an ask to compute outranks the "how much … hours" entitlement pattern ----
def test_how_much_would_it_cost_to_work_hours_is_costing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    q = f"How much would it cost for a {FF} to work 8 hours of overtime?"
    assert llm.classify_intent(q) == "costing"
    assert llm.classify_intent(
        "How much vacation does a Firefighter (56 hr, top step) accrue?") == "entitlement"
    assert llm.classify_intent("How many bereavement days does a sergeant get?") == "entitlement"


def test_how_much_would_it_cost_reaches_the_engine(client):
    res = _ask(client, f"How much would it cost for a {FF} to work 8 hours of overtime?")
    assert res["mode"] == "costing", res
    assert res["result"]["total"] == 640.80


# ---- F4: "pay for" is not a cost cue ----
def test_callback_pay_question_is_policy_not_costing(monkeypatch, client):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "pay for" not in llm._COST_CUES
    assert llm.classify_intent("What is the callback pay for firefighters?") == "policy"
    assert llm.classify_intent(f"Cost an 8-hour overtime shift for a {FF}") == "costing"
    assert llm.classify_intent("overtime pay for 8 hours for a firefighter") == "costing"
    res = _ask(client, "What is the callback pay for firefighters?")
    assert res["mode"] == "policy", res
    assert res.get("question") is None


