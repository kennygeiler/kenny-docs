"""Non-costing answers for /chat: policy and entitlement questions, clarifications,
out-of-scope refusals, nearest clauses, and the input-decision ledger record.
"""
from __future__ import annotations

import copy
import re

from .. import (
    audit,
    costing,  # costing-correctness (B1, B2, B4)
    decisions,  # search-tree (L1, L3, C1/I13)
    governance,
    llm,
    queryfacts,
    rulematch,
)
from ..engine import calculate, rounding_policy
from ..ruledsl import RuleError
from .sources import _doc_meta, _enrich_citations, _source_entry
from .state import DEFAULT_YEAR, _backend, _catalog, _extraction


def _is_rate_row(hit: dict) -> bool:
    """A retrieved chunk that is a row of a rate table rather than prose about pay."""
    text = hit.get("text") or ""
    return "|" in text and "$" in text


def _dept_scope(case, cat, dept) -> list[str]:
    """Candidate documents for a department, restricted to what is actually INGESTED.

    The ONE scoping helper for every retrieval path (TICKETS.md B1). It encodes two
    hard-won rules together so no path can drift with only one of them again:
      1. case.yaml declares the intended corpus; the catalog holds what really parsed.
         A document with no clauses in the index is a plan, not a candidate.
      2. Callers must treat an EMPTY scope as "nothing to search" — never pass [] to
         backend.search, which reads it as "no filter" and would leak every other
         department's contracts into the answer.
    """
    ingested = {d["doc_id"] for d in cat.documents()}
    return [d for d in case.docs_for_department(dept) if d in ingested]


_ASKS_EVERYONE_RE = re.compile(r"\b(all|every|each|entire|whole|everyone|roster)\b", re.I)


def _clarify(qid: str, prompt: str, question: str, options: list | None = None) -> dict:
    return {"query_id": qid, "mode": "clarify", "prompt_echo": prompt,
            "question": question, "options": options or []}


def _titles(case, doc_ids: list[str]) -> list[str]:
    return [rulematch.doc_short_title(case.source_by_id(d) or {"id": d}) or d for d in doc_ids]


def _out_of_scope(case, qid: str, prompt: str, scope: list[str], named: list[str],
                  reason: str, coverage: float | None = None) -> dict:
    """The refusal for a question these documents do not answer (DEMO_TICKETS.md B6).
    Names the documents it checked so the visitor knows what WAS searched; never
    offers a department, because no department holds a heat-pump rebate."""
    if named:
        titles = _titles(case, named)
        answer = ("I could not find that in " + " or ".join(titles)
                  + " — it is not covered by that document.")
    else:
        titles = _titles(case, scope)
        answer = (f"That is not covered by the {len(titles)} documents I have: "
                  + "; ".join(titles) + ".")
    return {"query_id": qid, "mode": "out_of_scope", "prompt_echo": prompt,
            "answer": answer, "answer_source": "none", "reason": reason,
            "coverage": coverage, "options": [], "sources": [],
            "considered": [_doc_meta(case, d) for d in scope]}


def _policy_answer(case, led, qid: str, prompt: str, department: str | None = None,
                   lookup: bool = False, doc_scope: list[str] | None = None) -> dict:
    """Policy Q&A over a multi-document CORPUS (DEMO_TICKETS.md B6).

    1. Scope: the document the question names, else the bargaining unit of the
       classification it names, else the stated department, else the corpus. A caller
       that already resolved governance passes `doc_scope` (the entitlement fallback),
       so a quote can only come from the subject's own contract.
    2. Query: the prompt minus the words that named the scope and the question frame.
    3. Search within the scope; pin the cited clause of any ratified rule whose topic
       is in the question, so the quote boxes the same clause the costing answer did.
    4. Relevance floor: when the best hit covers too little of the question, say it is
       not in these documents instead of quoting the least-irrelevant preamble.
    5. Answer strictly from retrieved text, with every document considered and used.

    `lookup=True` serves a figure the documents already PUBLISH — a rate off a salary
    schedule. Same spine; the schedule is always in scope and only rows that carry the
    named classification's words are floated.
    """
    cat = _catalog(case)
    backend = _backend(case)
    ingested = {d["doc_id"] for d in cat.documents()}
    subjects_all = case.subjects()

    # 1. Scope.
    if doc_scope is not None:
        sc = rulematch.Scope([d for d in doc_scope if d in ingested], "unit")
        if lookup:
            for s in case.manifest.get("sources", []):
                if s.get("doc_type") == "salary-schedule" and s["id"] in ingested \
                        and s["id"] not in sc.doc_ids:
                    sc.doc_ids.append(s["id"])
    else:
        sc = rulematch.docs_for(case, ingested, prompt, department, lookup, subjects_all)
    scope = sc.doc_ids
    dept = sc.department or (department if department in case.departments() else None)
    led.append("policy.department", {"department": dept, "stated": bool(department),
                                     "known_departments": case.departments(),
                                     "scope_how": sc.how, "named_docs": sc.named,
                                     "bargaining_units": sc.units},
               actor="chat", query_id=qid)
    led.append("retrieval.candidates",
               {"department": dept, "candidate_docs": scope, "corpus_size": len(ingested),
                "declared": len(case.manifest.get("sources", [])), "scope_how": sc.how},
               actor="chat", query_id=qid)
    decisions.record(led, qid, "document_scope",                      # search-tree (L1)
                     [{"id": d, "label": _titles(case, [d])[0], "detail": f"in scope by {sc.how}"}
                      for d in scope],
                     [{"id": d, "label": _titles(case, [d])[0],
                       "reason": (f"not the document the question named" if sc.named
                                  else f"outside the {sc.how} scope")}
                      for d in sorted(ingested) if d not in scope],
                     decisions.FIXED,
                     detail={"how": sc.how, "named": sc.named, "department": dept})

    # 2. Query: nothing left ('hi', or only a document's name) -> not a question these
    #    documents answer.
    query, stems = rulematch.retrieval_query(prompt, sc.consumed)
    if not stems:
        led.append("policy.out_of_scope", {"reason": "no content terms", "query": query},
                   actor="chat", query_id=qid)
        return _out_of_scope(case, qid, prompt, scope, sc.named, "no content terms")

    # 3. Search within candidates. An empty scope means nothing relevant has been
    #    ingested yet — do NOT call search([]), which the backend reads as "no filter".
    # search-tree (L1): fetch six past the cutoff so the hits that LOST are on record;
    # the answer still reads hits[:8] exactly as before.
    _raw_hits = backend.search(query, doc_ids=scope, k=8 + 6) if scope else []
    hits, _below = _raw_hits[:8], _raw_hits[8:]
    pins: list[dict] = []
    if hits:
        rules = [r for r in case.rules() if r.citation.doc_id in scope]
        hits, pins = rulematch.pin_rule_clauses(
            rules, hits, stems, scope, rulematch.chunk_lookup(backend, cat))
    not_found: list[str] = []
    quote_rows = 3                      # rows the no-key lookup wording may quote
    if lookup:
        word_sets = rulematch.classification_words(sc.subjects)
        rows = [h for h in hits if _is_rate_row(h)]
        if word_sets:
            matching = [h for h in rows if rulematch.row_matches(h, word_sets)]
            if matching:
                hits = matching + [h for h in hits if h not in matching]
                quote_rows = min(quote_rows, len(matching))
            else:
                not_found = sorted({str(s.get("rank") or s.get("name")) for s in sc.subjects})
        else:
            hits.sort(key=lambda h: 0 if _is_rate_row(h) else 1)
    led.append("retrieval.hits",
               {"query": query,
                "hits": [{k: h.get(k) for k in ("doc_id", "clause", "page", "score", "pinned_by")}
                         for h in hits]},
               actor="chat", query_id=qid)

    # 4. Relevance floor, before any clarify: an off-corpus question must not be asked
    #    which department it is about. A lookup that named a classification the
    #    schedule has no row for is answered by name first — that IS the finding.
    cov = rulematch.best_coverage(stems, hits, rulematch.idf_table(backend))
    decisions.record(led, qid, "clause_retrieval",
                     *decisions.retrieval_lists(
                         hits, _below, 8,
                         f"best coverage {cov:.2f} < floor {rulematch.OUT_OF_SCOPE_FLOOR}"
                         if hits and cov < rulematch.OUT_OF_SCOPE_FLOOR else None),
                     decisions.FIXED,
                     counts={"searched": len(_raw_hits), "scope_docs": len(scope)},
                     detail={"query": query, "scope": scope, "coverage": round(cov, 3)})
    if not_found:
        led.append("policy.answer", {"answer": "no published rate row", "source": "none",
                                     "lookup": True, "scope_how": sc.how,
                                     "not_found": not_found, "coverage": round(cov, 3)},
                   actor="chat", query_id=qid)
        return {"query_id": qid, "mode": "lookup", "answer_source": "none",
                "answer": (f"I couldn't find a published rate row for {', '.join(not_found)}"
                           f" in {' or '.join(_titles(case, scope)) or 'these documents'}."),
                "sources": [], "department": dept, "scope_how": sc.how,
                "not_found": not_found, "coverage": round(cov, 3),
                "considered": [_doc_meta(case, d) for d in scope]}
    if hits and cov < rulematch.OUT_OF_SCOPE_FLOOR:
        led.append("policy.out_of_scope", {"reason": "below relevance floor",
                                           "coverage": round(cov, 3), "query": query},
                   actor="chat", query_id=qid)
        return _out_of_scope(case, qid, prompt, scope, sc.named, "below relevance floor",
                             round(cov, 3))
    if not hits and (sc.how == "corpus" or sc.named):
        led.append("policy.out_of_scope", {"reason": "no hits", "query": query},
                   actor="chat", query_id=qid)
        return _out_of_scope(case, qid, prompt, scope, sc.named, "no hits", 0.0)

    # If nothing scoped the question and the evidence spans several bargaining units,
    # the answer differs by contract -> ask which document (PRD §3.3 never bluff).
    # Options are document titles, one per unit; the salary schedule is never offered.
    if sc.how == "corpus" and hits:
        by_unit: dict[str, str] = {}
        for h in hits:
            s = case.source_by_id(h["doc_id"]) or {}
            u = s.get("bargaining_unit")
            if u and u not in by_unit:
                by_unit[u] = rulematch.doc_short_title(s) or h["doc_id"]
        if len(by_unit) > 1:
            options = list(by_unit.values())
            led.append("policy.clarify",
                       {"reason": "evidence spans multiple bargaining units",
                        "options": options}, actor="chat", query_id=qid)
            return {"query_id": qid, "mode": "clarify", "prompt_echo": prompt,
                    "question": "That's answered differently by each unit's contract. "
                                "Which one?",
                    "options": options,
                    "considered": [_doc_meta(case, h["doc_id"]) for h in hits]}

    if not hits:
        where = (" or ".join(_titles(case, sc.named)) if sc.named
                 else f"the {dept} documents" if dept
                 else "these documents")
        return {"query_id": qid, "mode": "lookup" if lookup else "policy",
                "answer_source": "none",
                "answer": f"I couldn't find a clause covering that in {where}.",
                "sources": [], "department": dept, "scope_how": sc.how,
                "considered": [_doc_meta(case, d) for d in scope]}

    # 5. Grounded answer + provenance.
    ans = llm.answer_policy(prompt, hits, lookup=lookup)
    if ans.get("source") == "stub":
        # The no-key wording: name the document and page when the ingest found no
        # section label, so a quote never opens with a bare section sign.
        ans["answer"] = rulematch.compose_quote(
            hits, lambda d: _titles(case, [d])[0], lookup=lookup, max_rows=quote_rows)
    led.append("policy.answer", {"answer": ans["answer"], "source": ans["source"],
                                 "lookup": lookup, "scope_how": sc.how,
                                 "pinned": [p["rule_id"] for p in pins],
                                 "coverage": round(cov, 3),
                                 "docs_used": sorted({h["doc_id"] for h in hits[:4]})},
               actor="chat", query_id=qid)
    for h in hits[:4]:
        led.append("citation", {k: h.get(k) for k in ("doc_id", "clause", "page", "bbox")},
                   actor="engine", query_id=qid)
    return {"query_id": qid, "needs_confirmation": False,
            "mode": "lookup" if lookup else "policy",
            "answer": ans["answer"], "answer_source": ans["source"], "department": dept,
            "scope_how": sc.how, "bargaining_units": sc.units, "coverage": round(cov, 3),
            "pinned": pins, "corpus_size": len(ingested),
            "considered": [_doc_meta(case, d) for d in scope],
            "sources": [{**_source_entry(case, cat, h), "pinned_by": h.get("pinned_by")}
                        for h in hits[:4]]}


def _entitlement_answer(case, led, qid: str, prompt: str,
                        department: str | None = None) -> dict:
    """Answer a non-money rule question — "how many bereavement shifts?", "what's the
    grievance deadline?" — with the SAME guarantees as a dollar figure
    (DEMO_TICKETS.md B3).

    An MOU is a rulebook; pay is one chapter. These clauses are equally determinate and
    deserve the deterministic engine + citation, not an LLM paraphrase.

    Who the question is for decides which contract answers it: the named
    classification's bargaining unit (else the named document's), resolved through
    governance to its governing documents. Retrieval runs inside those documents only;
    a ratified non-currency rule is selected when the chunk occupying its cited box is
    among the top hits, or its topic word is in the question. The engine computes the
    typed value. No rule -> quote the subject's own contract (policy), never another
    unit's. Nobody named -> ask who it is for; one department holds two units, so a
    department-wide search would answer a firefighter from the chiefs' contract.
    """
    backend = _backend(case)
    cat = _catalog(case)
    subjects_all = case.subjects()
    params = llm.parse_intent(prompt, _extraction(case), subjects_all)
    if params.get("unverified_numbers"):
        return _clarify(qid, prompt,
                        "I read a number out of that question that it doesn't actually "
                        "state — can you restate it with the amount spelled out?")
    named = set(params.get("subjects") or [])
    named_rows = [s for s in subjects_all if s.get("name") in named]
    units, _rows, how = rulematch.units_for(case, prompt, subjects_all, label=department)
    if named_rows:
        units = sorted({s.get("bargaining_unit") for s in named_rows if s.get("bargaining_unit")})
        how = "classification"
    date_iso = governance.parse_date(params.get("date"), DEFAULT_YEAR) or ""
    led.append("entitlement.scope", {"units": units, "how": how,
                                     "subjects": [s.get("name") for s in named_rows],
                                     "date_iso": date_iso},
               actor="chat", query_id=qid)

    if not units:
        if department and department in case.departments():
            # The visitor already confirmed a department (clarify post-back): quote
            # from that department's documents, never from a wider scope.
            led.append("entitlement.fallback", {"reason": "no subject named"},
                       actor="chat", query_id=qid)
            return _policy_answer(case, led, qid, prompt, department,
                                  doc_scope=_dept_scope(case, cat, department))
        led.append("entitlement.clarify", {"reason": "no subject or document named"},
                   actor="chat", query_id=qid)
        examples = ", ".join(str(s.get("name")) for s in subjects_all[:3])
        return _clarify(qid, prompt,
                        "Who is this for? Name a classification (e.g. "
                        f"{examples}) or the contract (e.g. "
                        f"{', '.join(_titles(case, [s['id'] for s in case.manifest.get('sources', [])[:2]]))}).")

    _ingested = {d["doc_id"] for d in cat.documents()}
    # The classification named the scope; search for the entitlement, not the rank.
    query, stems = rulematch.retrieval_query(prompt, rulematch.rank_stems(named_rows))
    lookup = rulematch.chunk_lookup(backend, cat)
    eng = {"hours": params.get("hours", 0.0), "date": params.get("date", ""),
           "date_iso": date_iso, "holiday_weekday": params.get("holiday_weekday", "")}
    eng = {**queryfacts.query_defaults(case), **eng, **queryfacts.engine_extras(case, params)}  # vacation-accrual: seed every question fact (J1a/J1b)
    line_items: list[dict] = []
    matches: list[dict] = []
    not_covered: list[dict] = []
    all_scope: list[str] = []
    # F1 (KEN-102): the engine's own result over the subjects and rules it actually used,
    # frozen by audit.record_answer so GET /chat/replay recomputes it like a costing.
    raw_items: list[dict] = []
    covered_subjects: list[dict] = []
    rules_used: list = []
    for unit in units:
        gov = governance.resolve([unit], date_iso or None, case.manifest.get("sources", []))
        _titles_by_id = {s.get("id"): s.get("title") or s.get("id")
                         for s in case.manifest.get("sources", [])}
        decisions.record(led, qid, "governance",                      # search-tree (L1)
                         *decisions.governance_lists(gov, case.manifest.get("sources", []),
                                                     _titles_by_id),
                         decisions.FIXED, detail={"unit": unit, "date": date_iso})
        scope = [d for d in gov.doc_ids if d in _ingested]
        all_scope += [d for d in scope if d not in all_scope]
        _raw_hits = backend.search(query or prompt, doc_ids=scope, k=8 + 6) if scope else []
        hits, _below = _raw_hits[:8], _raw_hits[8:]
        led.append("entitlement.retrieval",
                   {"unit": unit, "candidates": len(scope), "candidate_docs": scope,
                    "query": query,
                    "hits": [{k: h.get(k) for k in ("doc_id", "clause", "page", "score")}
                             for h in hits]},
                   actor="chat", query_id=qid)
        decisions.record(led, qid, "clause_retrieval",
                         *decisions.retrieval_lists(hits, _below, 8), decisions.FIXED,
                         counts={"searched": len(_raw_hits), "scope_docs": len(scope)},
                         detail={"query": query or prompt, "scope": scope, "unit": unit})
        candidates = [r for r in case.rules()
                      if r.result_type != "currency" and r.citation.doc_id in scope]
        rules, found, ambiguous = rulematch.select_rules(candidates, hits, stems)
        if ambiguous:
            led.append("entitlement.clarify", {"reason": "several topics match",
                                               "options": ambiguous},
                       actor="chat", query_id=qid)
            return _clarify(qid, prompt, "Which of these is the question about? "
                            + ", ".join(ambiguous) + ".", ambiguous)
        if not rules:
            not_covered.append({"unit": unit, "docs": scope})
            continue
        subjects = [s for s in named_rows if s.get("bargaining_unit") == unit] \
            or [s for s in subjects_all if s.get("bargaining_unit") == unit]
        try:
            result = calculate(eng, subjects, rules, rounding_policy(case.manifest))
        except (ValueError, RuleError, ArithmeticError) as e:   # A4: a rule error is not a 500
            led.append("costing.blocked", {"reason": "rule evaluation error", "unit": unit,
                                           "error": f"{type(e).__name__}: {e}"},
                       actor="engine", query_id=qid)
            led.append("entitlement.fallback", {"reason": f"engine: {e}", "unit": unit},
                       actor="chat", query_id=qid)
            not_covered.append({"unit": unit, "docs": scope})
            continue
        raw = result.to_dict()
        raw_items += copy.deepcopy(raw.get("line_items", []))   # enrichment mutates in place
        used_ids = {li.rule_id for li in result.line_items}
        covered_subjects += [s for s in subjects if s not in covered_subjects]
        rules_used += [r for r in rules if r.id in used_ids
                       and r.id not in {u.id for u in rules_used}]
        rd = _enrich_citations(cat, raw)
        items = rd.get("line_items", [])
        if not any(s.get("bargaining_unit") == unit for s in named_rows) and items \
                and len({(li.get("total"), li.get("rule_id")) for li in items}) == 1:
            # Nobody named: the whole unit gets the same answer, so show one row for
            # the unit rather than six identical ones.
            title = _titles(case, scope[:1])[0] if scope else unit
            items = [dict(items[0], subject=f"Any {title} member")]
        for m in found:
            led.append("entitlement.match", {**m, "unit": unit, "scope_how": how},
                       actor="chat", query_id=qid)
        matches += [{**m, "unit": unit} for m in found]
        line_items += items

    if not line_items:
        led.append("entitlement.fallback",
                   {"reason": "no ratified non-currency rule for the retrieved clauses",
                    "units": units}, actor="chat", query_id=qid)
        return _policy_answer(case, led, qid, prompt, department, doc_scope=all_scope)

    total = line_items[0]["total"] if len(line_items) == 1 else \
        round(sum(float(li.get("total") or 0) for li in line_items), case.rounding_places())
    rd = {"total": total, "line_items": line_items}
    # Same path as the costing branch: schema-2 snapshot bound to the chain. The frozen
    # result is the engine's shape (uncollapsed rows, pre-enrichment) so a replay
    # recomputes it byte-for-byte; the response keeps its own collapsed `rd`.
    engine_result = {"total": costing.sum_lines(raw_items, case.rounding_places()),
                     "line_items": raw_items}
    audit.record_answer(case, led, qid, eng, covered_subjects, rules_used, engine_result,
                        # The Audit tab's history reads result_type from this snapshot
                        # and defaulted it to currency ("3 shifts" showed as "$3.00").
                        extra={"intent": "entitlement",
                               "result_type": line_items[0].get("result_type"),
                               "unit_label": line_items[0].get("result_type")})
    depts = sorted({(case.source_by_id(d) or {}).get("department") or "" for d in all_scope})
    return {"query_id": qid, "needs_confirmation": False, "mode": "entitlement",
            "department": ", ".join(d for d in depts if d) or None,
            "bargaining_units": units, "scope_how": how, "match": matches,
            "not_covered": not_covered, "params": params, "result": rd,
            "corpus_size": len(case.manifest.get("sources", []))}


# --- costing-correctness ----------------------------------------------------- #
def _nearest_clauses(case, cat, prompt: str, doc_ids: list[str], k: int = 2) -> list[dict]:
    """The closest passages in the governing document(s) to a refused costing question
    (B1). Shown as TEXT next to the refusal, never as an answer. An empty scope or a
    missing index yields [] rather than a search over other units' contracts."""
    if not doc_ids:
        return []
    # Search on the question MINUS its roster labels and parentheticals: 'Firefighter/
    # Paramedic (56 hr, top step)' would otherwise outscore the pay clause being asked
    # about. 'pay' and 'rate' anchor the query to the compensation articles.
    labels = [str(s.get("name", "")) for s in case.subjects()]
    query = re.sub(r"\s+", " ", costing.mask_labels(prompt, labels)).strip()
    query = f"{query} pay rate".strip()
    try:
        hits = _backend(case).search(query, doc_ids=doc_ids, k=k)
    except Exception:
        return []
    out = []
    for h in hits[:k]:
        try:
            out.append(_source_entry(case, cat, h))
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
# --- search-tree --- (DEMO_TICKETS.md L1/L2/L3, C1/I13: decision events, the tree,
#     the cited math line)
# --------------------------------------------------------------------------- #
def _input_decision(led, qid: str, backend, outcome: dict, rule, inp: dict,
                    subjects: list[dict], eng_params: dict, rate_refs: dict) -> None:
    """One `decision(input)` event for a declared rule input (L2).

    clause   -> backend.search(query) inside the governing documents, k=5; chosen is the
                DECLARED citation with 'declared by approver X; verified by search rank
                #n of 5' (or a 'not found by search' flag, shown never hidden); the
                other hits are rejected 'rank k, not the declared clause'. human-rule.
    roster   -> one leaf per covered subject with the field's value (+ the schedule
                cell ref when the math line bound it). fixed-logic.
    question -> the parameter the question stated. user.
    """
    name, kind, src = inp["name"], inp["kind"], inp["source"]
    cit = inp.get("citation") or {}
    approver = getattr(rule, "approver", "") or "?"
    detail = {"rule_id": rule.id, "name": name, "kind": kind, "source": src,
              "unit": outcome["bargaining_unit"]}
    if src == "clause":
        hits = backend.search(inp["query"], doc_ids=outcome["doc_ids"], k=5) if inp.get("query") else []
        rank = None
        for i, h in enumerate(hits, 1):
            if h.get("doc_id") == cit.get("doc_id") and h.get("page") == cit.get("page") \
                    and rulematch.bbox_overlap(cit.get("bbox") or [], h.get("bbox") or []) >= rulematch.MIN_OVERLAP:
                rank = i
                break
        verdict = (f"verified by search rank #{rank} of {len(hits)}" if rank
                   else f"not found by search (top {len(hits)})")
        chosen = [{"id": f"{rule.id}:{name}",
                   "label": f"{name}: {cit.get('clause') or 'p.' + str(cit.get('page'))}",
                   "ref": decisions.ref(cit.get("doc_id"), cit.get("page"), cit.get("bbox"),
                                        cit.get("clause"), hits[rank - 1].get("text") if rank else None),
                   "detail": f"declared by approver {approver}; {verdict}"}]
        rejected = [{"id": f"{h.get('doc_id')}:{h.get('page')}:{i}", "label": decisions.hit_label(h),
                     "ref": decisions.hit_ref(h), "value": h.get("score"),
                     "reason": f"rank {i}, not the declared clause"}
                    for i, h in enumerate(hits, 1) if i != rank]
        if rank is None:
            detail["flag"] = "declared citation not found by search"
        decisions.record(led, qid, "input", chosen, rejected, decisions.HUMAN,
                         counts={"searched": len(hits)}, detail={**detail, "query": inp.get("query")})
        return
    if src == "roster":
        field = inp.get("field") or name
        chosen = []
        for s in subjects:
            sname = str(s.get("name"))
            if sname not in outcome["subjects"]:
                continue
            ref = None
            rr = rate_refs.get(sname) if field == "base_hourly" else None
            if rr and not rr.get("unsourced"):
                ref = decisions.ref(rr.get("doc_id"), rr.get("page"), rr.get("context_bbox") or rr.get("bbox"),
                                    f"{rr.get('row')} · {rr.get('column')}", rr.get("quote") or rr.get("text"))
            chosen.append({"id": f"roster:{field}:{sname}",
                           "label": f"{field} = {s.get(field)} — roster row {sname}",
                           "value": s.get(field), "ref": ref,
                           "detail": "from the roster file"
                                     + (", printed on the salary schedule" if ref else "")})
        decisions.record(led, qid, "input", chosen, [], decisions.FIXED,
                         detail={**detail, "pointer": cit or None})
        return
    val = eng_params.get(name)
    decisions.record(led, qid, "input",
                     [{"id": f"question:{name}", "label": f"{name} = {val} — stated in the question",
                       "value": val}], [], decisions.USER, detail=detail)
