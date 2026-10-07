"""Entitlement answers come from the engine (DEMO_TICKETS.md B3).

A ratified non-currency rule is anchored to the retrieved evidence by document, page
and bounding-box overlap — not by the free-text clause label the ingest never
produced — and the question is scoped by the named classification's bargaining unit,
never by a department that holds two units. TestClient, no key, both local search
backends when the embedding model is cached (HF_HUB_OFFLINE=1).
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import auth, index, rulematch  # noqa: E402
from core.caseio import load_case  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE_SRC = os.path.join(ROOT, "cases", "santacruz")


def _backends() -> list[str]:
    out = ["bm25"]
    if index.embeddings_available():
        try:
            index.embedder()
            out.append("hybrid")
        except Exception:           # model not cached offline -> bm25 only
            pass
    return out


BACKENDS = _backends()


@pytest.fixture(params=BACKENDS)
def client(request, tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(CASE_SRC, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_BACKEND", request.param)
    # The per-minute chat rate limit (core/auth.py) is a spend cap on the model, not a
    # property under test here; this file posts dozens of questions in seconds.
    monkeypatch.setattr(auth.RateLimiter, "allow", lambda self, key: True)
    from fastapi.testclient import TestClient
    tc = TestClient(core_app.app)
    tc.case_dir = str(case)
    return tc


def _ledger_types(client, qid):
    led = load_case(client.case_dir).ledger()
    return [e["type"] for e in led.read() if e.get("query_id") == qid]


def _ask(client, prompt):
    return client.post("/chat", json={"prompt": prompt}).json()


# --------------------------------------------------------------------------- #
# (a) the second home-page example: 3 shifts from the ratified rule, p.21 box
# --------------------------------------------------------------------------- #
def test_firefighter_bereavement_is_3_shifts_from_the_ratified_rule(client):
    res = _ask(client, "How much bereavement leave does a firefighter get?")
    assert res["mode"] == "entitlement", res
    items = res["result"]["line_items"]
    assert len(items) == 1
    li = items[0]
    assert li["total"] == 3
    assert li["result_type"] == "shifts"
    assert li["rule_id"] == "firefighters_local3535_mou:bereavement_shifts"
    assert li["citations"][0]["doc_id"] == "firefighters_local3535_mou"
    assert li["citations"][0]["page"] == 21
    assert li["citations"][0]["bbox"], "the box the UI draws must travel with the answer"
    assert res["bargaining_units"] == ["firefighters-local-3535"]
    types = _ledger_types(client, res["query_id"])
    assert "entitlement.match" in types
    assert "entitlement.fallback" not in types
    assert "answer.snapshot" in types


# --------------------------------------------------------------------------- #
# (b) a Division Chief is governed by a different contract: 40 hours, p.14
# --------------------------------------------------------------------------- #
def test_division_chief_bereavement_is_40_hours_from_the_chiefs_contract(client):
    res = _ask(client, "How much bereavement leave does a Division Chief (top step) get?")
    assert res["mode"] == "entitlement", res
    li = res["result"]["line_items"][0]
    assert li["total"] == 40 and li["result_type"] == "hours"
    assert li["rule_id"] == "chief_officers_mou:bereavement_hours"
    assert li["citations"][0]["doc_id"] == "chief_officers_mou"
    assert li["citations"][0]["page"] == 14
    assert res["bargaining_units"] == ["chief-officers"]


# --------------------------------------------------------------------------- #
# (c) a Battalion Chief follows ITS roster row's unit (3 shifts once B5 moves the
#     56-hour Battalion Chief into Local 3535; 40 hours while it sits with the chiefs)
# --------------------------------------------------------------------------- #
def test_battalion_chief_follows_its_roster_unit(client):
    row = next(s for s in load_case(client.case_dir).subjects()
               if s["name"] == "Battalion Chief (56 hr top step)")
    res = _ask(client, "How much bereavement leave does a Battalion Chief (56 hr top step) get?")
    assert res["mode"] == "entitlement", res
    li = res["result"]["line_items"][0]
    expected = {"chief-officers": (40, "hours", "chief_officers_mou"),
                "firefighters-local-3535": (3, "shifts", "firefighters_local3535_mou")}
    total, rtype, doc = expected[row["bargaining_unit"]]
    assert (li["total"], li["result_type"], li["citations"][0]["doc_id"]) == (total, rtype, doc)


# --------------------------------------------------------------------------- #
# (d) negatives: no rule covers it -> quote the subject's OWN contract, never the
#     other fire unit's
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prompt", [
    "How many sick days does a firefighter get?",
    "How many holidays does a firefighter get?",
    "How long is the probationary period for a firefighter?",
])
def test_uncovered_entitlement_quotes_only_the_subjects_contract(client, prompt):
    res = _ask(client, prompt)
    assert res["mode"] == "policy", res
    assert res["sources"], "an in-corpus question about leave must quote something"
    assert {s["doc_id"] for s in res["sources"]} == {"firefighters_local3535_mou"}
    assert res["scope_how"] == "unit"
    types = _ledger_types(client, res["query_id"])
    assert "entitlement.match" not in types
    # the keyword router sends 'how many ... days' to the entitlement handler, which
    # must record that it fell back; 'holidays' alone routes straight to policy
    if "entitlement.retrieval" in types:
        assert "entitlement.fallback" in types


# --------------------------------------------------------------------------- #
# (e) nobody named -> ask, never a department-wide search
# --------------------------------------------------------------------------- #
def test_nobody_named_asks_who(client):
    res = _ask(client, "How much bereavement leave do employees get?")
    assert res["mode"] == "clarify", res
    assert "who is this for" in res["question"].lower()


def test_named_document_scopes_to_its_unit(client):
    res = _ask(client, "How much bereavement leave under the Chief Officers MOU?")
    assert res["mode"] == "entitlement", res
    items = res["result"]["line_items"]
    assert len(items) == 1, "the whole unit gets one answer -> one row, not one per rank"
    assert items[0]["total"] == 40 and items[0]["result_type"] == "hours"
    assert res["scope_how"] == "document"


# --------------------------------------------------------------------------- #
# the golden gate agrees with the chat path on the new result type
# --------------------------------------------------------------------------- #
def test_bereavement_golden_and_rule_share_the_shifts_type():
    import json
    case = load_case(CASE_SRC)
    with open(os.path.join(CASE_SRC, "rules", "rules_ratified.json")) as f:
        rules = json.load(f)["rules"]
    rule = next(r for r in rules if r["id"] == "firefighters_local3535_mou:bereavement_shifts")
    golden = next(g for g in case.golden_cases() if "Local 3535" in g["name"]
                  and "bereavement" in g["name"])
    assert rule["result_type"] == golden["result_type"] == "shifts"
    ok, detail = core_app._check_golden(case, rules, golden)
    assert ok and detail["status"] == "pass", detail


# --------------------------------------------------------------------------- #
# (f) geometry
# --------------------------------------------------------------------------- #
def test_bbox_overlap_identical_disjoint_and_inverted():
    a = [107.28, 189.498, 505.259, 167.118]          # PDF order: y0 > y1
    assert rulematch.bbox_overlap(a, a) == 1.0
    assert rulematch.bbox_overlap(a, [107.28, 167.118, 505.259, 189.498]) == 1.0  # inverted
    assert rulematch.bbox_overlap(a, [600, 700, 650, 690]) == 0.0
    assert rulematch.bbox_overlap(a, []) == 0.0 and rulematch.bbox_overlap(None, a) == 0.0
    # a chunk inside a wider box is the same evidence (smaller-area denominator)
    assert rulematch.bbox_overlap(a, [200, 185, 300, 170]) == 1.0
    # half-covered -> 0.5
    assert abs(rulematch.bbox_overlap([0, 0, 10, 10], [5, 0, 15, 10]) - 0.5) < 1e-9


def test_anchored_requires_same_doc_and_page_and_overlap():
    class R:
        class citation:
            doc_id, page, bbox = "d", 21, [100, 190, 500, 170]
    hits = [{"doc_id": "d", "page": 20, "bbox": [100, 190, 500, 170]},     # wrong page
            {"doc_id": "x", "page": 21, "bbox": [100, 190, 500, 170]},     # wrong doc
            {"doc_id": "d", "page": 21, "bbox": [100, 90, 500, 70]},       # no overlap
            {"doc_id": "d", "page": 21, "bbox": [100, 185, 500, 175]}]     # inside
    assert rulematch.anchored(R, hits) == (4, 1.0)
    assert rulematch.anchored(R, hits[:3]) is None


def test_select_rules_by_evidence_and_by_topic():
    class Rule:
        def __init__(self, id_, topic, page):
            self.id, self.topic = id_, topic
            self.citation = type("C", (), {"doc_id": "d", "page": page, "bbox": [0, 10, 10, 0]})()
    bere, sick = Rule("r1", "bereavement", 21), Rule("r2", "sick leave", 20)
    hits = [{"doc_id": "d", "page": 21, "bbox": [0, 10, 10, 0]}]
    rules, found, amb = rulematch.select_rules([bere, sick], hits, ["bereavement", "leave"])
    assert [r.id for r in rules] == ["r1"] and found[0]["via"] == "evidence" and not amb
    # retrieval missed, topic word present
    rules, found, amb = rulematch.select_rules([bere], [], ["bereavement"])
    assert [r.id for r in rules] == ["r1"] and found[0]["via"] == "topic"
    # nothing anchored, nothing in the question -> nothing selected
    assert rulematch.select_rules([bere, sick], [], ["holiday"]) == ([], [], [])
