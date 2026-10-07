"""The suite is hermetic (DEMO_TICKETS K2): no .env, no key, no real model client."""
import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import llm  # noqa: E402


def test_no_api_key_reaches_the_test_process():
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert not llm.have_key()


def test_dotenv_loader_is_a_no_op_under_pytest(monkeypatch):
    """Even with a .env present, importing core.app under pytest must not read it."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    def _no_open(*a, **k):
        raise AssertionError("_load_dotenv opened a file under pytest")
    monkeypatch.setattr(builtins, "open", _no_open)
    core_app._load_dotenv()          # returns before touching the filesystem
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_dotenv_loader_honours_the_opt_out_outside_pytest(monkeypatch):
    monkeypatch.setenv("KENNY_NO_DOTENV", "1")
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")))
    core_app._load_dotenv()


def test_real_anthropic_client_cannot_be_constructed():
    with pytest.raises(RuntimeError, match="must not construct a real Anthropic client"):
        llm._client()


def test_model_touchpoint_falls_back_without_a_key():
    """With no key the stub path answers; nothing tries to build a client."""
    out = llm.parse_intent("cost an 8-hour overtime shift", {"output_shape": {}}, [])
    assert out["source"] == "stub"
