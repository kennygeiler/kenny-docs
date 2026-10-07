"""F6 — the Audit tab's data: history rows carry ts, result type and outcome; the ledger
response carries count, head and signing status."""
import os
import shutil

import pytest

import core.app as core_app
from core import ledger as ledger_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(ledger_mod.KEY_ENV, raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def test_history_rows_and_ledger_header(client):
    c = client.post("/chat", json={"prompt": "Cost an 8-hour overtime shift for a "
                                             "Firefighter/Paramedic (56 hr, top step)"}).json()
    assert c["mode"] == "costing"
    p = client.post("/chat", json={"prompt": "What does the Firefighters Local 3535 MOU "
                                             "say about overtime?"}).json()
    rows = client.get("/admin/history").json()["history"]
    by_qid = {r["query_id"]: r for r in rows}
    cost = by_qid[c["query_id"]]
    assert isinstance(cost["ts"], float)
    assert cost["result_type"] == "currency" and cost["outcome"] == "computed"
    assert cost["total"] == 640.8 and cost["events"] > 5
    pol = by_qid[p["query_id"]]
    assert pol["total"] is None
    assert pol["outcome"] in ("quoted", "asked back"), pol
    assert rows[0]["query_id"] == p["query_id"]       # newest first, by ts

    led = client.get("/admin/ledger").json()
    assert led["verified"] is True
    assert led["count"] == len(led["events"])
    assert led["head"]["hash"] == led["events"][-1]["hash"]
    assert led["head"]["seq"] == led["events"][-1]["seq"]
    assert led["keyed"] is False
