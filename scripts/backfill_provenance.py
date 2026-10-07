#!/usr/bin/env python
"""Back-fill approval provenance into a case's ledger (DEMO_TICKETS.md F2).

    python scripts/backfill_provenance.py <case_dir> --approvals [--dry-run]

For every live rule whose current fingerprint has no full `authoring.ratify` event,
append one with actor 'system' and backfilled: true. The approver and time are copied
from rules_ratified.json; the rule text, clause and known answers are as they stand NOW,
and the event says so. Idempotent: a second run appends nothing. The rule file is never
written.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_dir")
    ap.add_argument("--approvals", action="store_true",
                    help="back-fill authoring.ratify events for the live rules")
    ap.add_argument("--dry-run", action="store_true", help="report, append nothing")
    args = ap.parse_args(argv)
    if not args.approvals:
        ap.error("nothing to do: pass --approvals")
    os.environ.pop("ANTHROPIC_API_KEY", None)   # no model call is ever needed here

    from core import approvals
    from core.caseio import load_case

    case = load_case(os.path.abspath(args.case_dir))
    res = approvals.backfill(case, dry_run=args.dry_run)
    verb = "would append" if args.dry_run else "appended"
    for rid in res["appended"]:
        print(f"{verb} authoring.ratify (backfilled) for {rid}")
    for rid in res["skipped"]:
        print(f"already recorded: {rid}")
    if not args.dry_run:
        ok, msg = case.ledger().verify()
        print(f"ledger verify: {ok} ({msg})")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
