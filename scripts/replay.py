#!/usr/bin/env python
"""Replay costing answers from their frozen snapshots (DEMO_TICKETS.md F1 step 7).

    python scripts/replay.py <case_dir> <query_id>
    python scripts/replay.py <case_dir> --all

Exit 1 on any mismatch. Snapshots that predate input capture (schema 1) are reported
as not_replayable and do not fail the run.
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
    ap.add_argument("query_id", nargs="?")
    ap.add_argument("--all", action="store_true", help="replay every snapshotted answer")
    args = ap.parse_args(argv)
    if not args.query_id and not args.all:
        ap.error("give a query_id or --all")
    os.environ.pop("ANTHROPIC_API_KEY", None)

    from core import audit
    from core.caseio import load_case

    case = load_case(os.path.abspath(args.case_dir))
    led = case.ledger()
    if args.all:
        qids = sorted({e.get("query_id") for e in led.read()
                       if e.get("type") == "answer.snapshot" and e.get("query_id")
                       and (e.get("payload") or {}).get("snapshot")})
    else:
        qids = [args.query_id]
    bad = 0
    for qid in qids:
        rep = audit.replay(case, led, qid)
        status = rep["status"]
        if status == "match":
            print(f"{qid}: match  total {rep['recomputed']['total']}  "
                  f"result {rep['recomputed']['result_sha256'][:16]}")
        elif status == "mismatch":
            bad += 1
            failing = [c["name"] for c in rep["checks"] if not c["ok"]]
            print(f"{qid}: MISMATCH  failing checks: {', '.join(failing)}")
        else:
            print(f"{qid}: not replayable ({rep.get('reason')})")
    print(f"{len(qids)} answer(s), {bad} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
