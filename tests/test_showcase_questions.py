"""Showcased policy and lookup questions return the right clause; off-corpus
questions are refused (DEMO_TICKETS.md B6).

Query-time only: scope by the named document / classification's unit, strip the
scope's own name from the query, pin the ratified rule's cited clause, and refuse
below an idf-weighted relevance floor instead of asking 'which department?'.
TestClient, no key, both local backends when the embedding model is cached.
"""
import os
import re
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import auth, index, rulematch  # noqa: E402
from core.caseio import load_case  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE_SRC = os.path.join(ROOT, "cases", "santacruz")
TEMPLATES = os.path.join(ROOT, "core", "templates")


def _backends() -> list[str]:
    out = ["bm25"]
    if index.embeddings_available():
        try:
            index.embedder()
            out.append("hybrid")
        except Exception:
            pass
    return out


@pytest.fixture(params=_backends())
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


def _ask(client, prompt, **extra):
    return client.post("/chat", json={"prompt": prompt, **extra}).json()


# --------------------------------------------------------------------------- #
# (a) the third home-page example quotes the p.8 clause the $640.80 answer boxed
# --------------------------------------------------------------------------- #
def test_showcase_overtime_question_quotes_the_pinned_p8_clause(client):
    res = _ask(client, "What does the Firefighters Local 3535 MOU say about overtime?")
    assert res["mode"] == "policy", res
    top = res["sources"][0]
    assert top["doc_id"] == "firefighters_local3535_mou" and top["page"] == 8
    assert "premium overtime compensation" in top["text"]
    assert top["pinned_by"] == "firefighters_local3535_mou:overtime_premium_rate"
    assert {s["doc_id"] for s in res["sources"]} == {"firefighters_local3535_mou"}
    assert not res["answer"].startswith("Per §:")
    assert res["answer"].startswith("From Firefighters Local 3535 MOU, p.8:")
    assert res["scope_how"] == "document"
    led = load_case(client.case_dir).ledger()
    ev = [e for e in led.read() if e.get("query_id") == res["query_id"]
          and e["type"] == "policy.answer"]
    assert ev and ev[0]["payload"]["pinned"] == ["firefighters_local3535_mou:overtime_premium_rate"]
    assert ev[0]["payload"]["scope_how"] == "document"
    assert "coverage" in ev[0]["payload"]


# --------------------------------------------------------------------------- #
# (b) a published rate: the schedule is in scope and only the named class's rows float
# --------------------------------------------------------------------------- #
def test_fire_captain_rate_lookup_reads_the_master_salary_schedule(client):
    res = _ask(client, "What is the top step hourly rate for a Fire Captain?")
    assert res["mode"] == "lookup", res
    assert "FIRE CAPTAIN" in res["answer"] and "$61.01" in res["answer"]
    assert "Fire Investigator" not in res["answer"] and "Investigator" not in res["answer"]
    assert res["sources"][0]["doc_id"] == "master_salary_schedule"
    assert "master_salary_schedule" in {c["doc_id"] for c in res["considered"]}


@pytest.mark.skip(reason="integration: data-goldens archived the police roster rows "
                  "(archive/santacruz_roster_police_sample.csv); every shipped roster "
                  "classification now has a Master Salary Schedule row, so the "
                  "'couldn't find' lookup path needs a tmp-case roster fixture (wave 2)")
def test_lookup_names_the_classification_it_could_not_find(client):
    # The schedule has no row for the unrepresented police sample rows.
    res = _ask(client, "What is the hourly rate for a Police Officer Step A?")
    assert res["mode"] == "lookup", res
    assert "Police Officer" in res["answer"] and "couldn't find" in res["answer"]
    assert res["sources"] == []


# --------------------------------------------------------------------------- #
# (c) off-corpus questions are refused by name, never asked for a department
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prompt", [
    "What is the heat pump rebate for a 3 ton system?",
    "hi",
    "What is the weather in Santa Cruz today?",
])
def test_off_corpus_questions_are_out_of_scope(client, prompt):
    res = _ask(client, prompt)
    assert res["mode"] == "out_of_scope", res
    assert res["options"] == [] and res["sources"] == []
    assert "not covered by the 5 documents" in res["answer"]
    assert "Which department" not in res["answer"]
    for title in ("Firefighters Local 3535 MOU", "Master Salary Schedule"):
        assert title in res["answer"]


# --------------------------------------------------------------------------- #
# (d) a firefighter's probation question never quotes the chiefs' contract
# --------------------------------------------------------------------------- #
def test_firefighter_probation_never_cites_the_chief_officers(client):
    res = _ask(client, "How long is the probationary period for a firefighter?")
    assert res["mode"] in ("policy", "entitlement"), res
    docs = {s["doc_id"] for s in res.get("sources", [])}
    assert docs and "chief_officers_mou" not in docs


# --------------------------------------------------------------------------- #
# (e) a named document is the scope, whatever the department guess would have been
# --------------------------------------------------------------------------- #
def test_named_management_mou_is_never_a_clarify(client):
    res = _ask(client, "What does the management MOU say about layoffs?")
    assert res["mode"] != "clarify", res
    assert all(s["doc_id"] == "management_mou" for s in res.get("sources", []))
    assert all(c["doc_id"] == "management_mou" for c in res.get("considered", []))
    if res["mode"] == "out_of_scope":            # the shipped index has no layoff clause
        assert "Management MOU" in res["answer"]


def test_unscoped_question_offers_contracts_not_departments(client):
    res = _ask(client, "What does the MOU say about layoffs?")
    assert res["mode"] == "clarify", res
    assert set(res["options"]) == {"Admin Group MOU", "Firefighters Local 3535 MOU",
                                   "Chief Officers Association MOU"}
    assert "district" not in res["options"] and "fire" not in res["options"]
    # the post-back carries the chosen title through the old `department` field
    again = _ask(client, "What does the MOU say about layoffs?",
                 query_id=res["query_id"], department="Admin Group MOU")
    assert again["mode"] == "policy", again
    assert {s["doc_id"] for s in again["sources"]} == {"admin_group_mou"}


# --------------------------------------------------------------------------- #
# (f) every printed example answers correctly today
# --------------------------------------------------------------------------- #
def _printed_examples() -> list[str]:
    html = open(os.path.join(TEMPLATES, "chat.html")).read()
    tour = open(os.path.join(TEMPLATES, "tour.js")).read()
    # chat-ui (I16) prints the examples as data-q buttons; the older <li> list is kept
    # as a fallback so either markup is read.
    out = re.findall(r'data-q="([^"]+)"', html)
    if "For example:" in html:
        out += re.findall(r"<li>([^<]+)</li>", html.split("For example:")[1].split("</ul>")[0])
    out += re.findall(r"QUESTION_\w+\s*=\s*'([^']+)'", tour)
    seen: list[str] = []
    for q in out:
        if q not in seen:
            seen.append(q)
    return seen


EXPECTED_PAGE = {"costing": 8, "entitlement": 21, "policy": 8}


@pytest.mark.parametrize("prompt", _printed_examples())
def test_every_printed_example_answers_with_its_clause(client, prompt):
    res = _ask(client, prompt)
    if res["mode"] == "blocked" and res.get("next"):
        # chat-ui's third example is a designed refusal (I6/I16): no clause to cite,
        # but it must name the contract and point at an admin tab, which `next` proves.
        assert "Management MOU" in res["message"], res
        return
    assert res["mode"] in EXPECTED_PAGE, (prompt, res)
    if res["mode"] == "policy":
        page = res["sources"][0]["page"]
    else:
        page = res["result"]["line_items"][0]["citations"][0]["page"]
    assert page == EXPECTED_PAGE[res["mode"]], (prompt, res["mode"], page)


# --------------------------------------------------------------------------- #
# (g) unit tests: coverage, row filter, document naming, query stripping
# --------------------------------------------------------------------------- #
def test_coverage_is_idf_weighted():
    idf = {"rebate": 8.0, "santa": 1.0, "cruz": 1.0, "__unseen__": 8.0}
    assert rulematch.coverage(["rebate", "santa", "cruz"], "Santa Cruz County", idf) == \
        pytest.approx(2 / 10)
    assert rulematch.coverage(["rebate"], "a rebate of $3,000", idf) == 1.0
    assert rulematch.coverage(["weather"], "Santa Cruz", idf) == 0.0   # unseen term
    assert rulematch.coverage([], "anything", idf) == 0.0
    assert rulematch.coverage(["overtime", "rate"], "the overtime rate", None) == 1.0


def test_rate_rows_float_only_for_the_named_classification():
    rows = [{"text": "TITLE: FIRE CAPTAIN- 56 hr | MAXIMUM HOURLY RATE: $61.01"},
            {"text": "TITLE: FIRE INVESTIGATOR | Hourly Regular: $500.00/monthly"}]
    words = rulematch.classification_words([{"name": "Fire Captain (56 hr top step)",
                                             "rank": "Fire Captain"}])
    assert words == [{"fire", "captain"}]
    assert rulematch.row_matches(rows[0], words) and not rulematch.row_matches(rows[1], words)


def test_named_documents_and_clarify_labels():
    sources = load_case(CASE_SRC).manifest["sources"]
    ids = lambda text: [s["id"] for s, _ in rulematch.named_documents(sources, text)]
    assert ids("What does the Firefighters Local 3535 MOU say about overtime?") == \
        ["firefighters_local3535_mou"]
    assert ids("what does the Local 3535 contract say") == ["firefighters_local3535_mou"]
    assert ids("What does the management MOU say about layoffs?") == ["management_mou"]
    assert ids("bereavement under the Chief Officers MOU") == ["chief_officers_mou"]
    assert ids("what is on the salary schedule for a captain") == ["master_salary_schedule"]
    # a bare topic word is not a document name
    assert ids("management rights for a firefighter") == []
    assert ids("How much bereavement leave does a firefighter get?") == []
    assert rulematch.source_for_label(sources, "Admin Group MOU")["id"] == "admin_group_mou"
    assert rulematch.source_for_label(sources, "fire") is None


def test_retrieval_query_strips_scope_and_frame_words():
    q, stems = rulematch.retrieval_query(
        "What does the Firefighters Local 3535 MOU say about overtime?",
        set(index.tokenize("firefighters local 3535 mou")))
    assert q == "overtime" and stems == ["overtime"]
    assert rulematch.retrieval_query("hi") == ("", [])
    q, stems = rulematch.retrieval_query("How much bereavement leave does a firefighter get?",
                                         {"firefighter"})
    assert stems == ["bereavement", "leave"]
