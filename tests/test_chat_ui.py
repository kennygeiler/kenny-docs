"""Chat-surface tripwires (DEMO_TICKETS.md I1, I2, I3, I4). The surface is frontend-only,
so these assert on the served assets: the behaviours a browser pass verified by hand
are pinned by the strings that implement them, and a regression that removes one is
caught here before the demo does."""
import os
import re
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
    return TestClient(core_app.app)


def _rule_block(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list contains `selector`."""
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [s.strip() for s in m.group(1).split(",")]:
            return m.group(2)
    raise AssertionError(f"no CSS rule for {selector!r}")


# ---------------------------------------------------------------- I1 lifecycle --
def test_chat_request_lifecycle_is_guarded(client):
    js = client.get("/static/app.js").text
    assert "AbortController" in js, "a request must be able to time out"
    assert "r.ok" in js, "a non-2xx must become an error bubble, not a parse crash"
    assert "msg bot pending" in js and "Working on it" in js
    assert "Retry" in js and "Edit question" in js, "the question is never lost"
    assert "if (IN_FLIGHT) return;" in js, "double submits send one request"
    assert "window.send = send" in js, "tour.js drives the page through window.send"
    css = client.get("/static/styles.css").text
    assert ".msg.pending" in css


# ---------------------------------------------------------------- I2 phone card --
def test_answer_card_is_phone_proof(client):
    css = client.get("/static/styles.css").text
    assert "overflow-wrap" in _rule_block(css, "table.lines td"), \
        "an unbreakable rule id must wrap instead of widening the page"
    amount = _rule_block(css, ".amount")
    assert "min-height: 44px" in amount and "min-width: 44px" in amount, \
        "the amount is the only entry to the drawer: it must be tappable"
    assert "overflow-wrap" in _rule_block(css, ".evt")
    assert re.search(r"#prompt\s*\{[^}]*font-size:\s*16px", css), "iOS zooms inputs under 16px"
    assert ".lines-wrap" in css
    js = client.get("/static/app.js").text
    assert "lines-wrap" in js
    assert "amount total-btn" in js, "the headline figure opens the same drawer"
    assert 'class="total"' in js, "tour.js waits for #log .msg.bot .total"


# ---------------------------------------------------------------- I3 AI panel --
def test_ai_panel_caveat_follows_what_ran(client):
    js = client.get("/static/app.js").text
    assert "No model wrote this" in js
    assert "CAVEAT[mode] || CAVEAT.costing" not in js, "caveat keyed by mode alone contradicts the headline"
    assert "function caveat(mode, ai, answerSource)" in js
    assert "res.answer_source)" in js, "renderPolicy must pass answer_source through"
    assert "function collapseCalls" in js, "a department follow-up lists classify_intent twice"
    for label in ("Decide what kind of question this is",
                  "Read who, hours and date from the question",
                  "Write the answer", "Pick the governing document"):
        assert label in js


# ---------------------------------------------------------------- I4 buyer copy --
def test_proof_surfaces_speak_contract_language(client):
    js = client.get("/static/app.js").text
    assert "Rule applied" in js and "Arithmetic" in js and "Technical details" in js
    for banned in ("parsed via", "relevance ${", "(score "):
        assert banned not in js, f"internal wording still rendered: {banned!r}"
    assert "function cite(" in js and "function docTitle(" in js
    # the trailing math figure is formatted as money
    assert "fmtVal(n, li ? li.result_type" in js
    # a bare section sign can no longer be rendered from an empty clause label
    assert "§${esc(s.clause)}" not in js and "§${esc(c.clause)}" not in js


def test_api_case_exposes_declared_titles(client):
    c = client.get("/api/case").json()
    by_id = {s["doc_id"]: s["title"] for s in c["sources"]}
    assert by_id["firefighters_local3535_mou"] == \
        "Firefighters Local 3535 MOU 2025–2028 (with Salary Schedule)"
    assert by_id["management_mou"] == "Management MOU 2024–2026 (with Side Letter)"


def test_chat_page_pins_fresh_assets(client):
    html = client.get("/").text
    assert "app.js?v=6" in html and "styles.css?v=7" in html
