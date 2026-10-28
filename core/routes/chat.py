"""POST /chat (prompt -> route -> deterministic engine -> cited answer) and its audit trail."""
from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import (  # search-tree (L1, L3, C1/I13)
    audit,
    costing,  # costing-correctness (B1, B2, B4)
    decisions,
    governance,
    llm,
    mathline,
    queryfacts,
    refusal,
)
from .. import qid as qid_mod
from ..ruledsl import SHIFT_BASES, rule_inputs
from ..services.answers import (
    _ASKS_EVERYONE_RE,
    _clarify,
    _entitlement_answer,
    _input_decision,
    _nearest_clauses,
    _policy_answer,
)
from ..services.rulestore import _raw_ratified
from ..services.sources import _doc_integrity, _doc_meta, _enrich_citations
from ..services.state import DEFAULT_YEAR, _backend, _case, _catalog, _extraction

router = APIRouter()


# --------------------------------------------------------------------------- #
# CHAT
# --------------------------------------------------------------------------- #
@router.post("/chat")
async def chat(request: Request):
    """Public entrypoint. Wraps the handler so that EVERY AI call made while answering
    lands in the ledger, whichever of the handler's several exits is taken — including
    the ones that refuse, and including the ones that fail.

    A try/finally rather than draining before each return: an exit that forgets to record
    is an answer with no evidence of how it was reached, and the handler grows exits.
    """
    body = await request.json()
    led = _case().ledger()
    # The id is ALWAYS server-issued (A1): it names the snapshot file and threads the
    # ledger, so a client value is only ever a reference to an earlier query.
    qid = qid_mod.mint()
    continues = qid_mod.continuation(led, body.get("query_id"))
    with llm.record() as trail:
        try:
            return await _chat(body, qid, continues)
        finally:
            for call in trail:
                led.append("llm.call", call, actor="llm", query_id=qid)


async def _chat(body: dict, qid: str, continues: str | None = None):
    prompt = (body.get("prompt") or "").strip()
    forced_doc = body.get("doc_id")  # set when the user confirms a document

    case = _case()
    led = case.ledger()
    cat = _catalog(case)

    department = body.get("department")  # set when the user answers "which department?"
    # Every query — follow-ups included — opens with its own chat.prompt, so the trail
    # of a confirmed-document answer starts at a prompt and `continues` links it back.
    led.append("chat.prompt", {"text": prompt, "department": department,
                               "doc_id": forced_doc, "continues": continues},
               actor="chat", query_id=qid)
    if forced_doc:
        # B2: a caller-supplied document never replaces governance on the costing path.
        # The question is still recorded (chat.prompt above) and still routed — it just
        # cannot pick its own contract.
        led.append("costing.doc_id_ignored", {"doc_id": forced_doc,
                   "reason": "documents are chosen by governance per bargaining unit"},
                   actor="chat", query_id=qid)
    # Router: costing vs policy Q&A. Both return an answer WITH clickable proof.
    intent = await run_in_threadpool(llm.classify_intent, prompt)   # A8: off the loop
    led.append("intent.classify", {"intent": intent}, actor="chat", query_id=qid)
    _by = decisions.decided_by_for("classify_intent")                # search-tree (L1)
    decisions.record(led, qid, "intent", *decisions.intent_explained(prompt, intent, _by), _by)
    if intent == "policy":
        return await run_in_threadpool(_policy_answer, case, led, qid, prompt, department)
    if intent == "lookup":
        return await run_in_threadpool(_policy_answer, case, led, qid, prompt,
                                       department, lookup=True)
    if intent == "entitlement":
        return await run_in_threadpool(_entitlement_answer, case, led, qid, prompt,
                                       department)

    # --- costing-correctness (B1, B2, B4) --------------------------------------- #
    # A clarifying answer comes back as {prompt, query_id, clarified: {<field>: value}}.
    # Only the field the server asked for is read, and only a value it can verify.
    clarified = body.get("clarified") if isinstance(body.get("clarified"), dict) else {}
    extraction_cfg = _extraction(case)

    # 1. Parse intent (LLM translation layer, with deterministic fallback).
    subjects_all = case.subjects()
    labels_all = [str(s.get("name", "")) for s in subjects_all]
    params = await run_in_threadpool(llm.parse_intent, prompt, extraction_cfg,
                                     subjects_all)
    led.append("llm.parse_intent", params, actor="chat", query_id=qid)

    # A model-extracted number the question doesn't contain never reaches the engine
    # (TICKETS.md B3) — ask, don't multiply by it.
    unverified = params.get("unverified_numbers")
    if unverified:
        led.append("costing.clarify",
                   {"reason": "model-extracted number absent from the question",
                    "unverified": unverified}, actor="chat", query_id=qid)
        return _clarify(qid, prompt,
                        f"I read {unverified.get('hours')} hours out of that, but the "
                        "question doesn't state that number. How long is the shift?")

    # 2. Select the subjects named in the prompt, and derive their bargaining unit(s)
    #    + the shift date — the governance keys. A question that names NOBODY is asked
    #    who it is for (TICKETS.md B4) — silently costing the entire roster turned a
    #    vague question into one large confident total.
    named = set(params.get("subjects") or [])
    chosen_subject = clarified.get("subject")
    if chosen_subject in labels_all:
        named = {chosen_subject}
        params["subjects"] = [chosen_subject]
    subjects = [s for s in subjects_all if s.get("name") in named]
    asks_everyone = bool(_ASKS_EVERYONE_RE.search(prompt))
    if not subjects:
        if asks_everyone:
            subjects = subjects_all
        else:
            led.append("costing.clarify", {"reason": "no subject named"},
                       actor="chat", query_id=qid)
            examples = ", ".join(str(s.get("name")) for s in subjects_all[:3])
            return _clarify(qid, prompt,
                            "Who is this for? Name a classification (e.g. "
                            f"{examples}).")
    # B4: a description that matches several classifications is a question, not a sum —
    # unless a label was typed verbatim or the question asked for a group.
    if len(subjects) > 1 and not asks_everyone and not chosen_subject:
        pl = prompt.lower()
        verbatim = any(str(s.get("name", "")).lower() in pl for s in subjects)
        ranks = {str(s.get("rank", "")).lower() for s in subjects if s.get("rank")}
        plural = any(re.search(rf"\b{re.escape(r)}s\b", pl) for r in ranks if r)
        if not verbatim and not plural:
            opts = [str(s.get("name")) for s in subjects]
            led.append("costing.clarify", {"reason": "several classifications match",
                                           "options": opts},
                       actor="chat", query_id=qid)
            res = _clarify(qid, prompt, "Which classification? That description matches "
                           f"{len(opts)} rows on the roster.", opts)
            res["field"] = "subject"
            return res
    units = sorted({s.get("bargaining_unit") for s in subjects if s.get("bargaining_unit")})
    date_iso = governance.parse_date(params.get("date"), default_year=DEFAULT_YEAR)
    year_stated = bool(re.search(r"\b(19|20)\d{2}\b", str(params.get("date") or "")))
    # search-tree (L1): the roster rows the question did not name, and why.
    decisions.record(led, qid, "subject",
                     *decisions.subject_explained(
                         prompt, subjects_all, [str(s.get("name")) for s in subjects],
                         "answered the clarifying question" if chosen_subject
                         else "everyone asked for" if asks_everyone and not named
                         else "named in the question"),
                     decisions.USER if chosen_subject
                     else decisions.AI if params.get("source") == "claude" else decisions.FIXED)
    led.append("data.read",
               {"adapter": case.manifest.get("data", {}).get("adapter"),
                "rows": [s.get("name") for s in subjects],
                "bargaining_units": units, "shift_date": date_iso,
                "fields": list((subjects[0].keys() if subjects else [])),
                "records": subjects, "data_sha256": audit.data_sha256(case)},
               actor="chat", query_id=qid)

    # 3. Hours (B4): the number the question STATES, with roster labels masked so the
    #    '56' in '(56 hr, top step)' is never a shift length. One clean candidate goes
    #    to the engine; anything else is asked about, never guessed.
    hx = costing.extract_hours(prompt, labels_all)
    cands = hx["candidates"]
    hours_max = costing.hours_limit(extraction_cfg)
    hours = None
    picked = clarified.get("hours")
    if picked is not None:
        try:
            pv = float(picked)
        except (TypeError, ValueError):
            pv = None
        if pv is not None and pv in cands and 0 < pv <= hours_max:
            hours = pv
    if hours is None and len(cands) == 1 and not hx["invalid"] and 0 < cands[0] <= hours_max:
        hours = cands[0]
    if hours is None:
        if not cands and not hx["invalid"]:
            led.append("costing.clarify", {"reason": "no hours stated"},
                       actor="chat", query_id=qid)
            res = _clarify(qid, prompt, "How many hours? The question doesn't state the "
                           "length of the shift.")
            res["field"] = "hours"
            return res
        valid = [c for c in cands if 0 < c <= hours_max]
        led.append("costing.clarify",
                   {"reason": "hours ambiguous", "candidates": cands,
                    "invalid": hx["invalid"], "max": hours_max},
                   actor="chat", query_id=qid)
        if valid:
            q = ("Which of these is the length of the shift? I read "
                 + ", ".join(f"{c:g} hours" for c in cands)
                 + (f" (and could not read: {', '.join(hx['invalid'])})" if hx["invalid"] else "")
                 + ".")
        else:
            q = ("How many hours? I could not read a usable shift length from the "
                 "question" + (f" ({', '.join(hx['invalid'])})" if hx["invalid"] else "")
                 + (f"; the longest shift I will cost is {hours_max:g} hours" if cands else "")
                 + ".")
        res = _clarify(qid, prompt, q, [f"{c:g}" for c in valid])
        res["field"] = "hours"
        return res
    params["hours"] = hours

    # 4. Pay type (B1): which branch of pay the question asks for — read from the case's
    #    lexicon, never from the model. Nothing asked -> ask, listing what is approved.
    lexicon = costing.pay_type_lexicon(extraction_cfg)
    pt = costing.detect_pay_type(prompt, lexicon)
    asked = list(pt["asked"])
    if clarified.get("pay_type") in lexicon:
        asked = [clarified["pay_type"]]
    approved_all = costing.base_topics([r for r in case.rules()
                                        if r.citation.doc_id in {
                                            s.get("id") for s in case.manifest.get("sources", [])
                                            if s.get("bargaining_unit") in units}])
    if not asked:
        led.append("costing.clarify", {"reason": "pay type not stated",
                                       "approved_topics": approved_all,
                                       "negated": pt["negated"]},
                   actor="chat", query_id=qid)
        res = _clarify(qid, prompt, "Which kind of pay? Approved for this unit: "
                       + (", ".join(approved_all) or "none yet") + ".", approved_all)
        res["field"] = "pay_type"
        return res
    pay_type = asked[0] if len(asked) == 1 else "+".join(asked)
    params["pay_type"] = pay_type
    interp = costing.interpretation(
        hours, pay_type, [str(s.get("name")) for s in subjects], date_iso, year_stated,
        str(params.get("source") or "stub"),
        checks=[{"field": "hours", "deterministic": hours,
                 "model": params.get("hours") if params.get("source") == "claude" else None,
                 "agree": True},
                {"field": "pay_type", "deterministic": pay_type, "model": None, "agree": True}],
        years_of_service=params.get("years_of_service"),
        years_note=params.get("years_note"))
    led.append("chat.interpretation", interp, actor="chat", query_id=qid)

    # 5. Per-unit governance + engine (B2). Each bargaining unit resolves to ITS contract
    #    and ITS ratified rules; an uncovered unit gets a reason, never another unit's rule.
    eng_params = {"hours": hours,
                  "date": params.get("date", ""),
                  "date_iso": date_iso or "",
                  "holiday_weekday": params.get("holiday_weekday", ""),
                  "pay_type": pay_type}
    eng_params.update(queryfacts.engine_extras(case, params))  # J1a: years_of_service etc.
    outcomes = costing.cost_by_unit(case, cat, led, qid, subjects, eng_params, date_iso,
                                    asked, _doc_integrity, _raw_ratified)
    ok = [o for o in outcomes if o["status"] == "ok"]
    refused = [o for o in outcomes if o["status"] != "ok"]
    uncovered = [{"subject": n, "bargaining_unit": o["bargaining_unit"],
                  "status": o["status"], "reason": o["reason"],
                  "governing_docs": o["doc_ids"], "approved_topics": o["approved_topics"]}
                 for o in refused for n in o["subjects"]]
    for o in refused:
        led.append("costing.uncovered",
                   {"bargaining_unit": o["bargaining_unit"], "subjects": o["subjects"],
                    "status": o["status"], "reason": o["reason"], "doc": o["doc_ids"],
                    "stale": o["stale"], "approved_topics": o["approved_topics"]},
                   actor="engine", query_id=qid)

    if not ok:
        gov_docs = sorted({d for o in refused for d in o["doc_ids"]})
        chosen = ", ".join(gov_docs)
        statuses = {o["status"] for o in refused}
        if statuses & {"no_rule_for_pay_type", "no_rule_for_scenario"}:
            # B1: the unit has approved rules, just not for THIS branch of pay. Refuse
            # with the reason, what IS approved, and the closest clause — labelled as
            # text, not as an answer. No snapshot: nothing was computed.
            approved = sorted({t for o in refused for t in o["approved_topics"]})
            reason = "; ".join(o["reason"] for o in refused)
            nearest = _nearest_clauses(case, cat, prompt, gov_docs)
            # The clause(s) that ARE approved for the unit, as chips that open the page:
            # the visitor sees what the unit's rulebook covers instead of this question.
            approved_clauses = []
            for o in refused:
                for ar in o.get("approved_rules", []):
                    c = ar["citation"]
                    approved_clauses.append({**_doc_meta(case, c.get("doc_id", "")),
                                             "rule_id": ar["rule_id"], "topic": ar["topic"],
                                             "clause": c.get("clause", ""),
                                             "page": c.get("page", 0), "bbox": c.get("bbox", []),
                                             "text": ar["human_readable"]})
            led.append("costing.refused",
                       {"asked": asked, "approved_topics": approved, "units": units,
                        "reason": reason, "doc": gov_docs,
                        "nearest": [{"doc_id": n["doc_id"], "page": n["page"]} for n in nearest]},
                       actor="engine", query_id=qid)
            return {"query_id": qid, "needs_confirmation": False, "mode": "refused",
                    "chosen_doc": chosen, "bargaining_units": units, "shift_date": date_iso,
                    "asked": asked, "approved_topics": approved, "reason": reason,
                    "message": ("I can't compute that: " + reason + ". "
                                "The closest clause is shown below as text — it is not a "
                                "computed answer."),
                    "nearest": nearest, "nearest_label": "closest text — not a computed answer",
                    "approved_clauses": approved_clauses,
                    "uncovered": uncovered, "interpretation": interp, "params": params}
        # No unit could be costed for a structural reason (no contract, no rules, stale,
        # provenance). Single unit keeps the long-standing message; several list each.
        o = refused[0]
        led.append("costing.blocked",
                   {"reason": {"no_contract": "no governing contract",
                               "stale": "rules pending re-verification",
                               "no_rules": "no ratified rules",
                               "integrity": "provenance mismatch",
                               "rule_error": "rule evaluation error"}.get(o["status"], o["status"]),
                    "error": o.get("error"),   # A4: the exception text, when a rule failed
                    "doc": gov_docs, "stale": [s for r in refused for s in r["stale"]],
                    "units": units, "per_unit": [{"bargaining_unit": r["bargaining_unit"],
                                                  "status": r["status"], "reason": r["reason"]}
                                                 for r in refused]},
                   actor="engine", query_id=qid)
        # One helper builds every blocked answer (chat-ui I6): contract named by title,
        # one plain sentence, a `next` link to an admin tab that exists.
        if len(refused) == 1:
            kind = {"no_contract": "no_contract", "stale": "stale_rules",
                    "integrity": "provenance", "rule_error": "rule_error"}.get(o["status"],
                                                                               "no_rules")
            detail = o["reason"] if kind == "no_contract" else \
                o.get("error") if kind == "rule_error" else \
                o["reason"] if kind == "provenance" else None
            res = refusal.blocked(case, qid, gov_docs, subjects, kind, detail=detail,
                                  stale=[st for st in o["stale"]])
        else:
            res = refusal.blocked(case, qid, gov_docs, subjects, "not_covered",
                                  detail="; ".join(f"{', '.join(r['subjects'])}: {r['reason']}"
                                                   for r in refused))
        res.update({"bargaining_units": units, "shift_date": date_iso,
                    "uncovered": uncovered, "interpretation": interp})
        return res

    # 6. Ledger: decision logic + math + citations, straight from the engine trace.
    #    (rule.* and citation payloads keep their shape; bargaining_unit is added.)
    # search-tree (C1/I13): the arithmetic as a person writes it, each factor with an
    # address — built BEFORE the events so the ledger carries the rate and its cell.
    math_lines: dict[str, dict] = {}
    for o in ok:
        for li in o["line_items"]:
            subj = next((s for s in subjects if str(s.get("name")) == li["subject"]), {})
            ml = mathline.build(case, cat, subj, li, eng_params)
            if ml:
                math_lines[li["subject"]] = ml
    # search-tree (L2): every input a fired rule DECLARES becomes a sub-search node under
    # that rule — a clause input is re-found by search and ranked, a roster or question
    # input is a leaf with the value the engine used. At most a handful of searches.
    _backend_ = _backend(case)
    _rate_refs = {s: (m["operands"][0].get("source") or {}) for s, m in math_lines.items()
                  if m["operands"] and m["operands"][0].get("role") == "rate"}
    for o in ok:
        for r in o["rules_used"]:
            for inp in rule_inputs(r):
                _input_decision(led, qid, _backend_, o, r, inp, subjects, eng_params, _rate_refs)
    for o in ok:
        for li in o["trace_items"]:
            for step in li.trace:
                if step.kind in ("modifier", "selector-considered", "selector-chosen", "math", "flag", "premium"):
                    payload = {"subject": li.subject, "rule_id": step.rule_id,
                               "detail": step.detail, "value": step.value,
                               "bargaining_unit": o["bargaining_unit"],
                               "inputs": step.inputs}                    # I13 (additive)
                    if step.kind == "math" and li.subject in math_lines:
                        payload.update({k: math_lines[li.subject][k]
                                        for k in ("expr", "operands", "line")})
                    led.append(f"rule.{step.kind.replace('-', '_')}", payload,
                               actor="engine", query_id=qid)
            for c in li.citations:
                led.append("citation", {"subject": li.subject, **c},
                           actor="engine", query_id=qid)

    # 7. Snapshot + answer. Covered lines only; the uncovered rows ride alongside with
    #    their reasons, and the total is the sum of what was actually computed.
    line_items = [li for o in ok for li in o["line_items"]]
    total = costing.sum_lines(line_items, case.rounding_places())
    rules_used: list = []
    seen_rule_ids: set[str] = set()
    for o in ok:
        for r in o["rules_used"]:
            if r.id not in seen_rule_ids:
                seen_rule_ids.add(r.id)
                rules_used.append(r)
    result_dict = {"total": total, "line_items": line_items, "uncovered": uncovered,
                   "partial": bool(uncovered),
                   "per_unit": [{"bargaining_unit": o["bargaining_unit"], "status": o["status"],
                                 "reason": o["reason"], "governing_docs": o["doc_ids"],
                                 "rules": [r.id for r in o["rules_used"]],
                                 "subjects": o["subjects"]} for o in outcomes]}
    # Schema-2 snapshot bound to the chain (F1). The frozen result is the engine's own
    # shape over the covered subjects and the rules actually used, so a replay recomputes
    # it exactly; the uncovered rows and per-unit detail live in the response only.
    covered_names = {n for o in ok for n in o["subjects"]}
    covered_subjects = [sd for sd in subjects if str(sd.get("name")) in covered_names]
    engine_result = {"total": total,
                     "line_items": [{k: v for k, v in li.items()
                                     if k not in ("bargaining_unit", "governing_docs")}
                                    for li in line_items]}
    audit.record_answer(case, led, qid, eng_params, covered_subjects, rules_used,
                        engine_result, basis_scope=SHIFT_BASES,
                        extra={"partial": bool(uncovered), "uncovered": uncovered,
                               # C1: the same operands the drawer shows, bound to the chain
                               "math": [{"subject": s, "line": m["line"], "expr": m["expr"],
                                         "operands": m["operands"]}
                                        for s, m in math_lines.items()]})

    result_dict = _enrich_citations(cat, result_dict)
    # After the snapshot (its stored result must replay byte-for-byte from the engine):
    # the response's math step carries the line and its sourced operands (C1/I13).
    for li in result_dict["line_items"]:
        ml = math_lines.get(li["subject"])
        if not ml:
            continue
        for t in li.get("trace", []):
            if t.get("kind") == "math":
                t.update({"line": ml["line"], "expr": ml["expr"], "operands": ml["operands"]})
    chosen_docs = []
    for o in ok:
        for d in o["doc_ids"]:
            if d not in chosen_docs:
                chosen_docs.append(d)
    return {"query_id": qid, "needs_confirmation": False, "mode": "costing",
            "chosen_doc": ", ".join(chosen_docs),
            "chosen_docs": [_doc_meta(case, d) for d in chosen_docs],   # chat-ui (I4)
            "routing_path": "governance",
            "bargaining_units": units, "shift_date": date_iso, "params": params,
            "partial": bool(uncovered), "interpretation": interp, "result": result_dict}


@router.get("/chat/audit/{query_id}")
def chat_audit(query_id: str):
    if not qid_mod.is_qid(query_id):
        return JSONResponse({"error": "unknown query id"}, status_code=404)
    case = _case()
    # A follow-up ("which department?" answered) is its own query that `continues` the
    # one it clarifies; the drawer shows the whole conversation, parents first (A1).
    events, continues = qid_mod.trail_with_parents(case.ledger(), query_id)
    # Summarise the AI involvement up front. "How did AI reach this answer" starts with
    # whether AI was involved at all — and with a deterministic fallback behind every
    # touchpoint, that is a real question with a non-obvious answer.
    calls = [e["payload"] for e in events if e.get("type") == "llm.call"]
    ai = {"calls": calls,
          "used_model": any(c.get("source") == "claude" for c in calls),
          "fell_back": [c["fn"] for c in calls if c.get("source") == "fallback"],
          "errors": [c for c in calls if c.get("source") == "error"],
          "total_ms": sum(c.get("ms", 0) for c in calls)}
    return {"query_id": query_id, "events": events, "ai": ai, "continues": continues,
            "tree": audit.build_tree(events, corpus_size=len(case.manifest.get("sources", [])))}
