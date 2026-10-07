"""G9: KENNY_LLM=off disables every model touchpoint even when a key exists, the
header can say so, and an explicitly empty real environment variable beats .env."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import llm  # noqa: E402

Q = "What does an 8-hour overtime shift cost for a Firefighter/Paramedic (56 hr, top step)?"


@pytest.fixture
def client(tmp_path, monkeypatch):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cases", "santacruz")
    case = tmp_path / "santacruz"
    shutil.copytree(src, case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("KENNY_LLM", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "No"])
def test_switch_beats_a_present_key(monkeypatch, value):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy-for-test")
    monkeypatch.setenv("KENNY_LLM", value)
    assert llm.have_key() is False
    assert llm.model_available() is False
    assert llm.llm_mode() == "off"


def test_chat_with_switch_off_never_builds_a_client(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy-for-test")
    monkeypatch.setenv("KENNY_LLM", "off")

    def boom():
        raise AssertionError("the model client must not be built with KENNY_LLM=off")
    monkeypatch.setattr(llm, "_client", boom)
    res = client.post("/chat", json={"prompt": Q})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "costing" and body["result"]["total"] == 640.8
    assert body["params"]["source"] == "stub"


def test_api_case_reports_the_mode(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy-for-test")
    monkeypatch.setenv("KENNY_LLM", "off")
    assert client.get("/api/case").json()["llm_mode"] == "off"
    monkeypatch.delenv("KENNY_LLM")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    j = client.get("/api/case").json()
    assert j["llm_mode"] == "no-key" and j["llm_status"] == "none"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy-for-test")
    assert client.get("/api/case").json()["llm_mode"] == "claude"


def test_dotenv_does_not_overwrite_an_explicitly_empty_variable(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-from-dotenv\nKENNY_OTHER=1\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("KENNY_OTHER", raising=False)
    monkeypatch.setattr(os.path, "abspath", lambda p: str(tmp_path / "core" / "app.py")
                        if p.endswith("app.py") else os.path.realpath(p))
    core_app._load_dotenv(force=True)     # the K2 guard would otherwise no-op under pytest
    assert os.environ["ANTHROPIC_API_KEY"] == "", "an empty real variable must win"
    assert os.environ.get("KENNY_OTHER") == "1", "unset variables are still loaded"
    monkeypatch.delenv("KENNY_OTHER", raising=False)


def test_chat_badge_text_lists_the_off_mode():
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "core", "templates", "app.js")).read()
    assert "LLM: off (deterministic)" in js
    assert "degraded" in js


def test_client_timeout_is_a_plain_number(monkeypatch):
    """Regression: an httpx.Timeout object raised TypeError on hosts whose SDK links
    against httpx2, tripping the breaker on every call. A float is portable."""
    import inspect
    from core import llm
    code = "\n".join(l for l in inspect.getsource(llm._client).splitlines()
                     if not l.strip().startswith("#"))
    assert "httpx.Timeout(" not in code
    captured = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            captured.update(kw)

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    llm._reset_client()
    llm._client()
    assert isinstance(captured["timeout"], float)
    llm._reset_client()
