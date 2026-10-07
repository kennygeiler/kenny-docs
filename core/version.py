"""Which commit is this process running? (DEMO_TICKETS.md K1)

The header of both surfaces shows a build hash so an audience, or the owner six weeks
later, can tie what they are looking at to a commit. Resolution order, first hit wins:

  1. KENNY_BUILD            — written into the image by the Dockerfile (`--build-arg
                              KENNY_BUILD=$(git rev-parse --short HEAD)`); .dockerignore
                              excludes .git/, so the image cannot ask git itself.
  2. RAILWAY_GIT_COMMIT_SHA — injected by Railway when the service is connected to GitHub.
  3. git                    — `git rev-parse --short HEAD` + `git status --porcelain` run
                              once at import with a 2 s timeout, cwd = repo root. This is
                              the laptop path and the only one that can say "dirty".
  4. "unknown"              — any failure above. The endpoint still answers 200.

Computed once at import: a build does not change while the process runs, and a
subprocess per /api/case call would be a silly cost for a constant.
"""
from __future__ import annotations

import os
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _from_git(root: str = _ROOT) -> dict | None:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=2)
        if rev.returncode != 0 or not rev.stdout.strip():
            return None
        st = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=2)
        dirty = st.returncode == 0 and bool(st.stdout.strip())
        return {"sha": rev.stdout.strip(), "dirty": dirty, "source": "git"}
    except Exception:
        return None


def compute(environ=None, root: str = _ROOT) -> dict:
    """{"sha": str, "dirty": bool, "source": "env" | "git" | "unknown"}. Pure function
    of the environment and the git tree so the tests can drive every branch."""
    env = os.environ if environ is None else environ
    for var in ("KENNY_BUILD", "RAILWAY_GIT_COMMIT_SHA"):
        val = (env.get(var) or "").strip()
        if val and val.lower() != "unknown":
            return {"sha": val[:12], "dirty": False, "source": "env"}
    got = _from_git(root)
    if got:
        return got
    return {"sha": "unknown", "dirty": False, "source": "unknown"}


_VERSION = compute()


def running_version() -> dict:
    """The value /api/case reports; computed once at import (see module docstring)."""
    return dict(_VERSION)


def label(v: dict | None = None) -> str:
    """'build 9129f7d', 'build 9129f7d +uncommitted' or 'build unknown' — the exact
    text the page footers show, kept here so the test and the UI cannot drift."""
    v = v or _VERSION
    return "build " + v["sha"] + (" +uncommitted" if v.get("dirty") else "")
