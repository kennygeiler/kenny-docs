"""Skeptic review of a rule: an agent that reads and challenges, never computes
(DEMO_TICKETS.md G1 / G6).

The skeptic is given three tools and a rule. It reads the clause the rule cites and the
pages around it, searches for the terms the clause uses but does not define, proposes
counter-example scenarios and runs them through the deterministic engine — as the rule
is written and, where the DSL can say it, as the clause reads. Every figure in its
review is either a verbatim quote or an engine result; `validate_review` drops anything
it cannot prove, and `verify` re-checks every quote and replays every engine call later.

Two hard rules of this module:
  * It imports caseio, catalog, index, engine, governance and ruledsl only — never
    core.llm, core.app or anthropic. The app serves stored reviews; it makes no model
    call. The model that produced a review is named in the artifact's provenance block.
  * Every tool result is persisted in the run log before the agent sees it, so the
    record is what the tool returned, not what a shell output hook showed the agent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from typing import Any

from . import governance, index, queryfacts
from .caseio import CaseContext
from .catalog import Catalog
from .engine import NoRuleApplies, calculate
from .ruledsl import SHIFT_BASES, Rule, RuleError, validate_rules

SCHEMA = "skeptic.v1"
LIMITS = {"tool_calls": 12, "search_clauses": 4, "read_page": 6, "run_engine": 6,
          "warnings": 8}
SEVERITIES = ("high", "medium", "low")
KINDS = ("ignored_condition", "undefined_term", "adjacent_clause", "missing_data",
         "rounding", "alternative_outcome", "citation")
VERDICTS = ("challenge", "no_objection", "incomplete")
QUOTE_MIN, QUOTE_MAX = 12, 300
CLAUSE_TEXT_CAP = 1200
MAX_SCENARIO_HOURS = 744.0
# Same regex as llm._FIGURE_RE, copied so this module never imports core.llm.
_FIGURE_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")


# --------------------------------------------------------------------------- #
# paths, hashing, lookup
# --------------------------------------------------------------------------- #
def safe_name(rule_id: str) -> str:
    return rule_id.replace(":", "__").replace("/", "__")


def reviews_dir(case: CaseContext) -> str:
    return case.path("reviews", "reviews")


def artifact_path(case: CaseContext, rule_id: str) -> str:
    return os.path.join(reviews_dir(case), safe_name(rule_id) + ".json")


def runs_dir(case: CaseContext) -> str:
    return os.path.join(reviews_dir(case), ".runs")


def _sha(data: Any) -> str:
    if isinstance(data, bytes):
        return hashlib.sha256(data).hexdigest()
    return hashlib.sha256(_canon(data).encode("utf-8")).hexdigest()


def _canon(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _file_sha(path: str | None) -> str:
    if not path or not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


RULE_HASH_FIELDS = ("id", "kind", "role", "result_type", "pay_basis", "when", "compute",
                    "set", "flags", "citation")


def rule_sha256(rule: dict) -> str:
    return _sha({k: rule.get(k) for k in RULE_HASH_FIELDS})


def _raw_rules(path: str | None) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path) as f:
        raw = json.load(f)
    return raw.get("rules", raw) if isinstance(raw, dict) else raw


def find_rule(case: CaseContext, rule_id: str) -> dict | None:
    """The stored rule dict (ratified library first, then the proposed queue)."""
    for key, default in (("rules", None), ("proposed_rules", "rules/rules_proposed.json")):
        for r in _raw_rules(case.path(key, default)):
            if r.get("id") == rule_id:
                return r
    return None


def catalog(case: CaseContext) -> Catalog:
    return Catalog(case.path("catalog", "catalog.json"))


def input_hashes(case: CaseContext, rule: dict, cat: Catalog | None = None) -> dict:
    """What a review was produced against: the rule, the cited PDF, its catalog clauses
    and the roster. A later change to any of them marks the review stale."""
    cat = cat or catalog(case)
    docs = allowed_docs(case, rule)
    out = {"rule_sha256": rule_sha256(rule), "doc_sha256": {}, "catalog_doc_sha256": {}}
    for d in docs:
        src = case.source_by_id(d) or {}
        file_ = src.get("file")
        out["doc_sha256"][d] = _file_sha(os.path.join(case.dir, file_)) if file_ else ""
        out["catalog_doc_sha256"][d] = _sha(cat.clauses(d))
    data = case.manifest.get("data", {}) or {}
    out["roster_sha256"] = _file_sha(os.path.join(case.dir, data.get("path", ""))) \
        if data.get("path") else ""
    return out


def allowed_docs(case: CaseContext, rule: dict) -> list[str]:
    """The cited document, the other sources of the same bargaining unit, and every
    salary schedule. A firefighters rule may not read the admin MOU."""
    cit = rule.get("citation") or {}
    doc = cit.get("doc_id")
    src = case.source_by_id(doc) or {}
    unit = src.get("bargaining_unit")
    out = [doc] if doc else []
    for s in case.manifest.get("sources", []):
        sid = s.get("id")
        if sid in out:
            continue
        if (unit and s.get("bargaining_unit") == unit) or s.get("doc_type") == "salary-schedule":
            out.append(sid)
    return out


# --------------------------------------------------------------------------- #
# run log
# --------------------------------------------------------------------------- #
class BudgetExhausted(Exception):
    pass


class Run:
    """One skeptic run: every tool call persisted in order, with its result and the
    result's hash, and mirrored to the ledger as skeptic.tool_call."""

    def __init__(self, case: CaseContext, data: dict):
        self.case = case
        self.data = data

    # ---- lifecycle ----
    @classmethod
    def start(cls, case: CaseContext, rule: dict, producer: str = "unknown",
              operator: str = "", model: str = "unreported", mode: str = "precomputed",
              api_key_in_env: bool = False) -> "Run":
        run_id = uuid.uuid4().hex[:12]
        data = {"run_id": run_id, "rule_id": rule.get("id"), "rule_sha256": rule_sha256(rule),
                "rule": rule, "producer": producer, "operator": operator, "model": model,
                "mode": mode, "api_key_in_env": bool(api_key_in_env),
                "started_at": _utc(), "limits": dict(LIMITS), "calls": [],
                "pages_read": [], "finished": False}
        run = cls(case, data)
        run.save()
        case.ledger().append("skeptic.start",
                             {"run_id": run_id, "rule_id": rule.get("id"),
                              "rule_sha256": data["rule_sha256"], "mode": mode,
                              "producer": producer, "limits": dict(LIMITS)},
                             actor="skeptic")
        return run

    @classmethod
    def load(cls, case: CaseContext, run_id: str) -> "Run":
        path = os.path.join(runs_dir(case), f"{run_id}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"no run {run_id!r} under {runs_dir(case)}")
        with open(path) as f:
            return cls(case, json.load(f))

    @property
    def run_id(self) -> str:
        return self.data["run_id"]

    @property
    def rule(self) -> dict:
        return self.data["rule"]

    @property
    def calls(self) -> list[dict]:
        return self.data["calls"]

    def path(self) -> str:
        return os.path.join(runs_dir(self.case), f"{self.run_id}.json")

    def save(self) -> None:
        _atomic_write(self.path(), self.data)

    # ---- tools ----
    def _count(self, tool: str) -> int:
        return sum(1 for c in self.calls if c["tool"] == tool)

    def tool(self, name: str, **inp) -> dict:
        """Dispatch one tool call under the budget; persist and ledger the result."""
        if name not in ("search_clauses", "read_page", "run_engine"):
            return {"error": f"unknown tool {name!r}"}
        if len(self.calls) >= LIMITS["tool_calls"] or self._count(name) >= LIMITS[name]:
            return {"error": "budget exhausted"}
        started = time.time()
        if name == "search_clauses":
            result = search_clauses(self.case, self.rule, inp.get("query", ""),
                                    inp.get("doc_id", ""))
        elif name == "read_page":
            result = read_page(self.case, self.rule, inp.get("doc_id", ""),
                               int(inp.get("page") or 0))
            if "error" not in result:
                self.data["pages_read"].append([inp.get("doc_id"), int(inp.get("page"))])
        else:
            result = run_engine(self.case, self.rule, inp.get("rule_set", "live"),
                                inp.get("scenario") or {}, inp.get("variant"))
        n = len(self.calls) + 1
        entry = {"n": n, "tool": name, "input": inp, "result_sha256": _sha(result),
                 "result": result, "summary": _summarise(name, inp, result),
                 "ms": int((time.time() - started) * 1000)}
        self.calls.append(entry)
        self.save()
        self.case.ledger().append(
            "skeptic.tool_call",
            {"run_id": self.run_id, "n": n, "tool": name, "input": inp,
             "result_sha256": entry["result_sha256"], "summary": entry["summary"],
             "ms": entry["ms"]}, actor="skeptic")
        return result

    def pages_read(self) -> set[tuple[str, int]]:
        return {(d, int(p)) for d, p in self.data.get("pages_read", [])}


def _summarise(tool: str, inp: dict, result: dict) -> str:
    if "error" in result:
        return f"{tool}: error — {result['error']}"
    if tool == "search_clauses":
        return (f"search {inp.get('doc_id')} for {inp.get('query')!r}: "
                f"{len(result.get('hits', []))} hit(s)")
    if tool == "read_page":
        return (f"read {inp.get('doc_id')} p.{inp.get('page')}: "
                f"{len(result.get('clauses', []))} clause(s)")
    sc = inp.get("scenario") or {}
    return (f"engine {inp.get('rule_set')} {sc.get('subjects')} "
            f"{sc.get('params')} -> {result.get('total')}")


def _atomic_write(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# the three tools (pure functions over the case)
# --------------------------------------------------------------------------- #
def search_clauses(case: CaseContext, rule: dict, query: str, doc_id: str) -> dict:
    """BM25 over the case's search index, scoped to one allowed document. BM25 on
    purpose: deterministic, and no embedding model to load."""
    if doc_id not in allowed_docs(case, rule):
        return {"error": f"document {doc_id!r} is outside this rule's scope "
                         f"(allowed: {allowed_docs(case, rule)})"}
    if not query or not query.strip():
        return {"error": "empty query"}
    be = index.LocalBM25Backend(case.path("search_index", "search_index.jsonl"))
    hits = be.search(query, [doc_id], k=6)
    cat = catalog(case)
    out = []
    for h in hits:
        page = h.get("page")
        ref = _ref_for(cat, doc_id, page, h.get("bbox"))
        out.append({"ref": ref, "page": page, "bbox": h.get("bbox"),
                    "text": (h.get("text") or "")[:400], "score": h.get("score")})
    return {"hits": out}


def _ref_for(cat: Catalog, doc_id: str, page: int | None, bbox) -> str:
    i = 0
    for c in cat.clauses(doc_id):
        if c.get("page") != page:
            continue
        if _same_bbox(c.get("bbox"), bbox):
            return f"{doc_id}#p{page}.{i}"
        i += 1
    return f"{doc_id}#p{page}"


def _same_bbox(a, b) -> bool:
    try:
        return len(a) == len(b) == 4 and all(round(float(x), 1) == round(float(y), 1)
                                            for x, y in zip(a, b))
    except (TypeError, ValueError):
        return False


def read_page(case: CaseContext, rule: dict, doc_id: str, page: int) -> dict:
    """Every catalog clause on one page, in order, each with a stable ref and whether
    it is the clause the rule cites (bbox equal to 1 dp)."""
    if doc_id not in allowed_docs(case, rule):
        return {"error": f"document {doc_id!r} is outside this rule's scope "
                         f"(allowed: {allowed_docs(case, rule)})"}
    cat = catalog(case)
    entry = cat.get(doc_id)
    if not entry:
        return {"error": f"document {doc_id!r} is not in the catalog"}
    page_count = int(entry.get("page_count") or 0)
    if page < 1 or (page_count and page > page_count):
        return {"error": f"page {page} is outside 1..{page_count}"}
    cit_bbox = (rule.get("citation") or {}).get("bbox")
    cited_doc = (rule.get("citation") or {}).get("doc_id") == doc_id
    clauses = []
    i = 0
    for c in cat.clauses(doc_id):
        if c.get("page") != page:
            continue
        clauses.append({"ref": f"{doc_id}#p{page}.{i}", "bbox": c.get("bbox", []),
                        "label": c.get("label", ""),
                        "text": (c.get("text") or "")[:CLAUSE_TEXT_CAP],
                        "cited": bool(cited_doc and _same_bbox(c.get("bbox"), cit_bbox))})
        i += 1
    return {"doc_id": doc_id, "page": page, "page_count": page_count, "clauses": clauses}


def _variant_ok(expr: str | None) -> str | None:
    if expr is None:
        return None
    if len(expr) > 200:
        return "expression over 200 characters"
    if "**" in expr:
        return "exponent operator is not allowed"
    return None


def run_engine(case: CaseContext, rule: dict, rule_set: str, scenario: dict,
               variant: dict | None = None) -> dict:
    """Run a scenario through core.engine.calculate exactly the way the app does
    (governance scoping, result_type filter, shift basis) — the agent never computes.

    rule_set: 'live' (the ratified library), 'with_rule' (live merged by id with the
    reviewed rule, for a proposed rule), 'variant' (the reviewed rule with `when` /
    `compute` replaced by `variant`, validated against the case's known facts first —
    an unknown-fact error is returned to the agent and is itself the needs-data signal).
    """
    subjects_all = case.subjects()
    names = list((scenario or {}).get("subjects") or [])
    subs = [s for s in subjects_all if s.get("name") in set(names)]
    if not subs:
        return {"error": f"no roster classification matches {names}"}
    params = {"hours": 0.0, "date": "", "date_iso": "", "holiday_weekday": ""}
    # Every declared numeric question fact at its safe default (data-goldens J1a: the
    # live longevity rule references years_of_service), exactly as the chat path does.
    params.update(queryfacts.query_defaults(case))
    raw = dict((scenario or {}).get("params") or {})
    try:
        hours = float(raw.get("hours", 0.0) or 0.0)
    except (TypeError, ValueError):
        return {"error": f"hours {raw.get('hours')!r} is not a number"}
    if not (0.0 <= hours <= MAX_SCENARIO_HOURS) or hours != hours:
        return {"error": f"hours must be between 0 and {MAX_SCENARIO_HOURS:g}"}
    params["hours"] = hours
    for k in ("date", "date_iso", "holiday_weekday"):
        if raw.get(k) is not None:
            params[k] = str(raw.get(k))

    live = [r for r in _raw_rules(case.path("rules"))
            if r.get("status") == "ratified" and r.get("approver")]
    if rule_set == "live":
        dicts = live
    elif rule_set == "with_rule":
        merged = {r["id"]: r for r in live}
        merged[rule["id"]] = {**rule, "status": "ratified", "approver": rule.get("approver")
                              or "skeptic-candidate"}
        dicts = list(merged.values())
    elif rule_set == "variant":
        variant = variant or {}
        for k in ("when", "compute"):
            bad = _variant_ok(variant.get(k))
            if bad:
                return {"error": f"variant {k}: {bad}"}
        cand = dict(rule)
        for k in ("when", "compute"):
            if variant.get(k) is not None:
                cand[k] = variant[k]
        errs = validate_rules([cand], case.known_facts())
        if errs:
            return {"error": "; ".join(errs.get(str(cand.get("id")), ["invalid variant"]))}
        merged = {r["id"]: r for r in live}
        merged[rule["id"]] = {**cand, "status": "ratified",
                              "approver": cand.get("approver") or "skeptic-variant"}
        dicts = list(merged.values())
    else:
        return {"error": f"rule_set must be live, with_rule or variant (got {rule_set!r})"}

    try:
        rules = case.assign_scope([Rule.from_dict(d) for d in dicts])
        units = sorted({s.get("bargaining_unit") for s in subs if s.get("bargaining_unit")})
        sources = case.manifest.get("sources", [])
        if units:
            gov = governance.resolve(units, params.get("date_iso") or None, sources)
            if gov.resolved:
                rules = [r for r in rules
                         if (not r.citation.doc_id) or (r.citation.doc_id in gov.doc_ids)]
                rules, _ = governance.apply_supersession(rules, sources, gov.doc_ids)
        want = rule.get("result_type", "currency")
        rules = [r for r in rules if r.result_type == want]
        scope = SHIFT_BASES if want == "currency" else None
        res = calculate(params, subs, rules, case.rounding_places(), basis_scope=scope)
    except (NoRuleApplies, RuleError, ValueError, ArithmeticError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    lines = []
    for li in res.line_items:
        math = next((t.detail for t in li.trace if t.kind == "math"), "")
        lines.append({"subject": li.subject, "rule_id": li.rule_id, "total": li.total,
                      "math": math})
    return {"total": res.total, "rule_set": rule_set, "lines": lines}


# --------------------------------------------------------------------------- #
# validation: what the agent cannot prove is dropped and listed
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _figures(text: str) -> set[str]:
    return {m.group(0).lstrip("$").replace(",", "") for m in _FIGURE_RE.finditer(text or "")}


def _as_numbers(figs: set[str]) -> set[float]:
    out = set()
    for f in figs:
        try:
            out.add(float(f))
        except ValueError:
            pass
    return out


def _find_quote(cat: Catalog, doc_id: str, page: int, quote: str) -> tuple[int, dict] | None:
    """The catalog clause on (doc_id, page) whose normalised text contains the quote."""
    q = _norm(quote)
    i = 0
    for c in cat.clauses(doc_id):
        if c.get("page") != page:
            continue
        if q and q in _norm(c.get("text", "")):
            return i, c
        i += 1
    return None


def validate_review(case: CaseContext, rule: dict, review: dict, run: Run,
                    cat: Catalog | None = None) -> tuple[dict, list[dict]]:
    """Deterministic. Returns (clean_review, rejected). Every quote must sit on a page
    this run read; every counter-example total is overwritten from the run log; every
    figure in a claim must come from a quote or a tool call; at most 8 warnings."""
    cat = cat or catalog(case)
    rejected: list[dict] = []
    pages_read = run.pages_read()
    engine_calls = {c["n"]: c for c in run.calls if c["tool"] == "run_engine"}
    # Figures the agent may use: everything the tools said, and everything it quoted.
    tool_text = _canon([{"input": c["input"], "result": c["result"]} for c in run.calls])
    grounded = _figures(tool_text)
    grounded_nums = _as_numbers(grounded)

    def _grounded(fig: str) -> bool:
        if fig in grounded:
            return True
        try:
            return float(fig) in grounded_nums
        except ValueError:
            return False

    clean: list[dict] = []
    for w in list(review.get("warnings") or []):
        wid = str(w.get("id") or f"w{len(clean) + 1}")
        sev = w.get("severity") if w.get("severity") in SEVERITIES else "low"
        kind = w.get("kind") if w.get("kind") in KINDS else "alternative_outcome"
        claim = str(w.get("claim") or "").strip()
        if not claim:
            rejected.append({"warning_id": wid, "reason": "empty claim"})
            continue
        # (a) quotes
        evidence = []
        quote_figs: set[str] = set()
        bad_quote = None
        for ev in (w.get("evidence") or []):
            doc, page, quote = ev.get("doc_id"), ev.get("page"), str(ev.get("quote") or "")
            try:
                page = int(page)
            except (TypeError, ValueError):
                bad_quote = f"evidence page {page!r} is not a number"
                break
            if not (QUOTE_MIN <= len(quote) <= QUOTE_MAX):
                bad_quote = f"quote length {len(quote)} outside {QUOTE_MIN}..{QUOTE_MAX}"
                break
            if (doc, page) not in pages_read:
                bad_quote = f"quote cites {doc} p.{page}, a page this run never read"
                break
            hit = _find_quote(cat, doc, page, quote)
            if hit is None:
                bad_quote = f"quote not found on {doc} p.{page}: {quote[:60]!r}"
                break
            i, clause = hit
            evidence.append({"doc_id": doc, "page": page, "ref": f"{doc}#p{page}.{i}",
                             "bbox": clause.get("bbox", []), "quote": quote})
            quote_figs |= _figures(quote)
        if bad_quote:
            rejected.append({"warning_id": wid, "reason": bad_quote})
            continue
        if not evidence:
            rejected.append({"warning_id": wid, "reason": "no evidence quote"})
            continue
        # (b) counter-example totals come from the run log, never from the agent
        ce = w.get("counter_example")
        ce_clean = None
        if isinstance(ce, dict):
            n = ce.get("call_n")
            call = engine_calls.get(n) if isinstance(n, int) else None
            if call is None or "error" in call["result"]:
                rejected.append({"warning_id": wid,
                                 "reason": f"counter-example points at no logged "
                                           f"run_engine call (call_n={n!r}); dropped"})
            else:
                bn = ce.get("baseline_call_n")
                base = engine_calls.get(bn) if isinstance(bn, int) else None
                ce_clean = {"scenario": call["input"].get("scenario"),
                            "rule_set": call["input"].get("rule_set"),
                            "variant": call["input"].get("variant"),
                            "call_n": n, "engine_total": call["result"].get("total"),
                            "baseline_call_n": bn if base and "error" not in base["result"]
                            else None,
                            "baseline_total": base["result"].get("total")
                            if base and "error" not in base["result"] else None,
                            "why": str(ce.get("why") or "")}
        # (c) every figure in claim / why must be grounded
        text = claim + " " + (ce_clean or {}).get("why", "")
        loose = [f for f in _figures(text) if not (_grounded(f) or f in quote_figs
                                                     or _as_numbers({f}) <= _as_numbers(quote_figs))]
        if loose:
            rejected.append({"warning_id": wid,
                             "reason": f"unverified figure(s) {sorted(loose)} in claim"})
            continue
        needs = [str(x) for x in (w.get("needs_data") or [])]
        clean.append({"id": wid, "severity": sev, "kind": kind, "claim": claim,
                      "evidence": evidence, "counter_example": ce_clean,
                      "needs_data": needs,
                      "suggested_action": str(w.get("suggested_action") or "")})
        if len(clean) >= LIMITS["warnings"]:
            extra = len(review.get("warnings") or []) - len(clean) - len(
                [r for r in rejected if r.get("warning_id")])
            if extra > 0:
                rejected.append({"reason": f"{extra} warning(s) over the limit of "
                                           f"{LIMITS['warnings']} dropped"})
            break
    summary = str(review.get("summary") or "").strip()
    loose = [f for f in _figures(summary)
             if not (_grounded(f) or any(f in _figures(e["quote"])
                                         for w in clean for e in w["evidence"]))]
    if loose:
        rejected.append({"reason": f"summary withheld: unverified figure(s) {sorted(loose)}"})
        summary = "(summary withheld: it contained a figure no quote or tool result holds)"
    verdict = review.get("verdict") if review.get("verdict") in VERDICTS else (
        "challenge" if clean else "no_objection")
    if verdict == "challenge" and not clean:
        verdict = "incomplete"     # it claimed a challenge and could prove none of it
    return {"verdict": verdict, "summary": summary, "warnings": clean}, rejected


# --------------------------------------------------------------------------- #
# artifact: build, save, load, verify
# --------------------------------------------------------------------------- #
def _code_rev() -> str:
    try:
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (rev or "unknown") + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def _prompt_sha() -> str:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "skeptic.md")
    return _file_sha(p)


def submit(run: Run, review: dict, harness: str = "", stopped: str = "submitted") -> dict:
    """Validate, build the artifact with its provenance and input hashes, save it
    atomically, and append skeptic.warning / skeptic.rejected / skeptic.finish."""
    case, rule = run.case, run.rule
    cat = catalog(case)
    clean, rejected = validate_review(case, rule, review, run, cat)
    led = case.ledger()
    artifact = {
        "schema": SCHEMA,
        "rule_id": rule.get("id"),
        "rule_sha256": rule_sha256(rule),
        "verdict": clean["verdict"],
        "summary": clean["summary"],
        "warnings": clean["warnings"],
        "rejected": rejected,
        "tool_calls": [{k: c[k] for k in ("n", "tool", "input", "result_sha256", "summary")}
                       for c in run.calls],
        # The full record: every step with the real tool output, so `verify` can replay
        # every engine call and re-check every quote without the run file.
        "steps": [{k: c[k] for k in ("n", "tool", "input", "result_sha256", "result")}
                  for c in run.calls],
        "stopped": stopped,
        "provenance": {
            "mode": run.data.get("mode", "precomputed"),
            "producer": run.data.get("producer"),
            "harness": harness or "unreported",
            "model": run.data.get("model") or "unreported",
            "operator": run.data.get("operator") or "",
            "produced_at": _utc(),
            "api_key_in_env": bool(run.data.get("api_key_in_env")),
            "billing": "subscription",
            "prompt_sha256": _prompt_sha(),
            "code_rev": _code_rev(),
            "run_id": run.run_id,
            "tool_calls": len(run.calls),
            "limits": dict(LIMITS),
        },
        "inputs": input_hashes(case, rule, cat),
    }
    path = artifact_path(case, rule.get("id"))
    _atomic_write(path, artifact)
    art_sha = _file_sha(path)
    for w in clean["warnings"]:
        led.append("skeptic.warning",
                   {"run_id": run.run_id, "id": w["id"], "severity": w["severity"],
                    "kind": w["kind"], "claim": w["claim"],
                    "refs": [e["ref"] for e in w["evidence"]],
                    "engine_total": (w.get("counter_example") or {}).get("engine_total")},
                   actor="skeptic")
    for r in rejected:
        led.append("skeptic.rejected", {"run_id": run.run_id, **r}, actor="skeptic")
    led.append("skeptic.finish",
               {"run_id": run.run_id, "rule_id": rule.get("id"), "verdict": clean["verdict"],
                "warnings": len(clean["warnings"]), "stopped": stopped,
                "artifact_sha256": art_sha}, actor="skeptic")
    run.data["finished"] = True
    run.save()
    return artifact


def list_reviews(case: CaseContext) -> dict[str, dict]:
    """rule_id -> stored artifact (raw), for every artifact under reviews/."""
    out = {}
    d = reviews_dir(case)
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            with open(os.path.join(d, name)) as f:
                art = json.load(f)
        except (OSError, ValueError):
            continue
        if art.get("schema") == SCHEMA and art.get("rule_id"):
            out[art["rule_id"]] = art
    return out


def stale_reasons(case: CaseContext, artifact: dict) -> list[str]:
    """Why a stored review no longer matches the live inputs (empty = fresh)."""
    rule = find_rule(case, artifact.get("rule_id", ""))
    if rule is None:
        return ["rule no longer exists"]
    now = input_hashes(case, rule)
    then = artifact.get("inputs") or {}
    reasons = []
    if now["rule_sha256"] != then.get("rule_sha256"):
        reasons.append("rule changed")
    for d, h in now["doc_sha256"].items():
        if (then.get("doc_sha256") or {}).get(d) != h:
            reasons.append(f"document file changed: {d}")
    for d, h in now["catalog_doc_sha256"].items():
        if (then.get("catalog_doc_sha256") or {}).get(d) != h:
            reasons.append(f"catalog clauses changed: {d}")
    if now["roster_sha256"] != then.get("roster_sha256"):
        reasons.append("roster changed")
    return reasons


def load_review(case: CaseContext, rule_id: str) -> dict | None:
    path = artifact_path(case, rule_id)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        art = json.load(f)
    reasons = stale_reasons(case, art)
    art["fresh"] = not reasons
    art["stale_reasons"] = reasons
    art["artifact_sha256"] = _file_sha(path)
    return art


def verify(case: CaseContext, rule_id: str) -> list[str]:
    """Re-hash the inputs, re-check every quote against the catalog, replay every
    logged run_engine call and compare totals. Empty list = everything reproduces."""
    art = load_review(case, rule_id)
    if art is None:
        return [f"no review artifact for {rule_id}"]
    problems = list(art["stale_reasons"])
    rule = find_rule(case, rule_id)
    if rule is None:
        return problems
    cat = catalog(case)
    for w in art.get("warnings", []):
        for e in w.get("evidence", []):
            if _find_quote(cat, e.get("doc_id"), int(e.get("page", 0)), e.get("quote", "")) is None:
                problems.append(f"{w.get('id')}: quote not on {e.get('doc_id')} "
                                f"p.{e.get('page')}: {e.get('quote', '')[:50]!r}")
    for step in art.get("steps", []):
        if step.get("tool") != "run_engine":
            continue
        inp = step.get("input") or {}
        got = run_engine(case, rule, inp.get("rule_set", "live"), inp.get("scenario") or {},
                         inp.get("variant"))
        want = step.get("result") or {}
        if got.get("total") != want.get("total") or ("error" in got) != ("error" in want):
            problems.append(f"engine call #{step.get('n')} replays to {got.get('total', got.get('error'))}, "
                            f"artifact recorded {want.get('total', want.get('error'))}")
        if _sha(got) != step.get("result_sha256"):
            problems.append(f"engine call #{step.get('n')}: replayed result hash differs")
    return problems
