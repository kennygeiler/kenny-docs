#!/usr/bin/env python
"""Skeptic tool CLI — the zero-spend bake path (DEMO_TICKETS.md G6).

Any harness that can run a shell command can drive the skeptic's three tools through
this script: a Claude Code session on the subscription, a headless `claude -p`, or a
person. Every result is persisted in the run file BEFORE it is printed, so the record is
what the tool returned, not what a shell output hook showed the agent.

    start   --rule ID --producer NAME [--operator NAME] [--model NAME]
    search  --run R --doc D --query Q
    page    --run R --doc D --page N
    engine  --run R --rule-set live|with_rule|variant --subjects L [L...] --hours H
            [--date-iso YYYY-MM-DD] [--holiday-weekday Sat] [--when EXPR] [--compute EXPR]
    submit  --run R (--json '<review>' | --file F)
    verify  (--rule ID | --all)          exit 1 on any mismatch
    repin   (--rule ID | --all)          verify, then stamp provenance.reverified with the
                                         current code rev; no model call, no new content;
                                         exit 1 (artifact untouched) on any mismatch
    show    --rule ID                    print the stored artifact and its provenance

Zero-spend guard: `start` exits 2 when ANTHROPIC_API_KEY is in the environment (override
with --allow-api-key, recorded in provenance). This script never imports core.app or
core.llm, so nothing here can reach a paid API.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import skeptic  # noqa: E402
from core.caseio import default_case_dir, load_case  # noqa: E402


def _out(data) -> None:
    print(json.dumps(data, separators=(",", ":"), default=str))


def _harness() -> str:
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unreported"
    except Exception:
        return "unreported"


def cmd_start(a) -> int:
    key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if key_present and not a.allow_api_key:
        print("refusing to start: ANTHROPIC_API_KEY is in the environment. The bake path "
              "must cost nothing — run with `env -u ANTHROPIC_API_KEY`, or pass "
              "--allow-api-key to record that a key was present.", file=sys.stderr)
        return 2
    case = load_case(a.case)
    rule = skeptic.find_rule(case, a.rule)
    if rule is None:
        print(f"no rule {a.rule!r} in the ratified library or the proposed queue",
              file=sys.stderr)
        return 1
    run = skeptic.Run.start(case, rule, producer=a.producer, operator=a.operator or "",
                            model=a.model or "unreported", api_key_in_env=key_present)
    cit = rule.get("citation") or {}
    cited = run.tool("read_page", doc_id=cit.get("doc_id"), page=int(cit.get("page") or 1))
    _out({"run_id": run.run_id, "rule": rule, "allowed_docs": skeptic.allowed_docs(case, rule),
          "cited_page": cited, "roster": [s.get("name") for s in case.subjects()],
          "known_facts": sorted(case.known_facts()), "limits": skeptic.LIMITS})
    return 0


def cmd_search(a) -> int:
    run = skeptic.Run.load(load_case(a.case), a.run)
    _out(run.tool("search_clauses", query=a.query, doc_id=a.doc))
    return 0


def cmd_page(a) -> int:
    run = skeptic.Run.load(load_case(a.case), a.run)
    _out(run.tool("read_page", doc_id=a.doc, page=a.page))
    return 0


def cmd_engine(a) -> int:
    run = skeptic.Run.load(load_case(a.case), a.run)
    params = {"hours": a.hours}
    if a.date_iso:
        params["date_iso"] = a.date_iso
    if a.holiday_weekday:
        params["holiday_weekday"] = a.holiday_weekday
    variant = None
    if a.when is not None or a.compute is not None:
        variant = {k: v for k, v in (("when", a.when), ("compute", a.compute)) if v is not None}
    _out(run.tool("run_engine", rule_set=a.rule_set,
                  scenario={"subjects": a.subjects, "params": params}, variant=variant))
    return 0


def cmd_submit(a) -> int:
    run = skeptic.Run.load(load_case(a.case), a.run)
    if a.file:
        with open(a.file) as f:
            review = json.load(f)
    else:
        review = json.loads(a.json)
    art = skeptic.submit(run, review, harness=_harness())
    _out({"artifact": skeptic.artifact_path(run.case, art["rule_id"]),
          "verdict": art["verdict"],
          "accepted": [{"id": w["id"], "severity": w["severity"], "kind": w["kind"]}
                       for w in art["warnings"]],
          "rejected": art["rejected"], "provenance": art["provenance"]})
    return 0


def cmd_verify(a) -> int:
    case = load_case(a.case)
    ids = list(skeptic.list_reviews(case).keys()) if a.all else [a.rule]
    if not ids:
        print("no review artifacts found", file=sys.stderr)
        return 1
    rc = 0
    for rid in ids:
        problems = skeptic.verify(case, rid)
        art = skeptic.load_review(case, rid) or {}
        _out({"rule_id": rid, "ok": not problems, "problems": problems,
              "provenance": art.get("provenance"), "inputs": art.get("inputs")})
        if problems:
            rc = 1
    return rc


def cmd_repin(a) -> int:
    case = load_case(a.case)
    ids = list(skeptic.list_reviews(case).keys()) if a.all else [a.rule]
    if not ids:
        print("no review artifacts found", file=sys.stderr)
        return 1
    rc = 0
    harness = _harness()
    code_rev = skeptic._code_rev()      # read once, before the first stamp dirties the tree
    for rid in ids:
        res = skeptic.reverify(case, rid, harness=harness, code_rev=code_rev)
        _out(res)
        if not res["ok"]:
            rc = 1
    return rc


def cmd_show(a) -> int:
    art = skeptic.load_review(load_case(a.case), a.rule)
    if art is None:
        print(f"no review artifact for {a.rule}", file=sys.stderr)
        return 1
    _out(art)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--case", default=os.environ.get("CASE") or default_case_dir())
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--rule", required=True)
    s.add_argument("--producer", required=True)
    s.add_argument("--operator", default="")
    s.add_argument("--model", default="unreported")
    s.add_argument("--allow-api-key", action="store_true")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("search")
    s.add_argument("--run", required=True)
    s.add_argument("--doc", required=True)
    s.add_argument("--query", required=True)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("page")
    s.add_argument("--run", required=True)
    s.add_argument("--doc", required=True)
    s.add_argument("--page", type=int, required=True)
    s.set_defaults(fn=cmd_page)

    s = sub.add_parser("engine")
    s.add_argument("--run", required=True)
    s.add_argument("--rule-set", default="live", choices=["live", "with_rule", "variant"])
    s.add_argument("--subjects", nargs="+", required=True)
    s.add_argument("--hours", type=float, required=True)
    s.add_argument("--date-iso")
    s.add_argument("--holiday-weekday")
    s.add_argument("--when")
    s.add_argument("--compute")
    s.set_defaults(fn=cmd_engine)

    s = sub.add_parser("submit")
    s.add_argument("--run", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--json")
    g.add_argument("--file")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("verify")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--rule")
    g.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("repin")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--rule")
    g.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_repin)

    s = sub.add_parser("show")
    s.add_argument("--rule", required=True)
    s.set_defaults(fn=cmd_show)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
