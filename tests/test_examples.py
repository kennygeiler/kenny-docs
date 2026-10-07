"""Home-page examples (DEMO_TICKETS.md I16): every `data-q` on the served chat page is
asked keyless, and each must still show the behaviour it advertises — a computed
number, a quoted clause, a reasoned refusal. The home page can then never advertise a
question the keyless product answers wrongly."""
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


def _examples(html: str) -> list[str]:
    return re.findall(r'class="ghost example"\s+data-q="([^"]+)"', html)


def test_examples_are_buttons_not_list_items(client):
    html = client.get("/").text
    qs = _examples(html)
    assert len(qs) == 3, qs
    welcome = html.split('<main id="log"')[1].split("</main>")[0]
    assert "<li>" not in welcome, "examples must be tappable, not plain list items"
    # the two questions the keyless product answered badly are gone (I16 evidence)
    assert "bereavement" not in welcome.lower()
    assert "say about overtime" not in welcome


def test_each_example_answers_the_way_it_is_labelled(client):
    qs = _examples(client.get("/").text)
    costing = client.post("/chat", json={"prompt": qs[0]}).json()
    assert costing["mode"] == "costing"
    assert round(costing["result"]["total"], 2) == 640.80

    policy = client.post("/chat", json={"prompt": qs[1]}).json()
    assert policy["mode"] == "policy", policy
    top = policy["sources"][0]
    assert top["doc_id"] == "firefighters_local3535_mou"
    assert top["page"] == 8
    assert "one and one" in top["text"]
    assert not policy["answer"].startswith("Per §:"), policy["answer"]

    refusal = client.post("/chat", json={"prompt": qs[2]}).json()
    assert refusal["mode"] == "blocked"
    assert "Management MOU" in refusal["message"]


def test_deep_link_and_example_handler_ship(client):
    js = client.get("/static/app.js").text
    assert "button.example" in js, "one delegated click handler for the examples"
    assert "URLSearchParams(location.search).get('q')" in js, "/?q= asks once on load"
