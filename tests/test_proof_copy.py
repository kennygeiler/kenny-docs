"""Buyer language in the proof surfaces (DEMO_TICKETS.md I4): the server names the
contract by its declared title wherever the chat page needs it, and a verbatim quote
cites a page instead of a bare section sign."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import llm  # noqa: E402

TITLE = "Firefighters Local 3535 MOU 2025–2028 (with Salary Schedule)"


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


def test_costing_response_names_the_contract(client):
    res = client.post("/chat", json={
        "prompt": "Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step)"
    }).json()
    assert res["mode"] == "costing"
    assert res["chosen_docs"][0]["doc_id"] == "firefighters_local3535_mou"
    assert res["chosen_docs"][0]["title"] == TITLE
    for li in res["result"]["line_items"]:
        for c in li["citations"]:
            assert c["title"] == TITLE, c
    # the existing fields other chunks read are untouched
    assert res["chosen_doc"] == "firefighters_local3535_mou"
    assert res["params"]["source"] in ("stub", "claude")


def test_keyless_policy_answer_cites_a_page_not_a_bare_section_sign(client):
    res = client.post("/chat", json={
        "prompt": "How is premium overtime compensated under the Firefighters Local 3535 MOU?"
    }).json()
    assert res["mode"] == "policy"
    assert not res["answer"].startswith("Per §:"), res["answer"]
    assert res["answer"].startswith("Quoted from p.8:"), res["answer"]


def test_quoted_from_prefers_a_clause_label_when_there_is_one():
    assert llm._quoted_from({"clause": "11.3", "page": 4}) == "Per §11.3"
    assert llm._quoted_from({"clause": "", "page": 4}) == "Quoted from p.4"
    assert llm._quoted_from({"clause": None, "page": None}) == "Quoted"
