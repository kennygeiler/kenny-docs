#!/usr/bin/env python
"""Re-ask every question the demo depends on and check the number, the document and
the page (DEMO_TICKETS.md K9). One command, no model, no key, exits non-zero on any miss.

    python scripts/demo_smoke.py                      # TestClient on a scratch copy of
                                                      # cases/santacruz (nothing written
                                                      # to the real case)
    python scripts/demo_smoke.py --url http://127.0.0.1:8495   # a running server

tests/test_demo_smoke.py runs the same CHECKS under pytest, so the table below is the
single statement of what the demo must show. Add a row when a demo beat is added.

Each check is (id, what, run) where run(client, ctx) returns (ok, observed, expected).
`ctx` carries state between rows (the first costing answer's query_id feeds the replay
row). Observed strings are what a person reads in the table; keep them short.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

Q_OVERTIME = "Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step)"
Q_LONGEVITY = Q_OVERTIME + " with 12 years of service"
Q_BEREAVEMENT = "How much bereavement leave does a firefighter get?"
Q_OFF_CORPUS = "What is the heat pump rebate for a 3 ton system?"
Q_REGULAR = "Cost an 8-hour regular shift for a Firefighter/Paramedic (56 hr, top step)"
Q_MIXED = Q_OVERTIME + " and a Fire Marshal (top step)"

L3535 = "firefighters_local3535_mou"


# --------------------------------------------------------------------------- #
# clients: TestClient on a scratch case, or a live URL
# --------------------------------------------------------------------------- #
class HttpClient:
    """The two calls the checks make, against a running server. No auth: the demo
    posture is open; a locked deploy would need a cookie here."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:  # pragma: no cover - live server only
            return e.code, json.loads(e.read().decode() or "null")

    def get(self, path: str):
        return self._req("GET", path)

    def post(self, path: str, body: dict):
        return self._req("POST", path, body)


class TestClientAdapter:
    """Same two calls over fastapi's TestClient, so the checks read one shape."""

    def __init__(self, tc):
        self.tc = tc

    def get(self, path: str):
        r = self.tc.get(path)
        return r.status_code, _json(r)

    def post(self, path: str, body: dict):
        r = self.tc.post(path, json=body)
        return r.status_code, _json(r)


def _json(r):
    try:
        return r.json()
    except Exception:
        return None


def scratch_client():
    """TestClient on a copy of cases/santacruz under a temp dir: the smoke run must
    never append to the real ledger or write a snapshot into the repo."""
    os.environ.setdefault("KENNY_NO_DOTENV", "1")
    os.environ.pop("ANTHROPIC_API_KEY", None)
    sys.path.insert(0, ROOT)
    from core import app as core_app
    from core import auth
    tmp = tempfile.mkdtemp(prefix="kenny-smoke-")
    case = os.path.join(tmp, "santacruz")
    shutil.copytree(os.path.join(ROOT, "cases", "santacruz"), case)
    core_app.CASE_DIR = case
    auth.RateLimiter.allow = lambda self, key: True   # 20/min cap is a spend cap, not a smoke property
    from fastapi.testclient import TestClient
    return TestClientAdapter(TestClient(core_app.app)), case


# --------------------------------------------------------------------------- #
# helpers shared by the checks
# --------------------------------------------------------------------------- #
def _ask(client, prompt: str, ctx: dict) -> dict:
    status, j = client.post("/chat", {"prompt": prompt})
    j = j if isinstance(j, dict) else {}
    j["_status"] = status
    ctx.setdefault("answers", {})[prompt] = j
    return j


def _first_citation(j: dict) -> dict:
    try:
        return j["result"]["line_items"][0]["citations"][0]
    except (KeyError, IndexError, TypeError):
        return {}


def _cite_pages(j: dict) -> list[tuple[str, int]]:
    out = []
    for li in (j.get("result") or {}).get("line_items", []):
        for c in li.get("citations", []):
            out.append((c.get("doc_id"), c.get("page")))
    return out


def _costing(j: dict, total: float, doc: str, page: int):
    c = _first_citation(j)
    observed = (f"mode={j.get('mode')} total={(j.get('result') or {}).get('total')} "
                f"cite={c.get('doc_id')} p.{c.get('page')}")
    expected = f"mode=costing total={total} cite={doc} p.{page}"
    ok = (j.get("mode") == "costing" and (j.get("result") or {}).get("total") == total
          and c.get("doc_id") == doc and c.get("page") == page)
    return ok, observed, expected


# --------------------------------------------------------------------------- #
# the checks — the demo script, row by row
# --------------------------------------------------------------------------- #
def chk_overtime(client, ctx):
    j = _ask(client, Q_OVERTIME, ctx)
    ctx["overtime_qid"] = j.get("query_id")
    return _costing(j, 640.8, L3535, 8)


def chk_longevity(client, ctx):
    j = _ask(client, Q_LONGEVITY, ctx)
    ok, observed, expected = _costing(j, 656.82, L3535, 12)
    pages = _cite_pages(j)
    yrs = (j.get("params") or {}).get("years_of_service")
    ok = ok and (L3535, 8) in pages and yrs == 12.0
    observed += f" pages={sorted(p for _, p in pages)} years={yrs}"
    expected += " pages=[8, 12] years=12.0"
    return ok, observed, expected


def chk_bereavement(client, ctx):
    j = _ask(client, Q_BEREAVEMENT, ctx)
    c = _first_citation(j)
    li = ((j.get("result") or {}).get("line_items") or [{}])[0]
    observed = (f"mode={j.get('mode')} total={(j.get('result') or {}).get('total')} "
                f"{li.get('result_type')} cite={c.get('doc_id')} p.{c.get('page')}")
    expected = f"mode=entitlement total=3.0 shifts cite={L3535} p.21"
    ok = (j.get("mode") == "entitlement" and (j.get("result") or {}).get("total") == 3.0
          and li.get("result_type") == "shifts" and c.get("doc_id") == L3535
          and c.get("page") == 21)
    return ok, observed, expected


def chk_off_corpus(client, ctx):
    j = _ask(client, Q_OFF_CORPUS, ctx)
    ans = j.get("answer") or ""
    observed = f"mode={j.get('mode')} answer={ans[:60]!r}"
    expected = "mode=out_of_scope answer names the 5 documents, no 'Which department'"
    ok = (j.get("mode") == "out_of_scope" and "not covered by the 5 documents" in ans
          and "Which department" not in ans and j.get("sources") == [])
    return ok, observed, expected


def chk_regular_refused(client, ctx):
    j = _ask(client, Q_REGULAR, ctx)
    msg = j.get("message") or ""
    observed = (f"mode={j.get('mode')} approved_topics={j.get('approved_topics')} "
                f"total={(j.get('result') or {}).get('total')}")
    expected = "mode=refused approved_topics=['overtime'] total=None, message names the rule gap"
    ok = (j.get("mode") == "refused" and j.get("approved_topics") == ["overtime"]
          and "No approved regular rule" in msg and "result" not in j)
    return ok, observed, expected


def chk_mixed_units(client, ctx):
    j = _ask(client, Q_MIXED, ctx)
    ok, observed, expected = _costing(j, 640.8, L3535, 8)
    res = j.get("result") or {}
    unc = (res.get("uncovered") or [{}])[0]
    ok = (ok and res.get("partial") is True and unc.get("subject") == "Fire Marshal (top step)"
          and unc.get("status") == "no_rules" and "Management MOU" in (unc.get("reason") or "")
          and len(res.get("line_items") or []) == 1)
    observed += f" partial={res.get('partial')} uncovered={unc.get('subject')}:{unc.get('status')}"
    expected += " partial=True uncovered=Fire Marshal (top step):no_rules (Management MOU named)"
    return ok, observed, expected


def chk_verification(client, ctx):
    status, v = client.get("/admin/verification")
    v = v if isinstance(v, dict) else {}
    goldens = v.get("goldens") or []
    passing = sum(1 for g in goldens if g.get("status") == "pass")
    observed = (f"all_passing={v.get('all_passing')} {passing}/{len(goldens)} pass "
                f"rules={v.get('rule_count')} unverified={v.get('unverified')}")
    expected = "all_passing=True 6/6 pass rules=5 unverified=[]"
    ok = (status == 200 and v.get("all_passing") is True and len(goldens) == 6
          and passing == 6 and v.get("rule_count") == 5 and v.get("unverified") == [])
    return ok, observed, expected


def chk_replay(client, ctx):
    qid = ctx.get("overtime_qid")
    status, r = client.get(f"/chat/replay/{qid}")
    r = r if isinstance(r, dict) else {}
    checks = r.get("checks") or []
    failed = [c.get("name") for c in checks if not c.get("ok")]
    observed = (f"status={r.get('status')} recomputed={(r.get('recomputed') or {}).get('total')} "
                f"checks={len(checks)} failed={failed} drift={(r.get('drift') or {}).get('data')}")
    expected = "status=match recomputed=640.8 checks=6 failed=[] drift=unchanged"
    ok = (status == 200 and r.get("status") == "match" and not failed and len(checks) == 6
          and (r.get("recomputed") or {}).get("total") == 640.8)
    return ok, observed, expected


def chk_p22_cells(client, ctx):
    status, c = client.get(f"/doc/{L3535}/clauses?page=22")
    c = c if isinstance(c, dict) else {}
    rows = sum(1 for x in c.get("clauses", []) if x.get("cell_status") == "disputed")
    observed = (f"disputed_cells={c.get('disputed_cells')} disputed_rows={rows} "
                f"text_origin={c.get('text_origin')}")
    expected = "disputed_cells=6 disputed_rows=4 text_origin=ocr-layer"
    ok = (status == 200 and c.get("disputed_cells") == 6 and rows == 4
          and c.get("text_origin") == "ocr-layer")
    return ok, observed, expected


def chk_skeptic(client, ctx):
    status, s = client.get("/admin/skeptic")
    reviews = (s or {}).get("reviews") or {}
    fresh = sorted(k for k, v in reviews.items() if v.get("fresh"))
    observed = f"reviews={len(reviews)} fresh={fresh}"
    expected = (f"reviews=2 fresh=['admin_group_mou:overtime_premium_rate', "
                f"'{L3535}:overtime_premium_rate']")
    ok = (status == 200 and len(reviews) == 2
          and fresh == ["admin_group_mou:overtime_premium_rate", f"{L3535}:overtime_premium_rate"]
          and all(v.get("mode") == "precomputed" for v in reviews.values()))
    return ok, observed, expected


def chk_build_and_health(client, ctx):
    status, c = client.get("/api/case")
    v = ((c or {}).get("version") or {})
    hstatus, h = client.get("/healthz")
    h = h or {}
    observed = (f"version={v.get('sha')} source={v.get('source')} dirty={v.get('dirty')} "
                f"healthz={h.get('status')} ledger={h.get('ledger')} library={h.get('library')}")
    expected = "version=<sha> source in (git, env, unknown) healthz=ok ledger=ok library=ok"
    ok = (status == 200 and isinstance(v.get("sha"), str) and v.get("sha")
          and v.get("source") in ("git", "env", "unknown") and isinstance(v.get("dirty"), bool)
          and hstatus == 200 and h.get("status") == "ok" and h.get("ledger") == "ok"
          and h.get("library") == "ok")
    return ok, observed, expected


CHECKS = [
    ("overtime_640_80", "Firefighter/Paramedic 8 h overtime -> $640.80, L3535 p.8", chk_overtime),
    ("longevity_656_82", "same with 12 years -> $656.82, p.12 + p.8", chk_longevity),
    ("bereavement_3_shifts", "firefighter bereavement -> 3 shifts, L3535 p.21", chk_bereavement),
    ("heat_pump_out_of_scope", "heat-pump rebate -> out_of_scope naming the 5 documents", chk_off_corpus),
    ("regular_shift_refused", "regular shift -> refused, only overtime approved", chk_regular_refused),
    ("mixed_units_partial", "firefighter + Fire Marshal -> $640.80 + not-covered row", chk_mixed_units),
    ("verification_all_passing", "/admin/verification -> 6 known answers pass, 5 rules", chk_verification),
    ("replay_match", "/chat/replay/{640.80} -> match on every check", chk_replay),
    ("p22_disputed_cells", "/doc/.../clauses?page=22 -> 6 disputed cells", chk_p22_cells),
    ("skeptic_two_reviews", "/admin/skeptic -> two fresh precomputed reviews", chk_skeptic),
    ("build_hash_and_health", "/api/case version + /healthz ok", chk_build_and_health),
]


def run_all(client) -> list[dict]:
    """Every check in order; a check that raises is a miss, never a crash."""
    ctx: dict = {}
    rows = []
    for cid, what, fn in CHECKS:
        try:
            ok, observed, expected = fn(client, ctx)
        except Exception as e:  # pragma: no cover - a crash is reported as a miss
            ok, observed, expected = False, f"EXCEPTION {type(e).__name__}: {e}", "no exception"
        rows.append({"id": cid, "what": what, "ok": bool(ok),
                     "observed": observed, "expected": expected})
    return rows


def format_table(rows: list[dict]) -> str:
    w = max(len(r["id"]) for r in rows)
    lines = [f"{'check':<{w}}  result  observed", f"{'-' * w}  ------  --------"]
    for r in rows:
        lines.append(f"{r['id']:<{w}}  {'PASS' if r['ok'] else 'MISS'}    {r['observed']}")
        if not r["ok"]:
            lines.append(f"{'':<{w}}          expected: {r['expected']}")
    missed = [r["id"] for r in rows if not r["ok"]]
    lines.append("")
    lines.append(f"{len(rows) - len(missed)} of {len(rows)} demo checks pass"
                 + (f"; MISSED: {', '.join(missed)}" if missed else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", help="base URL of a running Kenny (default: in-process "
                                  "TestClient on a scratch copy of cases/santacruz)")
    args = ap.parse_args(argv)
    if args.url:
        client, where = HttpClient(args.url), args.url
    else:
        client, where = scratch_client()
    rows = run_all(client)
    print(f"demo smoke against {where}")
    print(format_table(rows))
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
