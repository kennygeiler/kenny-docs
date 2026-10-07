"""Guided demo tour: one shared step engine (tour.js) served like app.js, included by
BOTH surfaces. The tour is frontend-only, so the meaningful server assertions are that
the pieces actually ship: the route serves, chat carries the start button, admin carries
the ?tour=1 resume hook (the script include), and the script defines both step lists.
A missing include or a stale cache-buster is exactly the failure that would make the
"Take the tour" promise in the README false."""
import os
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


def test_tour_js_route_serves(client):
    res = client.get("/static/tour.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    assert "startTour" in res.text


def test_chat_page_ships_the_tour(client):
    html = client.get("/").text
    assert '/static/tour.js?v=' in html, "chat must load the tour engine"
    assert 'id="tourStart"' in html and "Take the tour" in html
    assert 'data-page="chat"' in html, "the engine keys its step list off this"
    assert "styles.css?v=7" in html, "stale cached CSS would ship no coach-mark styles"


def test_admin_page_ships_the_tour_resume_hook(client):
    html = client.get("/admin").text
    assert '/static/tour.js?v=' in html, "admin must load the engine so ?tour=1 resumes"
    assert 'data-page="admin"' in html
    assert "styles.css?v=12" in html


def test_no_foreign_legal_pages(client):
    """DEMO_TICKETS I8: the privacy/terms pages described a different product (a
    call-screening SMS service). They live in that project now; this app must not
    serve them, and nothing in core/ or tests/ may reference them."""
    assert client.get("/privacy").status_code == 404
    assert client.get("/terms").status_code == 404
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("core", "tests"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, sub)):
            for f in files:
                if not f.endswith((".py", ".js", ".html", ".css", ".txt", ".yaml")):
                    continue
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    text = fh.read().lower()
                for needle in ("twilio", "a2p", "/privacy", "/terms"):
                    # this test is the one legitimate mention
                    if f == "test_tour.py":
                        continue
                    assert needle not in text, f"{sub}/{f} still mentions {needle!r}"


def test_tour_js_defines_both_step_lists_and_the_handoff(client):
    js = client.get("/static/tour.js").text
    assert "chat: [" in js and "admin: [" in js, "per-page step lists"
    assert "/admin?tour=1" in js, "chat hands off to the admin leg"
    assert "tour=1" in js and "startTour" in js, "admin resume hook"
    css = client.get("/static/styles.css").text
    assert ".tour-card" in css and ".tour-glow" in css and ".tour-start" in css


def test_tour_no_longer_claims_a_scan_would_say_recovered_layout(client):
    """D1: a scan with an OCR layer says "OCR'd scan", not "recovered layout" or
    "page-level" — the tour's tier-chip step must describe the chip that exists."""
    js = client.get("/static/tour.js").text
    assert "a scanned document would say" not in js
    assert "OCR\\'d scan" in js or "OCR'd scan" in js


# ---------------- tour-and-gate-demo (I5 / I15 / D4 step / E3 step) ----------------
import json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_ID = "firefighters_local3535_mou:overtime_double_time_draft"
LIVE_OT = "firefighters_local3535_mou:overtime_premium_rate"
Q640 = "Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step)"


def _case_dir():
    return core_app.CASE_DIR


def _queue(case_dir):
    p = os.path.join(case_dir, "rules", "rules_proposed.json")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return json.load(f).get("rules", [])


def _ledger(case_dir):
    p = os.path.join(case_dir, "ledger.jsonl")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def test_tour_copy_is_hardened_against_i5_findings(client):
    """I5: no blame on 'the deterministic fallback' for slowness, no claim about a digital
    text layer on scanned contracts, the measured policy question, ?tour=1 honoured on
    chat, Skip-safe actions (ctx.alive), avoid rules and a keep-clear placement."""
    js = client.get("/static/tour.js").text
    for stale in ("deterministic fallback can be slower", "digital text layer",
                  "a scanned document would say", "grey hash is the SHA-256"):
        assert stale not in js, stale
    assert "How is premium overtime compensated under the Firefighters Local 3535 MOU?" in js
    assert "the same clause the engine boxed a moment ago" in js
    assert "ctx.alive()" in js, "every action re-checks the sequence after each await"
    assert "avoid: '#drawer .cite img'" in js, "the card keeps clear of the boxed clause"
    assert "avoid: '#xrayStage'" in js
    assert "offsetParent === null" in js, "a 0x0 / detached target counts as missing"
    assert "'tour') === '1'" in js and "PAGE === 'admin' &&" not in js, "?tour=1 on chat too"
    assert "start.focus(" in js, "ending the tour returns focus to the start button"
    assert "not taken from payroll" in js
    # wave-1 chat UI: example buttons, the Read as line, the OCR'd scan chip
    assert "button.example" in js and ".read-as" in js and ".tier-chip" in js


def test_tour_has_the_showcase_try_break_and_gate_steps(client):
    """D4: the Compare step opens the showcase (p.22) and names 10.15. E3: a step presses
    'Try to break it' on the $640.80 rule. I15: steps load the demo draft, view its
    source, approve only that id, and land the Audit step on the refusal."""
    js = client.get("/static/tour.js").text
    assert "10.15" in js and "p.22" in js and ".btn-showcase" in js
    assert "td.xcell-disputed" in js, "the Compare step narrates the amber cells on screen"
    assert f"LIVE_OT_ID = '{LIVE_OT}'" in js and "Try to break it" in js
    assert ".break-result" in js
    assert f"DEMO_ID = '{DEMO_ID}'" in js
    assert "loadDemoDraft" in js and "approve selected" in js.lower()
    assert "checked[0].dataset.id !== DEMO_ID" in js, "only the seeded draft is ever approved"
    assert "#reviewMsg .flagline" in js
    assert "authoring.blocked" in js


def test_admin_ships_the_load_demo_draft_button(client):
    html = client.get("/admin").text
    assert 'id="loadDemoDraft"' in html and "Load the demo draft" in html
    assert "async function loadDemoDraft(" in html and "window.loadDemoDraft" in html
    assert "/admin/demo_draft/load" in html
    assert "tour.js?v=3" in html, "the rewritten engine must not be served from cache"
    assert "tour.js?v=3" in client.get("/").text


def test_demo_draft_file_is_wrong_on_purpose_and_cites_the_live_clause():
    with open(os.path.join(ROOT, "cases", "santacruz", "rules", "demo_wrong_draft.json")) as f:
        d = json.load(f)
    with open(os.path.join(ROOT, "cases", "santacruz", "rules", "rules_ratified.json")) as f:
        live = {r["id"]: r for r in json.load(f)["rules"]}
    [r] = d["rules"]
    assert r["id"] == DEMO_ID and r["_demo"] is True
    assert r["compute"] == "effective_base * 2 * hours"
    assert r["citation"] == live[LIVE_OT]["citation"], "same p.8 clause as the live rule"
    assert r["priority"] > live[LIVE_OT].get("priority", 0), "must win base selection"
    assert DEMO_ID not in live, "the wrong draft must never be live"


def test_load_demo_draft_is_idempotent_and_ledgered(client):
    case = _case_dir()
    # The queue file is gitignored, so a laptop that ran the demo may carry one into
    # the fixture copy; start from an empty queue either way.
    stray = os.path.join(case, "rules", "rules_proposed.json")
    if os.path.exists(stray):
        os.remove(stray)
    assert client.get("/admin/demo_draft").json()["loaded"] == []
    r1 = client.post("/admin/demo_draft/load").json()
    assert r1["added"] == [DEMO_ID] and r1["already_loaded"] is False
    r2 = client.post("/admin/demo_draft/load").json()
    assert r2["added"] == [] and r2["already_loaded"] is True
    ids = [r["id"] for r in _queue(case)]
    assert ids.count(DEMO_ID) == 1, ids
    st = client.get("/admin/demo_draft").json()
    assert st["loaded"] == [DEMO_ID] and st["live"] == []
    # the queue renders it (admin UI reads this), and it is well-formed
    assert DEMO_ID in [r["id"] for r in client.get("/admin/proposed").json()["rules"]]
    assert DEMO_ID not in client.get("/admin/validate").json().get("errors", {})
    loads = [e for e in _ledger(case) if e["type"] == "authoring.demo_draft_loaded"]
    assert len(loads) == 1 and loads[0]["payload"]["rule_ids"] == [DEMO_ID]


def test_load_keeps_other_queued_drafts(client):
    case = _case_dir()
    other = {"id": "management_mou:tk_other", "kind": "selector", "role": "base",
             "result_type": "currency", "topic": "x", "when": "True", "compute": "1",
             "citation": {"doc_id": "management_mou", "clause": "Flex time (p.6)",
                          "page": 6, "bbox": [82.4, 212.5, 538.9, 162.4]}}
    os.makedirs(os.path.join(case, "rules"), exist_ok=True)
    with open(os.path.join(case, "rules", "rules_proposed.json"), "w") as f:
        json.dump({"rules": [other], "needs_data": [{"field": "x"}]}, f)
    client.post("/admin/demo_draft/load")
    with open(os.path.join(case, "rules", "rules_proposed.json")) as f:
        data = json.load(f)
    assert [r["id"] for r in data["rules"]] == ["management_mou:tk_other", DEMO_ID]
    assert data["needs_data"] == [{"field": "x"}]


def test_gate_refuses_the_demo_draft_and_ledgers_the_block(client):
    """The demo beat: approve the well-formed, clause-cited, wrong draft with an approver
    name -> refused, $854.40 is not $640.80, library bytes unchanged, authoring.blocked
    on the ledger, chat still answers 640.80."""
    case = _case_dir()
    lib_path = os.path.join(case, "rules", "rules_ratified.json")
    with open(lib_path, "rb") as f:
        before = f.read()
    client.post("/admin/demo_draft/load")
    res = client.post("/admin/ratify",
                      json={"rule_ids": [DEMO_ID], "approver": "Tour visitor"}).json()
    assert res["ratified"] == []
    g = res["golden_failed"]
    assert g["expected"] == 640.8 and g["actual"] == 854.4, g
    assert "Nothing was approved" in res["warning"] and "854.4" in res["warning"]
    with open(lib_path, "rb") as f:
        assert f.read() == before, "the library file is byte-identical"
    blocked = [e for e in _ledger(case) if e["type"] == "authoring.blocked"]
    assert blocked and blocked[-1]["payload"]["approver"] == "Tour visitor"
    assert blocked[-1]["payload"]["reason"] == "known answer fails"
    assert client.get("/admin/demo_draft").json()["live"] == []
    # approving without a name is refused before the gate even runs
    res2 = client.post("/admin/ratify", json={"rule_ids": [DEMO_ID], "approver": ""}).json()
    assert res2["ratified"] == [] and "approver" in res2["warning"]
    r = client.post("/chat", json={"prompt": Q640}).json()
    assert r["mode"] == "costing" and r["result"]["total"] == 640.8


def test_try_break_on_the_live_overtime_rule_still_works_with_the_draft_queued(client):
    """The E3 tour step targets the live rule by id; the queued demo draft (same clause,
    different id) must not shadow it."""
    client.post("/admin/demo_draft/load")
    out = client.post("/admin/try_break", json={"rule_id": LIVE_OT}).json()
    assert out["rule_id"] == LIVE_OT and out["total"] == 7 and out["caught"] >= 1
    assert out["verdict"] in ("tested", "partly tested")


def test_showcase_deep_link_lands_on_p22_with_disputed_cells(client):
    """D4 tour step: the showcase the step opens is p.22 of the firefighters MOU, and the
    page it lands on carries the amber cells the step narrates (10.15 stored as '0) £5',
    468 as 'A468')."""
    sc = client.get("/admin/coverage").json()["showcase"]
    assert sc == {"doc": "firefighters_local3535_mou", "page": 22, "label": sc["label"]}
    page = client.get("/doc/firefighters_local3535_mou/clauses?page=22").json()
    stored = {c["stored"]: c.get("reread") for cl in page["clauses"]
              for c in (cl.get("cells") or []) if c.get("status") == "disputed"}
    assert stored.get("0) £5") == "10.15" and stored.get("A468") == "468", stored
