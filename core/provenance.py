"""Provenance helpers: content hashes for the things an answer depends on.

Pure functions, no app import (DEMO_TICKETS.md F1 step 1). An answer is replayable only
if every input it used can be named by a hash later: the roster file, the rule library,
the rule text itself, the source PDFs and the engine code. These helpers compute those
hashes one way, so the snapshot, the ledger and the back-fill script all agree.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from functools import lru_cache
from typing import Any

from .ruledsl import Rule

_HERE = os.path.dirname(os.path.abspath(__file__))
# The code that turns inputs into a number. A replay that matches under a different
# engine hash is still a match, but the drift is reported.
ENGINE_FILES = ("engine.py", "ruledsl.py", "governance.py")

# The fields that ARE the rule, in the order they are hashed. Status, approver,
# approved_at, stale_reason, scope_rank, _scenario and citation.doc_sha256 are
# deliberately excluded: the system rewrites those after approval, and the fingerprint
# must answer "is this the text the human approved?" not "has the bookkeeping moved?".
FINGERPRINT_FIELDS = ("id", "kind", "role", "result_type", "pay_basis", "topic", "when",
                      "compute", "set", "flags", "priority", "human_readable")
CITATION_FIELDS = ("doc_id", "clause", "page", "bbox")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def file_sha256(path: str | None) -> str | None:
    """SHA-256 of a file's bytes, or None when the file is missing."""
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=1)
def engine_sha256() -> str:
    """One hash over the engine's source files (cached: the code does not change while
    the process runs)."""
    h = hashlib.sha256()
    for name in ENGINE_FILES:
        with open(os.path.join(_HERE, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def rule_full_dict(rule: Rule) -> dict:
    """Every field of a Rule as plain data (flags and citation included)."""
    return dataclasses.asdict(rule)


def _normalised(rule_dict: dict) -> dict:
    """Run a dict through Rule.from_dict so defaults (role, pay_basis, citation shape)
    are filled in exactly as the engine would see them."""
    d = {k: v for k, v in rule_dict.items() if k != "_doc_id"}
    d.setdefault("status", "ratified")
    d.setdefault("approver", "fingerprint")
    rule = Rule.from_dict(d)
    full = rule_full_dict(rule)
    out = {k: full.get(k) for k in FINGERPRINT_FIELDS}
    cit = full.get("citation") or {}
    out["citation"] = {k: cit.get(k) for k in CITATION_FIELDS}
    return out


def rule_fingerprint(rule: Rule | dict) -> str:
    """SHA-256 of the canonical JSON of the rule's substantive fields.

    Accepts a Rule or its dict form; both normalise to the same bytes, so a fingerprint
    taken at approval time can be compared with the live library file later.
    """
    d = rule_full_dict(rule) if isinstance(rule, Rule) else rule
    return sha256_json(_normalised(d))
