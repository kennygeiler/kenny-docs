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
from core.services import state as core_state  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cases", "santacruz")
    case = tmp_path / "santacruz"
    shutil.copytree(src, case)
    monkeypatch.setattr(core_state, "CASE_DIR", str(case))
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
    assert "app.js?v=8" in html and "styles.css?v=14" in html


def test_admin_page_loads_its_script_from_a_pinned_file(client):
    """The admin console's script is a served file, not an inline block."""
    html = client.get("/admin").text
    assert '<script src="/static/admin.js?v=4"></script>' in html
    assert "<script>" not in html, "admin.html must carry no inline script"
    res = client.get("/static/admin.js")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/javascript")
    assert res.headers["cache-control"] == "no-store, max-age=0"
    assert "const TABS = [" in res.text


# ------------------------------------------------- audit/ui: number + sum + one hint --
def test_single_answer_shows_the_sum_and_one_hint(client):
    """One classification: the headline figure, the arithmetic right under it, one hint
    — no one-row table repeating the figure and no thrice-repeated 'click to see'."""
    js = client.get("/static/app.js").text
    assert "function sumLine(li)" in js and "t.kind === 'math' && t.line" in js
    assert 'class="sum-line"' in js
    assert "if (items.length > 1 || uncovered.length)" in js, "one row needs no table"
    for gone in ("click the figure to see how it was calculated",
                 "select an amount to see how it was calculated",
                 "Click any amount to open its audit trail."):
        assert gone not in js, f"duplicate hint still rendered: {gone!r}"
    assert js.count("Click the figure to see the clause it came from.") == 1
    assert js.count("Click any amount to see its sum and the clause it came from.") == 1
    css = client.get("/static/styles.css").text
    assert ".sum-line" in css


def test_the_engine_answer_carries_the_line_the_card_shows(client):
    r = client.post("/chat", json={"prompt": "Cost an 8-hour overtime shift for a "
                                             "Firefighter/Paramedic (56 hr, top step)"}).json()
    items = r["result"]["line_items"]
    assert len(items) == 1
    math = [t for t in items[0]["trace"] if t["kind"] == "math" and t.get("line")]
    assert math and math[0]["line"] == "$53.40/hr × 1.5 × 8 h = $640.80"


def test_chat_esc_covers_both_quotes(client):
    js = client.get("/static/app.js").text
    assert """/[&<>"']/g""" in js and "'&#39;'" in js


def test_tree_marks_reached_but_unapplied_rules(client):
    tree = client.get("/static/tree.js").text
    assert "function markNotApplied(" in tree and "'not applied: '" in tree
    js = client.get("/static/app.js").text
    assert "renderTree(tree, host, onRef, questionFacts(res))" in js
    css = client.get("/static/styles.css").text
    assert ".node.skipped" in css


# ------------------------------------------------------------- audit/ui: admin page --
def _admin(client):
    return (client.get("/admin").text + client.get("/static/admin.js").text)


def test_admin_builds_no_handler_from_a_string(client):
    """esc() output used to land inside single-quoted onclick strings; a quote in a
    document id or scenario name broke out into script. Dynamic buttons now carry
    data-* attributes and one delegated listener reads them."""
    html = _admin(client)
    assert re.search(r"""onclick="[^"]*\$\{""", html) is None, "an inline handler built from data"
    assert "JSON.stringify(g.name)" not in html
    assert """/[&<>"']/g""" in html and "'&#39;'" in html
    for act in ("xray", "compare", "source", "break", "draft", "reread"):
        assert f'data-act="{act}"' in html, act
    assert "e.target.closest('[data-act]')" in html
    tour = client.get("/static/tour.js").text
    assert 'onclick^=' not in tour and 'button[data-act="${act}"]' in tour


def test_admin_opens_on_verification_without_numbered_tabs(client):
    html = _admin(client)
    for n in ("1 · Documents", "2 · Verification", "3 · Review queue", "start here"):
        assert n not in html
    assert re.search(r'id="tab-verify"[^>]*aria-selected="true"', html)
    assert re.search(r'id="tab-docs"[^>]*aria-selected="false"', html)
    assert re.search(r'id="panel-verify"[^>]*tabindex="0">', html)
    assert re.search(r'id="panel-docs"[^>]*hidden>', html)
    assert "const DEFAULT_TAB = 'verify'" in html
    assert "showTab(DEFAULT_TAB, false, false)" in html, "no hash: default tab, no hash written"
    tour = client.get("/static/tour.js").text
    assert "q('#tab-docs')" in tour, "the tour starts on Documents, so it must open that tab"
    tabs = re.search(r"\n\.tabs \{([^}]*)\}", client.get("/static/styles.css").text).group(1)
    assert "flex-wrap: nowrap" in tabs and "overflow-x: auto" in tabs, "one scrollable row on a phone"


def test_verification_copy_and_amounts(client):
    html = _admin(client)
    assert "four shipped" not in html
    assert "function fmtVal(v, type)" in html
    assert "fmtVal(g.expected, g.result_type)" in html and "fmtVal(g.actual, g.result_type)" in html


def test_reread_is_demoted_and_confirm_says_what_it_does(client):
    html = _admin(client)
    toolbar = html[html.index('<label class="file-label" for="pdfFile">'):]
    toolbar = toolbar[:toolbar.index("</div>")]
    assert "Re-read" not in toolbar, "Re-read sits apart from Upload"
    assert 'class="ghost small" id="rereadBtn" data-act="reread"' in html
    first = html[html.index("function ingest()"):html.index("function ingestConfirmed()")]
    assert "backfill_quote_sha.py" in first and "rule library goes empty" in first
    assert "window.confirm" not in html and "confirm(" not in first


def test_rate_in_the_sum_line_is_a_citation_chip(client):
    """M1.4/I14: the chat's arithmetic line renders the rate as a button that opens its
    salary-schedule row (the citation's row box), not plain text."""
    js = client.get("/static/app.js").text
    assert "className = 'rate-cite'" in js and "o.source.citation" in js
    assert "b.onclick = () => openOperand(rate, res)" in js
    assert "(c && c.bbox) || s.context_bbox" in js
