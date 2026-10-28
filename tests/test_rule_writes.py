"""Rule-file writes are atomic and serialised (audit 2026-10-28).

rules_proposed.json / rules_ratified.json used to be written with open(path, 'w'): a
crash mid-dump left a truncated library, and two concurrent read-modify-writes (ratify,
draft, demo-draft load, citation revalidation) could each drop the other's rules.
"""
import json
import os
import shutil
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app as core_app  # noqa: E402
from core.services import rulestore as core_rulestore  # noqa: E402
from core.services import state as core_state  # noqa: E402
from core.routes import rules as rules_routes  # noqa: E402


@pytest.fixture
def case(tmp_path, monkeypatch):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cases", "santacruz")
    dst = tmp_path / "santacruz"
    shutil.copytree(src, dst)
    monkeypatch.setattr(core_state, "CASE_DIR", str(dst))
    return core_state._case()


def test_failed_write_leaves_previous_library_intact(case, monkeypatch):
    path = case.path("proposed_rules", "rules/rules_proposed.json")
    before = open(path).read()

    with pytest.raises(Exception):
        # json.dump fails partway (an unserialisable value after some output)
        core_rulestore._write_proposed(case, [{"id": "x", "bad": object()}])
    assert open(path).read() == before
    leftovers = [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")]
    assert leftovers == []


def test_write_ratified_is_atomic_replace(case):
    path = case.path("rules")
    ino_before = os.stat(path).st_ino
    rules = core_rulestore._raw_ratified(case)
    core_rulestore._write_ratified(case, rules)
    assert json.load(open(path))["rules"] == rules
    assert os.stat(path).st_ino != ino_before        # replaced, not truncated in place


def test_writers_hold_the_rules_lock(case, monkeypatch):
    seen = []
    real = core_rulestore._atomic_json_write

    def spy(path, obj):
        seen.append(core_rulestore._RULES_LOCK.locked())
        return real(path, obj)

    monkeypatch.setattr(core_rulestore, "_atomic_json_write", spy)
    from fastapi.testclient import TestClient
    c = TestClient(core_app.app)
    q = case.path("proposed_rules", "rules/rules_proposed.json")
    data = json.load(open(q))
    data["rules"] = [r for r in data["rules"]
                     if r.get("id") not in {d["id"] for d in core_rulestore._demo_drafts(case)}]
    json.dump(data, open(q, "w"))
    assert c.post("/admin/demo_draft/load").status_code == 200
    assert seen and all(seen)

    monkeypatch.setattr(rules_routes, "_ratify",
                        lambda body: {"locked": core_rulestore._RULES_LOCK.locked()})
    assert c.post("/admin/ratify", json={"approver": "t"}).json() == {"locked": True}
    assert not core_rulestore._RULES_LOCK.locked()


def test_concurrent_queue_merges_do_not_lose_rules(case):
    """Each thread adds its own rule to the queue via read-modify-write under the lock."""
    def add(i):
        with core_rulestore._RULES_LOCK:
            cur = rules_routes.admin_proposed()
            core_rulestore._write_proposed(case, cur["rules"] + [{"id": f"t{i}"}],
                                     cur.get("needs_data"))

    ts = [threading.Thread(target=add, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ids = {r.get("id") for r in rules_routes.admin_proposed()["rules"]}
    assert {f"t{i}" for i in range(12)} <= ids
