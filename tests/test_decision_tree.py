"""Search tree (DEMO_TICKETS.md L1, L3, C1/I13): the losers at every fork are in the
hash chain, the drawer's tree is built from those events alone, and the arithmetic line
carries real numbers with an address on each factor. No key, HF offline."""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import audit, decisions, llm, mathline  # noqa: E402
from core.caseio import load_case  # noqa: E402
from core.catalog import Catalog  # noqa: E402
from core.engine import calculate  # noqa: E402
from core.ruledsl import Rule  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE_DIR = os.path.join(ROOT, "cases", "santacruz")
NODE = shutil.which("node") or ("/opt/homebrew/bin/node" if os.path.exists("/opt/homebrew/bin/node") else None)

Q_640 = "Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step)"
Q_656 = ("What does an 8-hour overtime shift cost for a Firefighter/Paramedic (56 hr, top step) "
         "with 12 years of service?")
Q_716 = "Cost an 8-hour overtime shift for an Administrative Analyst (top step) on October 7"
Q_POLICY = "How is premium overtime compensated under the Firefighters Local 3535 MOU?"
Q_REFUSED = "Cost an 8-hour overtime shift for a Fire Marshal (top step)"
FF_RULE = "firefighters_local3535_mou:overtime_premium_rate"


@pytest.fixture
def case_dir(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE_DIR, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    return str(case)


@pytest.fixture
def client(case_dir):
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def _events(client, qid):
    return client.get(f"/chat/audit/{qid}").json()


def _decisions(events):
    return [e["payload"] for e in events if e["type"] == "decision"]


def _math_step(res):
    li = res["result"]["line_items"][0]
    return next(t for t in li["trace"] if t["kind"] == "math")


# --------------------------------------------------------------------------- #
# L1 — one decision event per fork, losers with reasons, answers unchanged
# --------------------------------------------------------------------------- #
def test_golden_decision_sequence(client):
    res = client.post("/chat", json={"prompt": Q_640}).json()
    assert res["mode"] == "costing" and res["result"]["total"] == 640.80
    audit_ = _events(client, res["query_id"])
    events = audit_["events"]
    forks = [d["fork"] for d in _decisions(events)]
    assert forks == ["intent", "subject", "governance", "rule_filter", "rule_select"] + ["input"] * 4
    by_fork = {d["fork"]: d for d in _decisions(events)}
    gov = by_fork["governance"]
    assert [c["id"] for c in gov["chosen"]] == ["firefighters_local3535_mou"]
    assert len(gov["rejected"]) == 4
    for r in gov["rejected"]:
        assert r["reason"] and ("unit" in r["reason"] or "doc_type" in r["reason"]), r
    rf = by_fork["rule_filter"]
    assert {r["id"] for r in rf["rejected"]} == {
        "firefighters_local3535_mou:bereavement_shifts", "chief_officers_mou:bereavement_hours",
        "admin_group_mou:overtime_premium_rate"}
    assert all(r["reason"] for r in rf["rejected"])
    sel = by_fork["rule_select"]
    assert sel["decided_by"] == "human-rule"
    assert [c["id"] for c in sel["chosen"]] == [FF_RULE]
    assert sel["rejected"][0]["id"] == "firefighters_local3535_mou:longevity_10yr"
    assert "years_of_service >= 10" in sel["rejected"][0]["reason"]
    # every fork says who decided; keyless, that is fixed logic, except the ratified rule
    # (human-rule) and the hours the visitor stated (user) — never 'ai'
    assert {d["decided_by"] for d in _decisions(events)} == {"fixed-logic", "human-rule", "user"}
    # the pre-existing event types are still there, in order, and no model call was added
    types = [e["type"] for e in events]
    expected = ["chat.prompt", "intent.classify", "llm.parse_intent", "data.read",
                "governance.resolve", "rule.selector_considered", "rule.math",
                "citation", "answer.snapshot"]
    positions = [types.index(t) for t in expected]
    assert positions == sorted(positions), types
    assert types.count("llm.call") == 2
    assert core_app._case().ledger().verify()[0]


def test_decision_payload_shape_and_counts(client):
    res = client.post("/chat", json={"prompt": Q_640}).json()
    for d in _decisions(_events(client, res["query_id"])["events"]):
        assert set(d) >= {"fork", "decided_by", "chosen", "rejected", "counts"}
        assert d["counts"]["considered"] == len(d["chosen"]) + len(d["rejected"]) \
            or d["counts"].get("rejected_omitted")
        for r in d["rejected"]:
            assert r["id"] and r["reason"], r
    subj = next(d for d in _decisions(_events(client, res["query_id"])["events"])
                if d["fork"] == "subject")
    assert subj["counts"]["considered"] == 13 and len(subj["chosen"]) == 1


def test_policy_decision_sequence(client):
    res = client.post("/chat", json={"prompt": Q_POLICY}).json()
    assert res["mode"] == "policy", res
    ds = _decisions(_events(client, res["query_id"])["events"])
    assert [d["fork"] for d in ds] == ["intent", "document_scope", "clause_retrieval"]
    cr = ds[-1]
    assert len(cr["chosen"]) == 8 and len(cr["rejected"]) == 6
    for h in cr["chosen"] + cr["rejected"]:
        assert h["ref"]["page"] and len(h["ref"]["bbox"]) == 4, h
    assert all("cutoff 8" in r["reason"] for r in cr["rejected"])
    scope = ds[1]
    assert [c["id"] for c in scope["chosen"]] == ["firefighters_local3535_mou"]
    assert len(scope["rejected"]) == 4


def test_refused_answer_stops_at_governance(client):
    res = client.post("/chat", json={"prompt": Q_REFUSED}).json()
    assert res["mode"] == "blocked", res
    ds = _decisions(_events(client, res["query_id"])["events"])
    assert [d["fork"] for d in ds] == ["intent", "subject", "governance"]
    assert [c["id"] for c in ds[-1]["chosen"]] == ["management_mou"]


def test_decided_by_follows_trail(client, monkeypatch):
    """A fork the model decided is badged 'ai'; keyless it is 'fixed-logic'."""
    def fake_classify(prompt):
        llm._note("classify_intent", "claude", ms=1)
        return "costing"
    monkeypatch.setattr(llm, "classify_intent", fake_classify)
    res = client.post("/chat", json={"prompt": Q_640}).json()
    intent = next(d for d in _decisions(_events(client, res["query_id"])["events"])
                  if d["fork"] == "intent")
    assert intent["decided_by"] == "ai"
    assert all(r["reason"] == "not chosen by the model" for r in intent["rejected"])
    assert decisions.decided_by_for("nothing_called") == "fixed-logic"


def test_governance_records_rejected_sources():
    from core import governance
    case = load_case(CASE_DIR)
    gov = governance.resolve(["admin-group"], "2026-10-07", case.manifest["sources"])
    assert gov.doc_ids == ["admin_group_mou"]
    reasons = {r["doc_id"]: r["reason"] for r in gov.rejected}
    assert set(reasons) == {"firefighters_local3535_mou", "management_mou",
                            "chief_officers_mou", "master_salary_schedule"}
    assert "doc_type" in reasons["master_salary_schedule"]
    gov = governance.resolve(["admin-group"], "2030-01-01", case.manifest["sources"])
    assert not gov.resolved
    assert "after effective_end" in {r["doc_id"]: r["reason"] for r in gov.rejected}["admin_group_mou"]


# --------------------------------------------------------------------------- #
# L3 — the tree is a pure function of the ledger events
# --------------------------------------------------------------------------- #
def test_build_tree_is_pure(client):
    res = client.post("/chat", json={"prompt": Q_640}).json()
    a = _events(client, res["query_id"])
    rebuilt = audit.build_tree(core_app._case().ledger().for_query(res["query_id"]), corpus_size=5)
    assert rebuilt == a["tree"]
    assert audit.build_tree([])["legacy"] is True
    legacy = audit.build_tree([{"seq": 7, "type": "chat.prompt", "payload": {"text": "x"}},
                               {"seq": 8, "type": "rule.math", "payload": {"detail": "a*b = 1"}}])
    assert legacy["legacy"] is True and legacy["seq"] == 8 and legacy["nodes"] == []


def test_tree_counts_and_shape(client):
    res = client.post("/chat", json={"prompt": Q_640}).json()
    tree = _events(client, res["query_id"])["tree"]
    assert tree["legacy"] is False
    assert tree["counts"]["documents"] == {"corpus": 5, "candidates": 5, "chosen": 1}
    # the costing path searches no clauses itself; the 10 are the two declared clause
    # inputs re-found by search (L2), five hits each
    assert tree["counts"]["clauses"] == {"searched": 10, "hits": 10, "cited": 1}
    forks = [n["fork"] for n in tree["nodes"]]
    assert forks == ["prompt", "intent", "subject", "governance", "rule_filter", "rule_select", "answer"]
    gov = tree["nodes"][3]
    assert gov["decided_by"] == "fixed-logic"
    assert [c["status"] for c in gov["children"]] == ["chosen"] + ["rejected"] * 4
    assert all(c["reason"] for c in gov["children"] if c["status"] == "rejected")
    rule = tree["nodes"][5]
    kinds = [(c["fork"], c["status"]) for c in rule["children"]]
    assert ("math", "chosen") in kinds
    math = next(c for c in rule["children"] if c["fork"] == "math")
    assert math["label"] == "$53.40/hr × 1.5 × 8 h = $640.80"
    # every chosen rule node is clickable: a ref with doc, page and bbox
    chosen_rule = next(c for c in rule["children"] if c["status"] == "chosen" and c["fork"] == "rule_select")
    assert chosen_rule["ref"]["doc_id"] == "firefighters_local3535_mou" and chosen_rule["ref"]["page"] == 8
    assert len(chosen_rule["ref"]["bbox"]) == 4


def test_policy_tree_counts(client):
    res = client.post("/chat", json={"prompt": Q_POLICY}).json()
    tree = _events(client, res["query_id"])["tree"]
    assert tree["counts"]["documents"] == {"corpus": 5, "candidates": 5, "chosen": 1}
    assert tree["counts"]["clauses"] == {"searched": 14, "hits": 14, "cited": 4}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_tree_js_renders_n_nodes(client, tmp_path):
    res = client.post("/chat", json={"prompt": Q_656}).json()
    tree = _events(client, res["query_id"])["tree"]
    path = tmp_path / "tree.json"
    path.write_text(json.dumps(tree))
    out = subprocess.run([NODE, "--test", os.path.join(ROOT, "tests", "tree_render.test.mjs")],
                         capture_output=True, text=True, env={**os.environ, "TREE_JSON": str(path)},
                         cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr


def test_tree_js_is_served_and_included(client):
    assert client.get("/static/tree.js").status_code == 200
    assert "/static/tree.js" in client.get("/").text
    js = client.get("/static/app.js").text
    assert "loadTree(" in js and "mathLineEl(" in js


# --------------------------------------------------------------------------- #
# C1 / I13 — the math line with real numbers, each factor with an address
# --------------------------------------------------------------------------- #
def test_math_step_records_inputs():
    rule = Rule.from_dict({"id": "ot", "kind": "selector", "when": "hours > 0",
                           "compute": "effective_base * 1.5 * hours",
                           "citation": {"doc_id": "d", "clause": "c", "page": 1},
                           "status": "ratified", "approver": "t"})
    res = calculate({"hours": 8}, [{"name": "x", "base_hourly": 53.40}], [rule])
    math = next(t for t in res.line_items[0].trace if t.kind == "math")
    assert math.inputs == {"effective_base": 53.4, "hours": 8}
    considered = next(t for t in res.line_items[0].trace if t.kind == "selector-considered")
    assert considered.inputs == {"hours": 8}
    assert res.to_dict()["line_items"][0]["trace"][-1]["inputs"]["effective_base"] == 53.4


def test_operands_and_expansion():
    assert mathline.operands("effective_base * 1.5 * hours", {"effective_base": 53.4, "hours": 8}) == [
        {"kind": "fact", "name": "effective_base", "value": 53.4}, {"kind": "const", "value": 1.5},
        {"kind": "fact", "name": "hours", "value": 8}]
    assert mathline.operands("a + b", {"a": 1, "b": 2}) is None
    ops = mathline.expand(mathline.operands("effective_base * 1.5 * hours",
                                            {"effective_base": 54.735, "hours": 8}),
                          {"effective_base": {"expr": "round(effective_base * 1.025, 6)",
                                              "inputs": {"effective_base": 53.4},
                                              "rule_id": "lon", "citation": {"page": 12}}})
    assert [o["value"] for o in ops] == [53.4, 1.025, 1.5, 8]
    assert ops[1]["rule_id"] == "lon"


def test_chat_math_line_golden(client, case_dir):
    res = client.post("/chat", json={"prompt": Q_640}).json()
    step = _math_step(res)
    assert step["line"] == "$53.40/hr × 1.5 × 8 h = $640.80"
    assert "effective_base" not in step["line"] and "640.8 " not in step["line"]
    rate, mult, hours = step["operands"]
    assert rate["role"] == "rate" and rate["value"] == 53.4
    src = rate["source"]
    assert src["doc_id"] == "master_salary_schedule" and src["page"] == 1
    assert src["text"] == "$53.40" and src["row"] == "FIREFIGHTER/PARAMEDIC- 56 hr"
    assert src["column"] == "MAXIMUM HOURLY RATE" and len(src["bbox"]) == 4
    assert not src.get("unsourced")
    assert mult["source"]["doc_id"] == "firefighters_local3535_mou" and mult["source"]["page"] == 8
    assert hours["source"] == {"kind": "question", "param": "hours"}
    # the same facts are in the ledger's rule.math event ...
    events = _events(client, res["query_id"])["events"]
    rm = next(e["payload"] for e in events if e["type"] == "rule.math")
    assert rm["inputs"]["effective_base"] == 53.4 and rm["line"] == step["line"]
    assert rm["operands"][0]["source"]["bbox"] == src["bbox"]
    # ... in the answer.snapshot event and the frozen snapshot file ...
    snap_ev = next(e["payload"] for e in events if e["type"] == "answer.snapshot")
    assert snap_ev["math"][0]["line"] == step["line"]
    with open(os.path.join(case_dir, "snapshots", snap_ev["snapshot"])) as f:
        frozen = f.read()
    assert "53.4" in frozen and '"hours": 8' in frozen and "effective_base * 1.5 * hours" in frozen
    # ... and the chain still verifies, and the answer still replays.
    assert core_app._case().ledger().verify()[0]
    assert client.get(f"/chat/replay/{res['query_id']}").json()["status"] == "match"


def test_chat_math_line_longevity(client):
    res = client.post("/chat", json={"prompt": Q_656}).json()
    step = _math_step(res)
    assert step["line"] == "$53.40/hr × 1.025 × 1.5 × 8 h = $656.82"
    lon = step["operands"][1]
    assert lon["value"] == 1.025 and lon["source"]["page"] == 12
    assert lon["rule_id"] == "firefighters_local3535_mou:longevity_10yr"
    assert step["operands"][0]["source"]["text"] == "$53.40"


def test_chat_math_line_admin_analyst(client):
    res = client.post("/chat", json={"prompt": Q_716}).json()
    step = _math_step(res)
    assert step["line"] == "$59.68/hr × 1.5 × 8 h = $716.16"
    src = step["operands"][0]["source"]
    assert src["row"] == "ADMINISTRATIVE ANALYST" and src["text"] == "$59.68"
    # $59.68 is printed three times on the page; the cell on the ANALYST line is the one
    # at the row's own height (pypdfium2 text search, y within the title's band).
    l, t, r, b = src["bbox"]
    assert 470 < b < t < 495, src["bbox"]
    assert step["operands"][1]["source"]["doc_id"] == "admin_group_mou"


def test_rate_cells_match_the_pdf():
    """Every roster row binds to a schedule cell whose text IS the roster value."""
    import pypdfium2 as pdfium
    case = load_case(CASE_DIR)
    cat = Catalog(case.path("catalog", "catalog.json"))
    pdf = pdfium.PdfDocument(os.path.join(CASE_DIR, "sources", "master_salary_schedule.pdf"))
    unbound = []
    for s in case.subjects():
        src = mathline.rate_source(case, cat, s, float(s["base_hourly"]))
        if src.get("unsourced"):
            unbound.append((s["name"], src.get("reason")))
            continue
        page = pdf[src["page"] - 1]
        l, t, r, b = src["bbox"]
        text = page.get_textpage().get_text_bounded(left=l - 1, bottom=b - 1, right=r + 1, top=t + 1)
        assert text.strip() == f"${s['base_hourly']:,.2f}", (s["name"], text)
        assert src["context_bbox"][0] < l            # the row band starts at the title
    pdf.close()
    assert unbound == [], unbound


# --------------------------------------------------------------------------- #
# L2 — rules declare their inputs; each input is a sub-search node under the rule
# --------------------------------------------------------------------------- #
def _input_decisions(client, qid, rule_id):
    return [d for d in _decisions(_events(client, qid)["events"])
            if d["fork"] == "input" and d["detail"]["rule_id"] == rule_id]


def test_inputs_become_nodes(client):
    res = client.post("/chat", json={"prompt": Q_640}).json()
    ins = _input_decisions(client, res["query_id"], FF_RULE)
    assert [d["detail"]["name"] for d in ins] == ["multiplier", "regular_rate_definition",
                                                   "base_hourly", "hours"]
    mult, defn, base, hours = ins
    assert mult["decided_by"] == "human-rule" and mult["chosen"][0]["ref"]["page"] == 8
    assert "declared by approver kenny" in mult["chosen"][0]["detail"]
    assert "rank #" in mult["chosen"][0]["detail"]
    assert mult["counts"]["searched"] == 5 and len(mult["rejected"]) + 1 == 5
    assert all("not the declared clause" in r["reason"] for r in mult["rejected"])
    assert defn["chosen"][0]["ref"]["page"] == 8 and defn["chosen"][0]["ref"]["bbox"]
    assert base["decided_by"] == "fixed-logic" and base["chosen"][0]["value"] == 53.4
    assert base["chosen"][0]["ref"]["doc_id"] == "master_salary_schedule"   # the cell
    assert base["detail"]["pointer"]["page"] == 6                          # Appendix A pointer
    assert hours["decided_by"] == "user" and hours["chosen"][0]["value"] == 8.0
    # the tree nests them under the rule node, after the rule's own children
    tree = _events(client, res["query_id"])["tree"]
    rule = next(n for n in tree["nodes"] if n["fork"] == "rule_select")
    names = [c["label"] for c in rule["children"] if c["fork"] == "input"]
    assert names == ["Input — multiplier (clause)", "Input — regular_rate_definition (clause)",
                     "Input — base_hourly (roster)", "Input — hours (question)"]
    assert tree["counts"]["clauses"]["searched"] == 10
    assert res["result"]["total"] == 640.80


def test_longevity_inputs(client):
    res = client.post("/chat", json={"prompt": Q_656}).json()
    lon = _input_decisions(client, res["query_id"], "firefighters_local3535_mou:longevity_10yr")
    assert [d["detail"]["name"] for d in lon] == ["longevity", "years_of_service"]
    assert lon[0]["chosen"][0]["ref"]["page"] == 12
    assert "rank #1" in lon[0]["chosen"][0]["detail"]
    assert lon[1]["chosen"][0]["value"] == 12.0 and lon[1]["decided_by"] == "user"
    assert res["result"]["total"] == 656.82


def test_not_found_declaration_is_flagged(client, case_dir):
    """A declared clause the search cannot find in its top 5 is shown with a flag,
    never dropped and never promoted."""
    path = os.path.join(case_dir, "rules", "rules_ratified.json")
    with open(path) as f:
        data = json.load(f)
    for r in data["rules"]:
        if r["id"] == FF_RULE:
            r["inputs"][0]["citation"]["page"] = 33       # the Term clause, not overtime
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    res = client.post("/chat", json={"prompt": Q_640}).json()
    mult = _input_decisions(client, res["query_id"], FF_RULE)[0]
    assert mult["detail"]["flag"] == "declared citation not found by search"
    assert "not found by search" in mult["chosen"][0]["detail"]
    assert len(mult["rejected"]) == 5
    tree = _events(client, res["query_id"])["tree"]
    rule = next(n for n in tree["nodes"] if n["fork"] == "rule_select")
    assert any("⚠ declared citation not found" in c["label"] for c in rule["children"])


def test_rule_inputs_validate_and_load():
    from core.ruledsl import RuleError, load_rules, parse_inputs, rule_inputs, validate_rules
    base = {"id": "r", "kind": "selector", "when": "True", "compute": "hours",
            "citation": {"doc_id": "d", "clause": "c", "page": 1}}
    assert validate_rules([base], {"hours"}) == {}                      # no inputs: fine
    bad = {**base, "inputs": [{"name": "m", "source": "clause", "query": "x",
                               "citation": {"doc_id": "d"}}]}           # clause needs a page
    errs = validate_rules([bad], {"hours"})
    assert "needs citation.doc_id and page" in " ".join(errs["r"])
    with pytest.raises(RuleError):
        parse_inputs([{"kind": "nope"}], "r")
    with pytest.raises(RuleError):
        Rule.from_dict({**base, "inputs": "multiplier"})
    ok = Rule.from_dict({**base, "inputs": [{"name": "h", "source": "question"}]})
    assert rule_inputs(ok) == [{"name": "h", "kind": "fact", "source": "question", "query": "",
                                "citation": {}, "field": ""}]
    assert rule_inputs(Rule.from_dict(base)) == []
    live = load_rules(os.path.join(CASE_DIR, "rules", "rules_ratified.json"))
    assert {r.id: len(rule_inputs(r)) for r in live} == {
        "firefighters_local3535_mou:bereavement_shifts": 1,
        "firefighters_local3535_mou:overtime_premium_rate": 4,
        "firefighters_local3535_mou:longevity_10yr": 2,
        "chief_officers_mou:bereavement_hours": 1,
        "admin_group_mou:overtime_premium_rate": 3}


def test_inputs_do_not_change_the_rule_fingerprint():
    """The fingerprint a human approved under must survive a declared input."""
    from core import provenance
    d = {"id": "r", "kind": "selector", "when": "True", "compute": "hours",
         "citation": {"doc_id": "d", "clause": "c", "page": 1}}
    with_inputs = {**d, "inputs": [{"name": "h", "source": "question"}]}
    assert provenance.rule_fingerprint(Rule.from_dict(d)) == \
        provenance.rule_fingerprint(Rule.from_dict(with_inputs))


def test_backfill_is_additive_and_idempotent(tmp_path):
    """Strip `inputs` from the shipped file; the back-fill restores the shipped bytes
    exactly — so nothing but `inputs` was touched — and a second run changes nothing."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import backfill_rule_inputs as bf
    shipped = os.path.join(CASE_DIR, "rules", "rules_ratified.json")
    with open(shipped) as f:
        shipped_bytes = f.read()
    data = json.loads(shipped_bytes)
    for r in data["rules"]:
        r.pop("inputs", None)
    stripped = tmp_path / "rules_ratified.json"
    stripped.write_text(json.dumps(data, indent=2))
    assert bf.backfill(str(stripped)) is True
    assert stripped.read_text() == shipped_bytes
    assert bf.backfill(str(stripped)) is False
    assert bf.backfill(shipped) is False                 # the shipped file is already filled


def test_unsourced_rate_is_said_not_hidden():
    case = load_case(CASE_DIR)
    cat = Catalog(case.path("catalog", "catalog.json"))
    src = mathline.rate_source(case, cat, {"name": "Nobody (top step)", "rank": "Nobody"}, 1.0)
    assert src["unsourced"] and "no schedule row" in src["reason"]
    row = mathline.rate_source(case, cat, {"name": "Fire Inspector (top step)", "rank": "Fire Inspector"}, 99.99)
    assert row["unsourced"] and "roster holds 99.99" in row["reason"]
