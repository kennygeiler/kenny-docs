"""Back-fill `inputs` on the five shipped live rules (DEMO_TICKETS.md L2).

Additive and idempotent: only the `inputs` key is written, every other byte of
cases/santacruz/rules/rules_ratified.json is left as it was (the file is json.dumps
indent=2 with no trailing newline; tests/test_decision_tree.py::test_backfill_is_additive
strips the inputs and checks this script restores the shipped bytes exactly).

Each clause input names the page + bbox from the shipped catalog (verified by the
BM25/hybrid sub-search at chat time: the chosen clause shows its search rank, or a
'declared citation not found by search' flag — never hidden). Roster and question
inputs are leaves with the value the engine used.

    python scripts/backfill_rule_inputs.py [path/to/rules_ratified.json]
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, "cases", "santacruz", "rules", "rules_ratified.json")
FF = "firefighters_local3535_mou"

INPUTS: dict[str, list[dict]] = {
    "firefighters_local3535_mou:overtime_premium_rate": [
        {"name": "multiplier", "kind": "multiplier", "source": "clause",
         "query": "premium overtime compensation at the rate of one and one half times regular rate of pay",
         "citation": {"doc_id": FF, "page": 8, "bbox": [141.418, 505.662, 511.182, 441.459],
                      "clause": "Overtime Rate (p.8)"}},
        {"name": "regular_rate_definition", "kind": "definition", "source": "clause",
         "query": "regular rate of pay definition overtime",
         "citation": {"doc_id": FF, "page": 8, "bbox": [142.138, 655.867, 513.71, 552.405],
                      "clause": "Regular Rate of Pay (p.8)"}},
        {"name": "base_hourly", "kind": "fact", "source": "roster", "field": "base_hourly",
         "query": "salary schedule appendix A",
         "citation": {"doc_id": FF, "page": 6, "bbox": [110.88, 526.566, 510.48, 489.331],
                      "clause": "Salary Schedule set forth in Appendix A (p.6)"}},
        {"name": "hours", "kind": "fact", "source": "question"},
    ],
    "firefighters_local3535_mou:longevity_10yr": [
        {"name": "longevity", "kind": "multiplier", "source": "clause",
         "query": "longevity pay ten years of service",
         "citation": {"doc_id": FF, "page": 12, "bbox": [143.606, 263.49, 511.56, 212.751],
                      "clause": "Longevity Pay (p.12)"}},
        {"name": "years_of_service", "kind": "fact", "source": "question"},
    ],
    "firefighters_local3535_mou:bereavement_shifts": [
        {"name": "entitlement", "kind": "definition", "source": "clause",
         "query": "bereavement leave death immediate family",
         "citation": {"doc_id": FF, "page": 21, "bbox": [107.28, 189.498, 505.259, 167.118],
                      "clause": "XIV"}},
    ],
    "chief_officers_mou:bereavement_hours": [
        {"name": "entitlement", "kind": "definition", "source": "clause",
         "query": "bereavement leave death immediate family",
         "citation": {"doc_id": "chief_officers_mou", "page": 14,
                      "bbox": [90.7, 575.8, 526.7, 552.8], "clause": "Bereavement Leave (p.14)"}},
    ],
    "admin_group_mou:overtime_premium_rate": [
        {"name": "multiplier", "kind": "multiplier", "source": "clause",
         "query": "one and one-half times regular rate of pay hours worked in excess",
         "citation": {"doc_id": "admin_group_mou", "page": 6,
                      "bbox": [104.4, 137.742, 509.597, 113.838], "clause": "Overtime (p.6)"}},
        {"name": "base_hourly", "kind": "fact", "source": "roster", "field": "base_hourly"},
        {"name": "hours", "kind": "fact", "source": "question"},
    ],
}


def backfill(path: str) -> bool:
    """Write inputs for every known rule id. Returns True when the file changed."""
    with open(path) as f:
        raw = f.read()
    data = json.loads(raw)
    rules = data["rules"] if isinstance(data, dict) else data
    for r in rules:
        decl = INPUTS.get(r.get("id"))
        if decl is not None and r.get("inputs") != decl:
            r["inputs"] = json.loads(json.dumps(decl))
    out = json.dumps(data, indent=2)
    if out == raw:
        return False
    with open(path, "w") as f:
        f.write(out)
    return True


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    changed = backfill(target)
    print(f"{'updated' if changed else 'unchanged'}: {target}")
