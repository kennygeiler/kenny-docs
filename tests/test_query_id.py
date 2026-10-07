"""Server-issued query ids; snapshots are write-once and cannot leave the snapshots
folder (DEMO_TICKETS.md A1).

Before: POST /chat took query_id from the body and used it as the snapshot filename —
'../rules/rules_ratified' replaced the rule library with a snapshot, and a re-used id
rewrote a 'frozen' record. Now every answer gets a server-minted id; a client id is only
a reference that threads a follow-up back to the query it continues.
"""
import hashlib
import json
import os
import re
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core import audit  # noqa: E402
from core.ruledsl import Citation, Rule  # noqa: E402

GOLDEN = ("What does an 8-hour overtime shift cost for a "
          "Firefighter/Paramedic (56 hr, top step)?")
HALF = ("What does a 4-hour overtime shift cost for a "
        "Firefighter/Paramedic (56 hr, top step)?")
QID = re.compile(r"^[0-9a-f]{12}$")


@pytest.fixture
def client(tmp_path, monkeypatch):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cases", "santacruz")
    case = tmp_path / "santacruz"
    # Local (gitignored) snapshots/ledger must not leak into the copy: the
    # assertions below count snapshot files.
    shutil.copytree(src, case, ignore=shutil.ignore_patterns("*.json.bak", "ledger.jsonl"))
    for f in os.listdir(case / "snapshots") if (case / "snapshots").exists() else []:
        if f.endswith(".json"):
            os.remove(case / "snapshots" / f)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    return TestClient(core_app.app), str(case)


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _snapshots(case):
    d = os.path.join(case, "snapshots")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def test_client_id_cannot_pick_the_file(client, tmp_path):
    c, case = client
    rules = os.path.join(case, "rules", "rules_ratified.json")
    before = _sha(rules)
    res = c.post("/chat", json={"prompt": GOLDEN, "query_id": "../rules/rules_ratified"})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "costing" and body["result"]["total"] == 640.8
    assert QID.match(body["query_id"]), body["query_id"]
    assert _sha(rules) == before                       # library untouched
    assert _snapshots(case) == [f"{body['query_id']}.json"]
    # the library still answers afterwards (it used to 500 from here on)
    again = c.post("/chat", json={"prompt": GOLDEN}).json()
    assert again["result"]["total"] == 640.8
    # an id aimed outside the case writes nothing outside snapshots/
    outside = tmp_path / "x.json"
    res = c.post("/chat", json={"prompt": GOLDEN, "query_id": "../../../../tmp/x"})
    assert res.status_code == 200 and QID.match(res.json()["query_id"])
    assert not outside.exists()
    assert len(_snapshots(case)) == 3
    assert c.get("/healthz").status_code == 200


def test_reused_id_never_rewrites(client):
    c, case = client
    id1 = c.post("/chat", json={"prompt": GOLDEN}).json()["query_id"]
    snap1 = os.path.join(case, "snapshots", f"{id1}.json")
    bytes1 = open(snap1, "rb").read()
    r2 = c.post("/chat", json={"prompt": HALF, "query_id": id1}).json()
    id2 = r2["query_id"]
    assert id2 != id1 and QID.match(id2)
    assert r2["result"]["total"] == 320.4
    assert open(snap1, "rb").read() == bytes1              # frozen stays frozen
    trail = c.get(f"/chat/audit/{id1}").json()
    types = [e["type"] for e in trail["events"]]
    assert types.count("chat.prompt") == 1
    snaps = [e for e in trail["events"] if e["type"] == "answer.snapshot"]
    assert len(snaps) == 1 and snaps[0]["payload"]["total"] == 640.8


def test_followup_keeps_the_trail(client):
    c, _ = client
    first = c.post("/chat", json={"prompt": "What does the MOU say about overtime?"}).json()
    assert first["mode"] == "clarify", first          # "which department?"
    id1 = first["query_id"]
    second = c.post("/chat", json={"prompt": first["prompt_echo"], "query_id": id1,
                                   "department": "fire"}).json()
    id2 = second["query_id"]
    assert id2 != id1
    trail = c.get(f"/chat/audit/{id2}").json()
    assert trail["continues"] == [id1]
    ids = [e["query_id"] for e in trail["events"]]
    assert ids[0] == id1 and ids[-1] == id2
    assert ids.index(id2) == len([i for i in ids if i == id1])   # parents first
    # the follow-up's own prompt event records the link
    own = [e for e in trail["events"] if e["query_id"] == id2 and e["type"] == "chat.prompt"]
    assert own and own[0]["payload"]["continues"] == id1
    # an unknown or malformed reference is simply ignored
    third = c.post("/chat", json={"prompt": GOLDEN, "query_id": "zzzzzzzzzzzz"}).json()
    assert c.get(f"/chat/audit/{third['query_id']}").json()["continues"] == []


def test_audit_rejects_malformed_ids(client):
    c, _ = client
    assert c.get("/chat/audit/../rules/x").status_code in (404, 422)
    assert c.get("/chat/audit/not-an-id").status_code == 404


def test_snapshot_is_write_once(tmp_path):
    snaps = str(tmp_path / "snapshots")
    rule = Rule(id="r", kind="selector", when="True", compute="1",
                citation=Citation(doc_id="d"))
    first = audit.snapshot(snaps, "abcdefabcdef", {"hours": 8}, [rule], {"total": 1})
    bytes1 = open(first, "rb").read()
    # A second write under the same id never touches the frozen file: since wave1/ledger
    # (F1) it lands in a suffixed sibling the ledger event names, instead of raising.
    second = audit.snapshot(snaps, "abcdefabcdef", {"hours": 4}, [rule], {"total": 2})
    assert second != first and re.match(r"^abcdefabcdef-[0-9a-f]{6}\.json$",
                                        os.path.basename(second))
    assert open(first, "rb").read() == bytes1
    with pytest.raises(ValueError):
        audit.snapshot(snaps, "../x", {}, [rule], {})
    with pytest.raises(ValueError):
        audit.snapshot(snaps, "ABCDEFABCDEF", {}, [rule], {})
    assert set(os.listdir(snaps)) == {"abcdefabcdef.json", os.path.basename(second)}
    frozen = json.load(open(os.path.join(snaps, "abcdefabcdef.json")))
    assert frozen["params"] == {"hours": 8}
    assert "quote_sha256" in frozen["rule_versions"][0]["citation"]


def test_healthz_flags_broken_library(client):
    c, case = client
    assert c.get("/healthz").status_code == 200
    with open(os.path.join(case, "rules", "rules_ratified.json"), "w") as f:
        json.dump({"query_id": 1}, f)
    res = c.get("/healthz")
    assert res.status_code == 503
    assert res.json()["status"] == "degraded" and res.json()["library"] == "broken"


def test_followup_with_confirmed_document_still_opens_with_a_prompt(client):
    """A doc_id follow-up used to skip chat.prompt entirely, so its trail began
    mid-way. Every query now starts with its own prompt event."""
    c, _ = client
    r = c.post("/chat", json={"prompt": GOLDEN, "doc_id": "firefighters_local3535_mou"}).json()
    trail = c.get(f"/chat/audit/{r['query_id']}").json()
    assert trail["events"][0]["type"] == "chat.prompt"
    assert trail["events"][0]["payload"]["doc_id"] == "firefighters_local3535_mou"
