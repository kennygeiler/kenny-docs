"""Drafted rule identifiers reach admin-page DOM attributes, so validate_rules only
accepts slug ids and markup-free clause labels (audit 2026-10-28; the UI escapes too)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ruledsl import validate_rules  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rule(**over):
    r = {"id": "fire_mou:holiday_premium", "kind": "modifier", "result_type": "currency",
         "when": "True", "set": {"premium": "1"},
         "citation": {"doc_id": "fire_mou", "clause": "Overtime Rate (p.8)", "page": 8}}
    r.update(over)
    return r


def _errs(r):
    return " ".join(validate_rules([r], set()).get(str(r.get("id") or "<no id>"), []))


def test_clean_rule_has_no_id_errors():
    assert "must match" not in _errs(_rule())
    assert "markup" not in _errs(_rule())


@pytest.mark.parametrize("bad", ['x"><img src=x onerror=alert(1)>', "x');alert(1);//",
                                 "Upper:Case", "has space", ""])
def test_unsafe_rule_id_rejected(bad):
    assert "must match" in _errs(_rule(id=bad))


@pytest.mark.parametrize("bad", ['9.1"><script>', "9.1' onmouseover='x", "a`b", "a\\b",
                                 "line\nbreak"])
def test_unsafe_citation_clause_rejected(bad):
    r = _rule(citation={"doc_id": "fire_mou", "clause": bad, "page": 1})
    assert "markup" in _errs(r)


def test_unsafe_citation_doc_id_rejected():
    r = _rule(citation={"doc_id": "../x<y", "clause": "9.1", "page": 1})
    assert "citation doc_id" in _errs(r)


def test_shipped_rules_still_pass_id_checks():
    for name in ("rules_ratified.json", "rules_proposed.json", "demo_wrong_draft.json"):
        rules = json.load(open(os.path.join(ROOT, "cases", "santacruz", "rules", name)))
        for r in rules.get("rules", []):
            errs = " ".join(validate_rules([r], set()).get(r["id"], []))
            assert "must match" not in errs and "markup" not in errs, (name, r["id"])
