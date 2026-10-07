"""Hermetic test harness (DEMO_TICKETS K2).

The suite must never reach a paid model, whatever is in the developer's .env:

  1. core/app.py runs _load_dotenv() at import, and most test modules import core.app
     at module top (during collection). KENNY_NO_DOTENV makes that loader a no-op, and
     the key is popped here BEFORE any test module is collected.
  2. An autouse fixture deletes ANTHROPIC_API_KEY for every test and replaces the SDK
     client constructor so a real client cannot be built by accident. Tests that
     exercise the model path replace `core.llm._client` themselves (test_policy,
     test_chat_correctness, test_injection) and keep working — the SDK constructor is
     patched, not the module seam they use.
"""
import os

os.environ["KENNY_NO_DOTENV"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402


def _refuse_real_client(self, *args, **kwargs):
    raise RuntimeError("tests must not construct a real Anthropic client; "
                       "monkeypatch core.llm._client")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        import anthropic
    except ImportError:  # pragma: no cover - SDK absent means no live call is possible
        yield
        return
    monkeypatch.setattr(anthropic.Anthropic, "__init__", _refuse_real_client)
    if hasattr(anthropic, "AsyncAnthropic"):
        monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _refuse_real_client)
    yield
