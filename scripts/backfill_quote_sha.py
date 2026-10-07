"""Bind the shipped rule library to the text it cites (DEMO_TICKETS.md A2, step 6).

For every ratified rule whose citation has no `quote_sha256`, locate the cited clause
in the catalog by box/page (core/evidence.py) and record the clause text's hash plus
the first 200 characters of the quote. Idempotent: a second run changes nothing, and
no other key of any rule is touched (tests/test_revalidate.py proves both). Unlike
the app's own revalidation it writes no .bak and appends nothing to the ledger —
this is a data migration, not an event.

    python scripts/backfill_quote_sha.py [cases/santacruz]
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import evidence  # noqa: E402
from core.caseio import load_case  # noqa: E402
from core.catalog import Catalog  # noqa: E402


def run(case_dir: str) -> int:
    case = load_case(case_dir)
    cat = Catalog(case.path("catalog", "catalog.json"))
    path = case.path("rules")
    with open(path) as f:
        data = json.load(f)
    changed = 0
    for r in data.get("rules", []):
        if r.get("status") != "ratified":
            continue
        cit = r.get("citation") or {}
        if cit.get("quote_sha256"):
            continue
        found, how = evidence.locate(cat.clauses(cit.get("doc_id", "")), cit)
        if found is None or not found.get("text"):
            print(f"  {r.get('id')}: not located ({how}) — left alone")
            continue
        cit["quote_sha256"] = evidence.text_sha(found["text"])
        cit["quote"] = evidence.quote_of(found)
        r["citation"] = cit
        changed += 1
        print(f"  {r.get('id')}: bound by {how} -> {cit['quote_sha256']}")
    if changed:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    return changed


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "cases", "santacruz")
    n = run(target)
    print(f"{n} citation(s) bound in {target}")
