"""Load the repo's .env into the process environment (real variables always win).

Called once by core/services/state.py before anything reads CASE or the upload cap.
"""
from __future__ import annotations

import os


def _load_dotenv(force: bool = False) -> None:
    """Load the repo's .env at startup so the server works with a plain `uvicorn`
    command — no --env-file flag required. Real environment variables always win.
    Skipped under pytest (and when KENNY_NO_DOTENV is set): this runs at import, during
    test collection, which is exactly how a developer's real ANTHROPIC_API_KEY used to
    leak into the suite (DEMO_TICKETS K2; tests/conftest.py is the other half).
    `force` is for tests of the loader itself: it skips that guard, nothing else."""
    import sys
    if not force and (os.environ.get("KENNY_NO_DOTENV") or "pytest" in sys.modules):
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and k not in os.environ:   # an explicitly EMPTY real variable still wins
            os.environ[k] = v
