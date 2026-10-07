"""The demo path, asserted through HTTP (DEMO_TICKETS.md K9), and the running commit
shown by the app (K1).

scripts/demo_smoke.py is the single table of what the demo must show; this file runs
the same rows under pytest so a wave that changes chat, the gate or the ledger turns
the row red here before it turns red on stage. One scratch copy of cases/santacruz
serves the whole module (the rows are read-only after the run), so the suite pays the
embedding-model load once.
"""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import app as core_app  # noqa: E402
from core import auth, version  # noqa: E402
from scripts import demo_smoke  # noqa: E402


# --------------------------------------------------------------------------- #
# K9: every demo row, once per module
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def smoke_rows(tmp_path_factory):
    case = tmp_path_factory.mktemp("smoke") / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    saved_dir, saved_allow = core_app.CASE_DIR, auth.RateLimiter.allow
    core_app.CASE_DIR = str(case)
    auth.RateLimiter.allow = lambda self, key: True
    try:
        from fastapi.testclient import TestClient
        client = demo_smoke.TestClientAdapter(TestClient(core_app.app))
        rows = demo_smoke.run_all(client)
    finally:
        core_app.CASE_DIR, auth.RateLimiter.allow = saved_dir, saved_allow
    return {r["id"]: r for r in rows}


@pytest.mark.parametrize("check_id", [c[0] for c in demo_smoke.CHECKS])
def test_demo_row(smoke_rows, check_id):
    row = smoke_rows[check_id]
    assert row["ok"], f"{check_id}: observed {row['observed']} — expected {row['expected']}"


def test_smoke_table_covers_every_demo_question(smoke_rows):
    # The six questions the wave-1 merge report named, plus the four proof screens.
    assert len(smoke_rows) == len(demo_smoke.CHECKS) == 11
    assert "MISSED" not in demo_smoke.format_table(list(smoke_rows.values()))


def test_smoke_script_reports_a_miss_as_non_zero(capsys):
    """The CLI must fail loudly: a row that misses is an exit code, not a line to read."""
    class Dead:
        def get(self, path): return 500, None
        def post(self, path, body): return 500, None
    rows = demo_smoke.run_all(Dead())
    assert rows and not any(r["ok"] for r in rows)
    table = demo_smoke.format_table(rows)
    assert "MISSED" in table and "expected:" in table
    assert "0 of 11 demo checks pass" in table


def test_smoke_script_never_touches_the_shipped_case(monkeypatch):
    """scratch_client copies the case; the repo's own ledger and snapshots stay put."""
    real = os.path.join(ROOT, "cases", "santacruz")
    before = sorted(os.listdir(os.path.join(real, "snapshots"))) if os.path.isdir(
        os.path.join(real, "snapshots")) else None
    monkeypatch.setattr(core_app, "CASE_DIR", core_app.CASE_DIR)
    monkeypatch.setattr(auth.RateLimiter, "allow", auth.RateLimiter.allow)
    client, case = demo_smoke.scratch_client()
    assert case != real and os.path.isfile(os.path.join(case, "case.yaml"))
    assert core_app.CASE_DIR == case
    after = sorted(os.listdir(os.path.join(real, "snapshots"))) if before is not None else None
    assert after == before


# --------------------------------------------------------------------------- #
# K1: the app shows the commit it runs
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    case = tmp_path / "santacruz"
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    monkeypatch.setattr(core_app, "CASE_DIR", str(case))
    from fastapi.testclient import TestClient
    return TestClient(core_app.app)


def test_env_build_wins_over_git():
    v = version.compute({"KENNY_BUILD": "abc1234"})
    assert v == {"sha": "abc1234", "dirty": False, "source": "env"}
    v = version.compute({"KENNY_BUILD": "", "RAILWAY_GIT_COMMIT_SHA": "0123456789abcdef0123"})
    assert v == {"sha": "0123456789ab", "dirty": False, "source": "env"}
    # the Dockerfile's default ARG must not masquerade as a real build
    assert version.compute({"KENNY_BUILD": "unknown"}, root=os.devnull)["source"] == "unknown"


def test_git_failure_degrades_to_unknown(monkeypatch):
    def boom(*a, **k):
        raise OSError("no git here")
    monkeypatch.setattr(subprocess, "run", boom)
    assert version.compute({}) == {"sha": "unknown", "dirty": False, "source": "unknown"}


def test_git_path_reports_sha_and_dirty(monkeypatch):
    class R:
        def __init__(self, out, rc=0): self.stdout, self.returncode = out, rc
    outs = {"rev-parse": R("9129f7d\n"), "status": R(" M core/app.py\n")}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: outs[cmd[1]])
    assert version.compute({}) == {"sha": "9129f7d", "dirty": True, "source": "git"}
    outs["status"] = R("")
    assert version.compute({}) == {"sha": "9129f7d", "dirty": False, "source": "git"}


def test_label_is_the_footer_text():
    assert version.label({"sha": "9129f7d", "dirty": False}) == "build 9129f7d"
    assert version.label({"sha": "9129f7d", "dirty": True}) == "build 9129f7d +uncommitted"
    assert version.label({"sha": "unknown", "dirty": False}) == "build unknown"


def test_api_case_carries_the_version(client, monkeypatch):
    monkeypatch.setattr(version, "_VERSION", {"sha": "abc1234", "dirty": False, "source": "env"})
    v = client.get("/api/case").json()["version"]
    assert v == {"sha": "abc1234", "dirty": False, "source": "env"}
    # the real value is whatever this checkout computed; it is always well-formed
    monkeypatch.undo()
    v = client.get("/api/case").json()["version"]
    assert set(v) == {"sha", "dirty", "source"} and v["sha"]
    assert v["source"] in ("git", "env", "unknown")


def test_both_pages_show_the_build_tag(client):
    for path in ("/", "/admin"):
        html = client.get(path).text
        assert 'id="buildTag"' in html, path
    # the pages fill it from /api/case with the same text version.label() produces
    js = client.get("/static/app.js").text
    assert "'build ' + CASE.version.sha" in js and "+uncommitted" in js
    assert "'build ' + c.version.sha" in client.get("/admin").text
