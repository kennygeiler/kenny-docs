"""Rule review: proposed queue, gaps, scenario drafting, verification, ratify,
try-to-break, skeptic reviews, the demo wrong draft, cell gate/confirm.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import approvals, cellcheck, governance, llm
from .. import skeptic as _skeptic
from ..ruledsl import validate_rules
from ..services.gate import (
    _cell_gate_rows,
    _check_golden,
    _coverage,
    _gate_verdict,
    _rule_for_break,
    _try_break,
)
from ..services.rulestore import (
    _RULES_LOCK,
    DEMO_DRAFT_FILE,
    _demo_draft_status,
    _demo_drafts,
    _live_dicts,
    _proposed_queue,
    _raw_ratified,
    _rules_locked,
    _with_live,
    _write_proposed,
    _write_ratified,
)
from ..services.state import _backend, _case, _catalog

router = APIRouter()


@router.get("/admin/proposed")
def admin_proposed():
    return _proposed_queue()


# A gap is only actionable as a FIELD, not as 31 separate clause rows. The LLM's
# "missing" text is prose, so normalise it to the field a data owner would add.
_GAP_FIELDS = [
    ("subject_assignment", ("detective", "investigator", "motorcycle", "swat",
                            "special response", "assignment to", "bureau")),
    ("subject_k9_handler", ("k-9", "k9", "canine")),
    ("subject_paramedic", ("paramedic", "medic unit")),
    ("event", ("holdover", "callback", "call-back", "standby", "court", "recall",
               "mandated to work", "held over", "event")),
    ("subject_step", ("step",)),
    ("subject_seniority_years", ("years of service", "seniority", "length of service")),
    ("subject_acting_assignment", ("acting",)),
    ("subject_hire_date", ("hire date", "appointment date", "probation")),
    ("subject_separation_date", ("separation", "retire")),
    ("subject_coverage_tier", ("coverage tier", "medical plan", "dependent")),
]


def _gap_field(text: str) -> str:
    t = (text or "").lower()
    import re as _re
    m = _re.search(r"subject_[a-z0-9_]+", t)
    if m:
        return m.group(0)
    for field, cues in _GAP_FIELDS:
        if any(c in t for c in cues):
            return field
    return "other"


@router.get("/admin/gaps")
def admin_gaps():
    """Data gaps grouped by the FIELD required — a spec a data owner can act on
    ("add `assignment` and these four premiums start working"), not a clause list."""
    case = _case()
    proposed = _proposed_queue()
    groups: dict[str, dict] = {}
    for n in proposed.get("needs_data", []):
        if n.get("category") == "narrative":
            continue
        key = n.get("missing_field") or _gap_field(f"{n.get('missing','')} {n.get('reason','')}")
        g = groups.setdefault(key, {"field": key, "unlocks": []})
        src = case.source_by_id(n.get("doc_id", "")) or {}
        g["unlocks"].append({"doc_id": n.get("doc_id"), "clause": n.get("clause"),
                             "title": src.get("title", n.get("doc_id")),
                             "department": src.get("department"),
                             "reason": n.get("reason", "")})
    # Named fields first (they are the actionable ones); the unclassified catch-all
    # sorts last however big it is — "add `other`" is not a request anyone can action.
    out = sorted(groups.values(),
                 key=lambda g: (g["field"] == "other", -len(g["unlocks"])))
    return {"gaps": out, "total_clauses_blocked": sum(len(g["unlocks"]) for g in out)}


def _scenario_query(scenario: dict) -> str:
    """Retrieval query for the pay branch a scenario exercises. An explicit `branch_query`
    on the scenario wins; otherwise fall back to its name plus generic pay vocabulary."""
    if scenario.get("branch_query"):
        return scenario["branch_query"]
    base = "pay premium rate differential hours worked base"
    return f"{scenario.get('name', '')} {base}"


@router.post("/admin/draft_scenario")
async def admin_draft_scenario(request: Request):
    """Draft ONLY the rules a known scenario (a paystub) needs, and verify them.

    This is the rebuilt authoring model. Instead of drafting a whole 140-clause MOU into
    33 rules and hoping approve-all matches one grand total, a scenario names a real
    known answer; Kenny retrieves the handful of clauses that answer it, drafts just
    those, and checks them against the paystub. Review collapses from ~33 rules to ~5,
    and a small focused drafting task is one the model gets right (roles, pay_basis,
    compounding differentials) where the whole-MOU task was fragile.
    """
    body = await request.json()
    name = body.get("scenario")
    case = _case()
    led = case.ledger()
    scenario = next((g for g in case.golden_cases() if g.get("name") == name), None)
    if scenario is None:
        return JSONResponse({"error": f"no scenario named {name!r}"}, status_code=404)

    # 1. Which document(s) govern the scenario's people on its date.
    subs = [s for s in case.subjects() if s["name"] in set(scenario.get("subjects") or [])]
    units = sorted({s.get("bargaining_unit") for s in subs if s.get("bargaining_unit")})
    gov = governance.resolve(units, scenario.get("params", {}).get("date_iso"),
                             case.manifest.get("sources", []))
    doc_ids = gov.doc_ids or None

    # 2. Retrieve only the clauses this scenario's pay branch needs.
    cat = _catalog(case)
    ingested = {d["doc_id"] for d in cat.documents()}
    scope = [d for d in (doc_ids or list(ingested)) if d in ingested]
    if not scope:
        return JSONResponse({"error": "governing documents are not ingested yet — run "
                             "Ingest first"}, status_code=400)
    hits = _backend(case).search(_scenario_query(scenario), doc_ids=scope, k=8)
    want = {(h["doc_id"], str(h["clause"])) for h in hits}
    # Never re-draft a clause that already has a LIVE rule. A scenario completes the
    # library incrementally: the 2026 side-letter scenario needs ONE new rule (the 6.5%
    # amendment), not a re-draft of the base rules the 2025 scenario already ratified —
    # a re-draft carries the same ids, would overwrite the approved rules on ratify, and
    # the regression guard then (correctly) blocks the whole approval. Draft only the gap.
    live = {(r.citation.doc_id, str(r.citation.clause)) for r in case.rules()}
    want -= live
    clauses_by_doc: dict[str, list] = {}
    for d, cl in want:
        for c in cat.clauses(d):
            if str(c.get("clause")) == cl:
                clauses_by_doc.setdefault(d, []).append(c)

    # 3. Draft ONLY those clauses (per document, so ids namespace correctly). The drafter
    #    also reports clauses it correctly REFUSED to draft (event pay the roster cannot
    #    see) — that is the Data-gaps feed, kept per scenario.
    drafted: list[dict] = []
    needs: list[dict] = []
    with llm.record() as trail:
        for d, clauses in clauses_by_doc.items():
            rules = await run_in_threadpool(llm.draft_rules, clauses, d, case.known_facts(),
                                            case.field_values(), case.bool_facts())
            doc_sha = (cat.get(d) or {}).get("pdf_sha256", "")
            for r in rules:
                r["_doc_id"] = d
                if not str(r.get("id", "")).startswith(d + ":"):
                    r["id"] = f"{d}:{r.get('id')}"
                # Bind the citation to the exact file the reviewer will be shown
                # (TICKETS.md A1): if the PDF later changes, the mismatch is detectable.
                if doc_sha:
                    cit = r.get("citation") or {}
                    cit["doc_sha256"] = doc_sha
                    r["citation"] = cit
            drafted.extend(rules)
            needs.extend(getattr(llm.draft_rules, "last_needs_data", []) or [])
    for call in trail:
        led.append("llm.call", {**call, "scenario": name}, actor="llm")

    # Keep only rules of the scenario's own type — a currency paystub authors currency
    # rules, not the vacation-accrual clause that happened to sit near a holiday clause.
    # Cross-type clauses that surfaced in retrieval are answered by policy Q&A, not costed.
    want_type = scenario.get("result_type", "currency")
    drafted = [r for r in drafted if r.get("result_type", "currency") == want_type]

    # 4. Verify the drafted set against the paystub, and only offer it if it reproduces.
    ok, detail = _check_golden(case, _with_live(case, drafted), scenario)

    # 5. Merge into the review queue (replace this scenario's prior drafts + gaps).
    #    Locked: the queue may have changed while the model was drafting.
    for r in drafted:
        r["_scenario"] = name
    for n in needs:
        n["_scenario"] = name
    async with _rules_locked():
        prior = _proposed_queue()
        keep = [r for r in prior.get("rules", []) if r.get("_scenario") != name]
        keep_needs = [n for n in prior.get("needs_data", []) if n.get("_scenario") != name]
        _write_proposed(case, keep + drafted, keep_needs + needs)
    led.append("authoring.draft_scenario",
               {"scenario": name, "clauses": sorted(f"{d}:{c}" for d, c in want),
                "rule_ids": [r["id"] for r in drafted], "verify": detail.get("status")},
               actor="admin")

    return {"scenario": name, "considered_clauses": sorted(f"{d}§{c}" for d, c in want),
            "drafted": drafted, "verify": detail}


@router.get("/admin/verification")
def admin_verification():
    """The known answers, what each computes, and which live rules each one proves.

    Ratification is only meaningful because the library must reproduce a known answer —
    so the known answers belong in the UI, not just in case.yaml. `proved_by` maps each
    live rule id to the PASSING known answers in which it fired; `unverified` lists the
    live rules that fired in none. Each rule is checked only at its known answer's
    inputs — that is what "proved" means here, no more (E1, E7).
    """
    case = _case()
    rules = case.rules()
    goldens = case.golden_cases()
    ingested = {d["doc_id"] for d in _catalog(case).documents()}
    results = []
    live = _live_dicts(case)
    statuses, proved = _coverage(case, live)
    exercised = set(proved)
    for g in goldens:
        detail = statuses[g["name"]]
        ok = detail.get("status") in ("pass", "pending")
        want = g.get("result_type", "currency")
        # Which documents this scenario needs, and whether each is ingested yet — so the
        # card can say "needs X (not ingested)" instead of only failing on the button.
        subs = [s for s in case.subjects() if s["name"] in set(g.get("subjects") or [])]
        units = sorted({s.get("bargaining_unit") for s in subs if s.get("bargaining_unit")})
        gov = governance.resolve(units, g.get("params", {}).get("date_iso"),
                                 case.manifest.get("sources", []))
        needs_docs = [{"doc_id": d, "ingested": d in ingested,
                       "title": (case.source_by_id(d) or {}).get("title", d)}
                      for d in (gov.doc_ids or [])]
        results.append({"name": g.get("name"), "result_type": want,
                        "source": g.get("source", ""),
                        "expected": detail.get("expected"), "actual": detail.get("actual"),
                        "passed": ok, "status": detail.get("status", "fail"),
                        "per_subject": detail.get("per_subject"),
                        # every rule that contributed to this number (base + modifiers +
                        # premiums), so the card can say which rules it exercises
                        "fired": detail.get("fired") or [],
                        "note": detail.get("note"),
                        "error": detail.get("error"),
                        "needs_docs": needs_docs,
                        "ready": all(d["ingested"] for d in needs_docs) and bool(needs_docs)})
    unverified = [{"id": r.id, "result_type": r.result_type, "topic": r.topic,
                   "doc_id": r.citation.doc_id, "clause": r.citation.clause}
                  for r in rules if r.id not in exercised]
    # "Pending" is not passing — a scenario nothing covers is unproven, not proven. It
    # simply does not BLOCK approval (see _check_golden).
    return {"goldens": results, "rule_count": len(rules),
            "unverified": unverified, "proved_by": proved,
            "pending": sum(1 for g in results if g["status"] == "pending"),
            "all_passing": (all(g["status"] == "pass" for g in results)
                            if results else None)}


@router.get("/admin/validate")
def admin_validate():
    """Static validation of the current proposed rules against the case's real fact
    vocabulary. This is what the ratify gate enforces (PRD §8A)."""
    case = _case()
    proposed = _proposed_queue().get("rules", [])
    errors = validate_rules(proposed, case.known_facts())
    return {"known_facts": sorted(case.known_facts()),
            "rule_count": len(proposed), "errors": errors,
            "valid_ids": [r.get("id") for r in proposed if r.get("id") not in errors]}


@router.post("/admin/ratify")
async def admin_ratify(request: Request):
    """Human gate: approve proposed rules -> ratified library (PRD 5.2 / §8A).

    A rule may only be ratified if it PASSES VALIDATION — its expressions must parse
    and may only reference facts that actually exist in the case's data schema. A
    human approving a broken rule is still blocked; approval is necessary, not
    sufficient.
    """
    body = await request.json()
    # Off the event loop (the gate re-runs every known answer) and under _RULES_LOCK:
    # read queue + library -> gate -> merge -> write is one critical section.
    return await run_in_threadpool(_ratify_locked, body)


def _ratify_locked(body: dict):
    with _RULES_LOCK:
        return _ratify(body)


def _ratify(body: dict):
    approver = (body.get("approver") or "").strip()
    if not approver:  # F2: a record of who approved must name someone
        return {"ratified": [], "warning": "Nothing was approved — enter the approver's "
                "name first. The approval record names the person who made it."}
    approved_ids = body.get("rule_ids")  # None -> approve all
    case = _case()
    led = case.ledger()
    proposed = _proposed_queue().get("rules", [])

    selected = [r for r in proposed
                if approved_ids is None or r.get("id") in approved_ids]
    # A denial is an audit event too — record what the reviewer rejected and why.
    if approved_ids is not None:
        denied = [r.get("id") for r in proposed if r.get("id") not in approved_ids]
        if denied:
            led.append("authoring.denied",
                       {"rule_ids": denied, "approver": approver,
                        "reason": body.get("deny_reason", "not approved by reviewer")},
                       actor="admin")
    errors = validate_rules(selected, case.known_facts())
    if errors:
        led.append("authoring.rejected",
                   {"errors": errors, "approver": approver}, actor="admin")
        return {"ratified": [], "rejected": errors,
                "warning": "Validation failed — nothing was ratified. These rules "
                           "reference facts that don't exist or can't execute. Fix the "
                           "rule (or the data schema) and re-approve."}

    # KNOWN-ANSWER GATE (E1) — see _gate_verdict for the five rules. A selection is
    # approved as a whole or not at all: no known answer may come out wrong, none that
    # reproduced may stop reproducing, the scenario a draft was made for must pass, and
    # every selected rule must fire in a passing known answer. A unit nobody has authored
    # yet stays 'pending' and never blocks an unrelated approval.
    if not selected:
        # Never let an empty/failed draft wipe a working ratified library.
        return {"ratified": [], "warning": "No proposed rules to ratify — the existing "
                "ratified library was left untouched."}
    verdict = _gate_verdict(case, selected, approver, led)
    if not verdict["approved"]:
        return verdict["response"]
    proved_by = verdict["proved_by"]
    checks = verdict["ledger_checks"]   # (golden_check seq, detail) — cited by F2 approvals

    import time
    newly = []
    for r in selected:
        r = {k: v for k, v in r.items() if k != "_doc_id"}
        r["status"] = "ratified"
        r["approver"] = approver
        r["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        newly.append(r)

    # Ratification ADDS to the library, it does not replace it. Merge the existing live
    # rules with the newly-approved ones, keyed by id (a re-approval updates in place).
    # Writing only the new selection silently dropped every previously-ratified rule:
    # approve A -> library {A}; approve B,C -> library {B,C}, and A was gone. The gate
    # above already judges the UNION (_live_dicts + selected), so the write must persist
    # that same union or the gate and the library disagree.
    existing = _raw_ratified(case)
    merged = {r["id"]: r for r in existing}
    for r in newly:
        merged[r["id"]] = r
    library = list(merged.values())

    # Full approval record: rule text + fingerprint, clause, source hash, known answers
    # (F2) plus the gate's proved_by (E1). Appended first, then approval:{seq, hash} is
    # stamped on the library entry.
    approvals.record_approvals(case, led, newly, approver, checks,
                               extra_by_rule={r.get("id"): {"proved_by": proved_by.get(r.get("id"), [])}
                                              for r in newly})
    _write_ratified(case, library)
    return {"ratified": [r.get("id") for r in newly],
            "library_size": len(library),
            "proved_by": {r.get("id"): proved_by.get(r.get("id"), []) for r in newly}}


@router.post("/admin/try_break")
async def admin_try_break(request: Request):
    """Mutate the named rule (proposed queue first, else live library) in memory and
    report, per mutant, whether a known answer catches it: {rule_id, total, caught,
    verdict, mutants: [{label, kind, caught, caught_by: {scenario, expected, actual,
    status}}]}. verdict is 'tested' (all caught), 'partly tested', or 'not actually
    tested' (none caught, or the rule fires in no passing known answer). Never writes a
    rule file; appends one `authoring.mutation_check` ledger event."""
    body = await request.json()
    rule_id = str(body.get("rule_id") or "")
    case = _case()
    rule = _rule_for_break(case, rule_id)
    if rule is None:
        return JSONResponse({"error": f"no proposed or live rule with id {rule_id!r}"},
                            status_code=404)
    out = _try_break(case, rule)
    case.ledger().append("authoring.mutation_check",
                         {"rule_id": rule_id, "total": out["total"], "caught": out["caught"],
                          "survivors": out["survivors"], "verdict": out["verdict"]},
                         actor="admin")
    return out


@router.get("/admin/mutation_report")
def admin_mutation_report():
    """The mutation check over every live rule, for the foot of the Verification tab:
    {total, caught, survivors: [{rule_id, label}], rules: [{rule_id, total, caught, verdict}]}.
    Read-only; no ledger event (it is a view, not an attempt)."""
    case = _case()
    rules = []
    survivors = []
    for r in _live_dicts(case):
        out = _try_break(case, r)
        rules.append({"rule_id": out["rule_id"], "total": out["total"],
                      "caught": out["caught"], "verdict": out["verdict"]})
        survivors.extend({"rule_id": out["rule_id"], "label": s} for s in out["survivors"])
    return {"total": sum(r["total"] for r in rules), "caught": sum(r["caught"] for r in rules),
            "survivors": survivors, "rules": rules}


def _note_imported(case, art: dict) -> None:
    """Append skeptic.imported once per artifact hash the first time THIS instance serves
    a review whose run is not in its own ledger (a baked artifact shipped in git)."""
    led = case.ledger()
    sha = art.get("artifact_sha256")
    run_id = (art.get("provenance") or {}).get("run_id")
    for ev in led.read():
        if ev.get("type") == "skeptic.imported" and ev["payload"].get("artifact_sha256") == sha:
            return
        if ev.get("type") == "skeptic.finish" and ev["payload"].get("run_id") == run_id:
            return
    led.append("skeptic.imported",
               {"rule_id": art.get("rule_id"), "artifact_sha256": sha,
                "producer": (art.get("provenance") or {}).get("producer"),
                "produced_at": (art.get("provenance") or {}).get("produced_at"),
                "fresh": art.get("fresh")}, actor="skeptic")


@router.get("/admin/skeptic")
def admin_skeptic_index():
    case = _case()
    out = {}
    for rid in _skeptic.list_reviews(case):
        art = _skeptic.load_review(case, rid) or {}
        ws = art.get("warnings", [])
        out[rid] = {"verdict": art.get("verdict"), "warnings": len(ws),
                    "high": sum(1 for w in ws if w.get("severity") == "high"),
                    "fresh": art.get("fresh"), "stale_reasons": art.get("stale_reasons", []),
                    "mode": (art.get("provenance") or {}).get("mode"),
                    "producer": (art.get("provenance") or {}).get("producer"),
                    "produced_at": (art.get("provenance") or {}).get("produced_at")}
    return {"reviews": out}


@router.get("/admin/skeptic/trail")
def admin_skeptic_trail(run_id: str):
    led = _case().ledger()
    return {"run_id": run_id,
            "events": [e for e in led.read() if e.get("type", "").startswith("skeptic.")
                       and e.get("payload", {}).get("run_id") == run_id]}


@router.get("/admin/skeptic/{rule_id:path}")
def admin_skeptic_review(rule_id: str):
    case = _case()
    art = _skeptic.load_review(case, rule_id)
    if art is None:
        return JSONResponse(
            {"error": f"no skeptic review for {rule_id}",
             "bake": f"env -u ANTHROPIC_API_KEY .venv/bin/python scripts/skeptic.py start "
                     f"--rule {rule_id} --producer claude-code-session"}, status_code=404)
    _note_imported(case, art)
    return art


@router.get("/admin/demo_draft")
def admin_demo_draft():
    """The seeded wrong draft and whether it is in the review queue:
    {rules, ids, loaded: [ids in the queue], live: [ids in the library, expected []]}."""
    return _demo_draft_status(_case())


@router.post("/admin/demo_draft/load")
def admin_demo_draft_load():
    """Merge the seeded demo draft(s) into rules/rules_proposed.json BY ID (idempotent:
    a second call changes nothing). Other queued drafts and needs_data are kept. One
    ledger event records the load; approving it is a separate, ordinary /admin/ratify."""
    case = _case()
    drafts = _demo_drafts(case)
    if not drafts:
        return JSONResponse({"error": f"no demo draft file at {DEMO_DRAFT_FILE}"},
                            status_code=404)
    with _RULES_LOCK:
        current = _proposed_queue()
        queue = list(current.get("rules", []))
        before = {r.get("id") for r in queue}
        merged = {r.get("id"): r for r in queue}
        for r in drafts:
            merged[r["id"]] = r
        added = [r["id"] for r in drafts if r["id"] not in before]
        if added:
            _write_proposed(case, list(merged.values()), current.get("needs_data") or [])
    if added:
        case.ledger().append("authoring.demo_draft_loaded",
                             {"rule_ids": added, "file": DEMO_DRAFT_FILE,
                              "note": "deliberately wrong draft seeded for the gate demo; "
                                      "not approved, nothing computes from it"},
                             actor="admin")
    return {"loaded": [r["id"] for r in drafts], "added": added,
            "already_loaded": not added, "queue_size": len(merged)}


@router.get("/admin/cell_gate")
def admin_cell_gate(rule_id: str = ""):
    """What the cell-evidence gate says about the live library (or one rule, proposed
    queue first): [{rule_id, cell_ref, cell: {stored, reread, status, confirmed},
    errors, approvable}]. Read-only, no ledger event — the demo's 'before' and
    'after' view around POST /admin/cell_confirm."""
    case = _case()
    if rule_id:
        rule = _rule_for_break(case, rule_id)
        if rule is None:
            return JSONResponse({"error": f"no proposed or live rule with id {rule_id!r}"},
                                status_code=404)
        rules = [rule]
    else:
        rules = _live_dicts(case)
    rows = _cell_gate_rows(case, rules)
    return {"rules": rows, "blocked": [r["rule_id"] for r in rows if not r["approvable"]]}


@router.post("/admin/cell_confirm")
async def admin_cell_confirm(request: Request):
    """A person confirms what a disputed table cell says on the page image:
    {doc_id, page, row, col, value, by, note?}. Writes cells[i].confirmed into
    cell_checks.json (stored and re-read values stay beside it) and appends the
    ledger event `cell.confirm`. `row` is the row's ordinal among the page's
    table-row clauses (header = 0), `col` the grid column, as /doc/{id}/clauses and
    the Compare view number them. 400 without a value or a name; 404 for a cell the
    verifier never recorded."""
    body = await request.json()
    try:
        doc_id = str(body.get("doc_id") or "")
        page, row, col = int(body["page"]), int(body["row"]), int(body["col"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "doc_id, page, row and col are required"}, status_code=400)
    case = _case()
    try:
        async with _rules_locked():   # cell_checks.json read-modify-write + gate read
            cell = cellcheck.confirm_cell(case_dir=case.dir, doc_id=doc_id, page=page,
                                          row=row, col=col, value=body.get("value"),
                                          by=body.get("by") or "",
                                          note=str(body.get("note") or ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except KeyError as e:
        return JSONResponse({"error": str(e).strip("'")}, status_code=404)
    ev = case.ledger().append("cell.confirm", {
        "doc_id": doc_id, "page": page, "row": row, "col": col,
        "header": cell.get("header"), "stored": cell.get("stored"),
        "reread": cell.get("reread"), "reread_score": cell.get("score"),
        "status_before": cell.get("status"), "value": cell["confirmed"]["value"],
        "by": cell["confirmed"]["by"], "note": cell["confirmed"]["note"],
        "agrees_with_reread": cell["confirmed"]["agrees_with_reread"],
        "agrees_with_stored": cell["confirmed"]["agrees_with_stored"],
    }, actor="admin")
    # live rules AND the Review queue: the demo confirms a cell so a QUEUED tier can be
    # approved, so the response must name the queued rules the confirm unblocked
    judged = _live_dicts(case) + list(_proposed_queue().get("rules", []))
    now_blocked = {r["rule_id"] for r in _cell_gate_rows(case, judged)
                   if not r["approvable"]}
    return {"ok": True, "cell": cell, "ledger_seq": ev["seq"],
            "unblocked": [r.get("id") for r in judged
                          if (r.get("citation") or {}).get("cell")
                          and r.get("id") not in now_blocked]}
