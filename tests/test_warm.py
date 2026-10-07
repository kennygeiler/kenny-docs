"""Pre-warm retrieval at startup; no Hugging Face hub contact at request time; one
search backend per case (DEMO_TICKETS.md A5)."""
import os
import shutil
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import index, warm  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeModel:
    constructions: list = []

    def __init__(self, name, **kwargs):
        _FakeModel.constructions.append((name, kwargs))
        time.sleep(0.2)

    def encode(self, texts, normalize_embeddings=True):
        import numpy as np
        return np.zeros((len(texts), 384))        # same width as the baked index


@pytest.fixture
def fake_model(monkeypatch):
    import sentence_transformers
    _FakeModel.constructions = []
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeModel)
    monkeypatch.setattr(index, "_MODEL", None)
    return _FakeModel


@pytest.fixture
def case_copy(tmp_path, monkeypatch):
    dst = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), dst)
    monkeypatch.setattr(core_app, "CASE_DIR", str(dst))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    warm.reset()
    yield str(dst)
    warm.wait(10)
    warm.reset()


def test_embedder_loads_locally_once_across_threads(fake_model):
    got = []
    threads = [threading.Thread(target=lambda: got.append(index.embedder()))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fake_model.constructions) == 1
    name, kwargs = fake_model.constructions[0]
    assert name == index._MODEL_NAME and kwargs.get("local_files_only") is True
    assert got[0] is got[1]


def test_embedder_honours_offline_when_cache_is_empty(monkeypatch):
    import sentence_transformers

    class Boom:
        def __init__(self, name, **kw):
            raise OSError("not cached")

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", Boom)
    monkeypatch.setattr(index, "_MODEL", None)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(OSError):
        index.embedder()                     # never falls through to a download


def test_one_backend_per_case(tmp_path, monkeypatch):
    from core.caseio import load_case
    monkeypatch.setenv("SEARCH_BACKEND", "bm25")
    case = load_case(os.path.join(ROOT, "cases", "santacruz"))
    assert index.make_backend(case) is index.make_backend(case)
    other = tmp_path / "other"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), other)
    be = index.make_backend(load_case(str(other)))
    assert be is not index.make_backend(case)
    be.index("tmpdoc", [{"chunk_id": "0", "clause": "1", "page": 1, "bbox": [],
                         "text": "canine handler stipend"}])
    hits = index.make_backend(load_case(str(other))).search("canine stipend",
                                                             doc_ids=["tmpdoc"], k=1)
    assert hits and hits[0]["doc_id"] == "tmpdoc"


def test_lifespan_warms_and_reports_ready(fake_model, case_copy):
    from fastapi.testclient import TestClient
    with TestClient(core_app.app) as c:
        deadline = time.time() + 5
        state = None
        while time.time() < deadline:
            state = c.get("/healthz").json()["retrieval"]
            if state == "ready":
                break
            time.sleep(0.05)
        assert state == "ready", warm.state()
        assert c.get("/api/case").json()["retrieval"] == "ready"
    assert fake_model.constructions and fake_model.constructions[0][1]["local_files_only"]


def test_warm_can_be_switched_off(fake_model, case_copy, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("KENNY_WARM", "0")
    with TestClient(core_app.app) as c:
        assert c.get("/healthz").json()["retrieval"] == "skipped"
    assert fake_model.constructions == []


def test_policy_question_after_warm_up_touches_no_network(case_copy, monkeypatch):
    """With the real cached model: once warm, a policy question must not open a
    connection to anything but loopback (the hub, in particular)."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with TestClient(core_app.app) as c:
        assert warm.wait(60)["state"] == "ready", warm.state()
        real = socket.create_connection

        def guarded(address, *a, **kw):
            host = address[0]
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise AssertionError(f"network call to {host} during a policy question")
            return real(address, *a, **kw)

        monkeypatch.setattr(socket, "create_connection", guarded)
        t0 = time.time()
        res = c.post("/chat", json={"prompt": "What does the Firefighters Local 3535 "
                                              "MOU say about overtime?"})
        assert res.status_code == 200 and res.json()["mode"] == "policy"
        assert time.time() - t0 < 5
