"""G2 / A4 / A8: schema-validated model I/O at one chokepoint, one bounded client, the
breaker, and 'a model slip or a rule error must never 500 a question'.

Every model reply here comes from a stub client (no key, no network)."""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import llm  # noqa: E402

Q = "What does an 8-hour overtime shift cost for a Firefighter/Paramedic (56 hr, top step)?"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# stub client: answers each labelled call from a script
# --------------------------------------------------------------------------- #
class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, text, stop="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop
        self.usage = type("U", (), {"input_tokens": 11, "output_tokens": 7})()


def _scripted(monkeypatch, replies: dict):
    """replies: substring-of-system -> text (or a dict to be JSON-dumped)."""
    monkeypatch.setattr(llm, "have_key", lambda: True)
    llm._breaker_reset()

    class _Messages:
        def create(self, **kw):
            system = kw.get("system", "")
            for key, val in replies.items():
                if key in system:
                    if isinstance(val, Exception):
                        raise val
                    return _Msg(val if isinstance(val, str) else json.dumps(val))
            raise RuntimeError("API down")

    class _Client:
        messages = _Messages()

        def with_options(self, **kw):
            return self

    monkeypatch.setattr(llm, "_client", lambda: _Client())


@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("KENNY_LLM", raising=False)
    llm._breaker_reset()
    from fastapi.testclient import TestClient
    return TestClient(core_app.app, raise_server_exceptions=False)


def _trail(client, qid):
    return [e["payload"] for e in client.get(f"/chat/audit/{qid}").json()["events"]
            if e["type"] == "llm.call"]


def _intent_and_parse(hours=8, date="", subjects=None):
    return {"Classify the question": {"intent": "costing"},
            "extract query parameters": {"subjects": subjects if subjects is not None
                                         else ["Firefighter/Paramedic (56 hr, top step)"],
                                         "hours": hours, "date": date,
                                         "holiday_weekday": ""}}


# --------------------------------------------------------------------------- #
# the parse schema: type slips become coercions or recorded fallbacks, never 500s
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hours,expect", [
    ("8", 640.8), ("8.0", 640.8), (8, 640.8),
    (None, None), ([8], None), ("eight", None), ("1" * 400, None),
])
def test_hours_type_slips_never_500(client, monkeypatch, hours, expect):
    _scripted(monkeypatch, _intent_and_parse(hours=hours))
    res = client.post("/chat", json={"prompt": Q})
    assert res.status_code == 200, res.text
    body = res.json()
    trail = _trail(client, body["query_id"])
    if expect is not None:
        assert body["mode"] == "costing" and body["result"]["total"] == expect
        assert body["params"]["hours"] == 8.0           # coerced AND written back
    else:
        assert body["mode"] in ("costing", "clarify", "blocked")
        kinds = [c["source"] for c in trail if c["fn"] == "parse_intent"]
        # a schema failure is recorded as an error followed by a fallback (stub), or the
        # value was coerced (None -> 0.0 -> the hours>0 rule refuses -> blocked)
        if "fallback" in kinds:
            assert kinds.index("error") < kinds.index("fallback")
        else:
            parsed = [e["payload"] for e in client.get(f"/chat/audit/{body['query_id']}")
                      .json()["events"] if e["type"] == "llm.parse_intent"]
            assert parsed and parsed[0]["hours"] == 0.0
        events = client.get(f"/chat/audit/{body['query_id']}").json()["events"]
        assert any(e["type"] in ("answer.snapshot", "costing.blocked", "costing.clarify")
                   for e in events), "every turn ends with a terminal event"
    assert trail, "every turn records its model calls"


def test_date_as_integer_does_not_500(client, monkeypatch):
    _scripted(monkeypatch, _intent_and_parse(date=20260704))
    res = client.post("/chat", json={"prompt": Q + " on July 4"})
    assert res.status_code == 200
    assert res.json()["mode"] in ("costing", "clarify", "blocked")


def test_fenced_json_followed_by_prose_is_taken_from_the_model(client, monkeypatch):
    _scripted(monkeypatch, {
        "Classify the question": '```json\n{"intent": "policy"}\n```\nNote: I chose {policy} because...',
        "Answer the question": {"answer": "Per the clause, three (3) shifts."}})
    with llm.record() as trail:
        assert llm.classify_intent("what does the contract say about bereavement?") == "policy"
    assert [c["source"] for c in trail] == ["claude"], "no fallback for valid fenced JSON"
    assert trail[0]["input_tokens"] == 11 and trail[0]["output_tokens"] == 7


def test_refusal_is_reported_as_a_refusal(monkeypatch):
    monkeypatch.setattr(llm, "have_key", lambda: True)
    llm._breaker_reset()

    class _Messages:
        def create(self, **kw):
            return _Msg("I cannot help with that.", stop="refusal")

    class _Client:
        messages = _Messages()
    monkeypatch.setattr(llm, "_client", lambda: _Client())
    with llm.record() as trail:
        llm.classify_intent("anything")
    assert trail[0]["source"] == "error" and "ModelRefused" in trail[0]["error"]
    assert trail[1]["source"] == "fallback"


def test_rank_score_string_and_missing_doc_id_do_not_raise(monkeypatch):
    _scripted(monkeypatch, {"rank which documents": {"candidates": [
        {"doc_id": "firefighters_local3535_mou", "score": "0.95", "reason": None},
        {"score": 0.5, "reason": "no id"},
        {"doc_id": "phantom", "score": 0.4}]}})
    with llm.record() as trail:
        out = llm.rank_documents("overtime?", [{"doc_id": "firefighters_local3535_mou",
                                                 "tags": [], "summary": ""}])
    # a candidate without doc_id fails the schema -> recorded fallback, no exception
    assert isinstance(out, list) and out
    assert any(c["source"] in ("fallback", "error") for c in trail)
    _scripted(monkeypatch, {"rank which documents": {"candidates": [
        {"doc_id": "firefighters_local3535_mou", "score": "0.95"},
        {"doc_id": "phantom", "score": 0.4}]}})
    out = llm.rank_documents("overtime?", [{"doc_id": "firefighters_local3535_mou",
                                             "tags": [], "summary": ""}])
    assert [c["doc_id"] for c in out] == ["firefighters_local3535_mou"]
    assert out[0]["score"] == 0.95 and out[0]["source"] == "claude"


def test_draft_shape_slips_are_rejected_then_repaired_once(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(llm, "have_key", lambda: True)
    llm._breaker_reset()

    def fake(system, user, max_tokens=1500, label="llm"):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"rules": None, "needs_data": ["x"]}      # needs_data entries not dicts
        return {"rules": [{"id": "r1", "kind": "selector", "result_type": "currency",
                           "when": "True", "compute": "1", "citation": "p.8"}],
                "needs_data": []}
    monkeypatch.setattr(llm, "_claude_json", fake)
    out = llm.draft_rules([{"clause": "1.1", "page": 1, "text": "x", "bbox": []}], "d",
                          known_facts={"hours"}, field_values={}, bool_facts=[])
    assert calls["n"] == 2, "one repair call with the validation errors appended"
    assert out and out[0]["citation"]["clause"] == "p.8" and out[0]["citation"]["doc_id"] == "d"


def test_tag_document_coerces_shapes(monkeypatch):
    _scripted(monkeypatch, {"classify a policy document": {
        "department": None, "tags": "overtime", "summary": 3, "proposed_tags": None}})
    out = llm.tag_document("text", {})
    assert out["tags"] == ["overtime"] and out["department"] == "" and out["summary"] == "3"
    assert out["proposed_tags"] == [] and out["source"] == "claude"


# --------------------------------------------------------------------------- #
# A4: rule errors and handler exceptions are blocked answers with a terminal event
# --------------------------------------------------------------------------- #
def test_rule_that_raises_is_blocked_not_500(client, monkeypatch):
    rules_path = os.path.join(core_app.CASE_DIR, "rules", "rules_ratified.json")
    data = json.load(open(rules_path))
    for r in data["rules"]:
        if r["id"] == "firefighters_local3535_mou:overtime_premium_rate":
            r["when"] = "hours > 0 and 1/(hours-8) > -99"
    json.dump(data, open(rules_path, "w"))
    res = client.post("/chat", json={"prompt": Q})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "blocked"
    events = client.get(f"/chat/audit/{body['query_id']}").json()["events"]
    blocked = [e for e in events if e["type"] == "costing.blocked"]
    assert blocked and "RuleError" in blocked[-1]["payload"]["error"]


def test_handler_exception_returns_json_blocked_and_records_chat_error(client, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("forced")
    monkeypatch.setattr(core_app, "_chat", boom)
    res = client.post("/chat", json={"prompt": Q})
    assert res.status_code == 500
    assert res.headers["content-type"].startswith("application/json")
    assert res.json()["mode"] == "blocked"
    events = client.get("/admin/ledger").json()["events"]
    errs = [e for e in events if e["type"] == "chat.error"]
    assert errs and errs[-1]["payload"]["type"] == "RuntimeError"


# --------------------------------------------------------------------------- #
# A8: one shared bounded client, breaker, visible degradation
# --------------------------------------------------------------------------- #
def test_client_is_built_once_with_timeout_and_one_retry(monkeypatch):
    built = []

    class _Fake:
        def __init__(self, **kw):
            built.append(kw)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Fake)
    llm._reset_client()
    try:
        a = llm._client()
        b = llm._client()
    finally:
        llm._reset_client()
    assert a is b and len(built) == 1
    kw = built[0]
    assert kw["max_retries"] == 1
    assert float(kw["timeout"].read) == 30.0 and float(kw["timeout"].connect) == 5.0


def test_draft_rules_requests_the_long_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "have_key", lambda: True)
    llm._breaker_reset()

    class _Messages:
        def create(self, **kw):
            return _Msg(json.dumps({"rules": [], "needs_data": []}))

    class _Client:
        messages = _Messages()

        def with_options(self, **kw):
            seen.append(kw)
            return self
    monkeypatch.setattr(llm, "_client", lambda: _Client())
    llm.draft_rules([{"clause": "1.1", "page": 1, "text": "x", "bbox": []}], "d",
                    known_facts={"hours"}, field_values={}, bool_facts=[])
    assert seen and seen[0]["timeout"] == 90.0
    seen.clear()
    llm.classify_intent("x")
    assert not seen, "ordinary touchpoints use the shared 30 s client"


def test_breaker_opens_after_three_errors_and_cools_down(client, monkeypatch):
    import anthropic
    monkeypatch.setattr(llm, "have_key", lambda: True)
    llm._breaker_reset()
    now = [1000.0]
    monkeypatch.setattr(llm, "_clock", lambda: now[0])
    calls = {"n": 0}

    class _Messages:
        def create(self, **kw):
            calls["n"] += 1
            raise anthropic.APITimeoutError(request=None)

    class _Client:
        messages = _Messages()
    monkeypatch.setattr(llm, "_client", lambda: _Client())
    for _ in range(3):
        llm.classify_intent("x")
    assert calls["n"] == 3
    assert llm.model_available() is False
    assert llm.llm_status() == "degraded"
    with llm.record() as trail:
        llm.classify_intent("x")
    assert calls["n"] == 3, "a fourth touchpoint makes no client call"
    assert trail[0]["source"] == "fallback" and "cooling down" in trail[0]["rule"]
    assert client.get("/api/case").json()["llm_status"] == "degraded"
    now[0] += 61
    assert llm.model_available() is True
    llm._breaker_reset()
