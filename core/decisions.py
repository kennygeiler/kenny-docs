"""Decision events: the losers at every fork, recorded in the hash chain (DEMO_TICKETS L1).

Every other ledger event names only the winner — governance.resolve lists the document
that matched and silently skips the four that did not; rule.selector_considered sees
only the rules that survived an unrecorded filter. An auditor asking "why not the Admin
Group contract?" or "why did the longevity rule not fire?" had nothing to read.

One `decision` event per fork fixes that. The payload is the same shape everywhere:

    {fork, decided_by, chosen: [{id, label, ref?, value?, detail?}],
     rejected: [{id, label, reason, ref?}], counts: {considered, chosen}, detail?}

`decided_by` is the honest answer to "who made this call":
    fixed-logic — deterministic code (regexes, governance tables, the engine)
    ai          — the model's structured output was taken (read from llm._TRAIL)
    human-rule  — a human-ratified rule decided (the engine only evaluated it)
    user        — the visitor answered a clarifying question

`ref` is a clickable address: {doc_id, page, bbox, clause, text[:120]}. The helpers here
only BUILD payloads and APPEND events; nothing in this module changes an answer, and no
model is called. Answers are unchanged with or without these events.
"""
from __future__ import annotations

import re
from typing import Any

FIXED = "fixed-logic"
AI = "ai"
HUMAN = "human-rule"
USER = "user"
EVENT = "decision"

# Rejected lists are capped so a 200-row roster does not write a 200-row event; the
# counts strip says how many were considered.
REJECTED_CAP = 25


def ref(doc_id: str | None, page: Any = None, bbox: list | None = None,
        clause: str | None = None, text: str | None = None) -> dict:
    """A clickable address. Text is trimmed to 120 chars — the ref opens the page."""
    out: dict = {"doc_id": doc_id or "", "page": page}
    if bbox:
        out["bbox"] = list(bbox)
    if clause:
        out["clause"] = str(clause)
    if text:
        out["text"] = str(text)[:120]
    return out


def record(led, qid: str, fork: str, chosen: list[dict], rejected: list[dict],
           decided_by: str = FIXED, counts: dict | None = None,
           detail: Any = None, actor: str = "chat") -> dict:
    """Append one decision event and return its payload."""
    if decided_by not in (FIXED, AI, HUMAN, USER):
        decided_by = FIXED
    rej = list(rejected or [])
    payload: dict = {
        "fork": fork,
        "decided_by": decided_by,
        "chosen": [dict(c) for c in (chosen or [])],
        "rejected": [dict(r) for r in rej[:REJECTED_CAP]],
        "counts": {"considered": len(chosen or []) + len(rej), "chosen": len(chosen or []),
                   **(counts or {})},
    }
    if len(rej) > REJECTED_CAP:
        payload["counts"]["rejected_omitted"] = len(rej) - REJECTED_CAP
    if detail is not None:
        payload["detail"] = detail
    led.append(EVENT, payload, actor=actor, query_id=qid)
    return payload


def decided_by_for(fn: str) -> str:
    """Who decided the touchpoint `fn` on THIS request: 'ai' when the last recorded call
    for that function in the per-request trail (core/llm.py) came from the model,
    'fixed-logic' otherwise (no key, model failed, or the function never calls out)."""
    try:
        from . import llm
        trail = llm._TRAIL.get()
    except Exception:
        trail = None
    if not trail:
        return FIXED
    for entry in reversed(trail):
        if entry.get("fn") == fn:
            return AI if entry.get("source") == "claude" else FIXED
    return FIXED


# --------------------------------------------------------------------------- #
# intent: why the other three kinds of question were not chosen
# --------------------------------------------------------------------------- #
INTENTS = ("costing", "lookup", "entitlement", "policy")
INTENT_LABELS = {"costing": "a cost to compute", "lookup": "a published figure",
                 "entitlement": "a non-money entitlement", "policy": "what the contract says"}


def intent_explained(prompt: str, chosen: str, decided_by: str) -> tuple[list[dict], list[dict]]:
    """(chosen, rejected) for the intent fork. With the model deciding, the rejected
    reason is simply 'not chosen by the model'; with the keyword router it names the
    cue test each other kind failed, in the router's own order (core/llm.py)."""
    from . import llm
    p = (prompt or "").lower()
    ent = bool(llm._ENTITLEMENT_RE.search(p))
    look = bool(llm._LOOKUP_RE.search(p))
    comp = bool(llm._COMPUTE_RE.search(p))
    pol = any(c in p for c in llm._POLICY_STRONG)
    cost = any(c in p for c in llm._COST_CUES)
    rejected: list[dict] = []
    for it in INTENTS:
        if it == chosen:
            continue
        if decided_by == AI:
            reason = "not chosen by the model"
        elif it == "entitlement":
            reason = "no 'how many days/hours/shifts', deadline or accrual cue"
        elif it == "lookup":
            reason = ("rate/schedule cue present but a compute cue wins" if look and comp
                      else "no rate or salary-schedule cue")
        elif it == "costing":
            reason = ("entitlement cue outranks the compute cue" if ent and (comp or cost)
                      else "no compute cue (cost, total, calculate, N-hour)")
        else:  # policy
            reason = ("policy phrasing present but the compute verb governs" if pol and (comp or cost)
                      else "not the default: a stronger cue matched")
        rejected.append({"id": it, "label": INTENT_LABELS[it], "reason": reason})
    return [{"id": chosen, "label": INTENT_LABELS.get(chosen, chosen)}], rejected


# --------------------------------------------------------------------------- #
# subject: the roster rows the question did not name
# --------------------------------------------------------------------------- #
_SUBJECT_FIELDS = ("department", "rank", "shift", "bargaining_unit")


def subject_explained(prompt: str, subjects_all: list[dict], chosen_names: list[str],
                      how: str = "named") -> tuple[list[dict], list[dict]]:
    """Chosen roster rows and, for every other row, which of its attributes the
    question did not mention (mirrors llm._resolve_classifications' attribute filter)."""
    pl = (prompt or "").lower()
    chosen = [{"id": f"roster:{n}", "label": n, "detail": how} for n in chosen_names]
    rejected: list[dict] = []
    for s in subjects_all:
        name = str(s.get("name") or "")
        if name in chosen_names:
            continue
        missing = []
        for f in _SUBJECT_FIELDS:
            v = str(s.get(f) or "")
            if v and not re.search(rf"\b{re.escape(v.lower())}s?\b", pl):
                missing.append(f"{f}={v}")
        reason = "label not mentioned" + (f"; {', '.join(missing)} not mentioned" if missing else "")
        rejected.append({"id": f"roster:{name}", "label": name, "reason": reason})
    return chosen, rejected


# --------------------------------------------------------------------------- #
# governance: the contracts that do not govern this unit on this date
# --------------------------------------------------------------------------- #
def governance_lists(gov, sources: list[dict], titles: dict[str, str] | None = None
                     ) -> tuple[list[dict], list[dict]]:
    """Chosen/rejected from a GovResult (core/governance.py records `rejected` with the
    exact test each source failed: doc_type, unit, or the date window)."""
    titles = titles or {}
    chosen = [{"id": m["doc_id"], "label": titles.get(m["doc_id"], m["doc_id"]),
               "detail": m.get("why", "")} for m in (gov.matched or [])]
    rejected = [{"id": r["doc_id"], "label": titles.get(r["doc_id"], r["doc_id"]),
                 "reason": r.get("reason", "")} for r in (getattr(gov, "rejected", None) or [])]
    return chosen, rejected


# --------------------------------------------------------------------------- #
# rule_filter: why a live rule never reached the engine
# --------------------------------------------------------------------------- #
def rule_ref(rule) -> dict:
    c = getattr(rule, "citation", None)
    if c is None:
        return {}
    return ref(c.doc_id, c.page, c.bbox, c.clause, getattr(rule, "human_readable", ""))


def rule_filter_lists(all_rules: list, survivors: list, gov_doc_ids: list[str],
                      asked_types: list[str], dropped: list[dict] | None = None,
                      titles: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """Every live rule either survived to the engine or has a reason it did not."""
    titles = titles or {}
    alive = {r.id for r in survivors}
    superseded = {d.get("rule_id"): d for d in (dropped or [])}
    chosen = [{"id": r.id, "label": r.human_readable[:80] or r.id, "ref": rule_ref(r),
               "detail": f"topic {r.topic}, {r.result_type}, {r.role}"} for r in survivors]
    rejected: list[dict] = []
    for r in all_rules:
        if r.id in alive:
            continue
        doc = r.citation.doc_id
        if doc not in gov_doc_ids:
            reason = f"cites {titles.get(doc, doc)}, not a governing document"
        elif r.result_type != "currency":
            reason = f"result_type {r.result_type}, costing wants currency"
        elif r.id in superseded:
            reason = f"superseded by {superseded[r.id].get('superseded_by')}"
        elif asked_types and r.role == "base" and r.topic not in asked_types:
            reason = f"topic {r.topic}, question asked for {'/'.join(asked_types)}"
        else:
            reason = "filtered before the engine"
        rejected.append({"id": r.id, "label": r.human_readable[:80] or r.id,
                         "reason": reason, "ref": rule_ref(r)})
    return chosen, rejected


# --------------------------------------------------------------------------- #
# rule_select: per subject, the rule that fired and why each other one did not
# --------------------------------------------------------------------------- #
def rule_select_lists(line_item, rules: list, approved_at: dict[str, str] | None = None
                      ) -> tuple[list[dict], list[dict]]:
    """From one engine LineItem: the chosen base rule (plus every differential/premium
    that fired) and, for each rule the engine considered but did not apply, the test
    that failed — straight from the selector-considered trace and the rule's `when`."""
    approved_at = approved_at or {}
    by_id = {r.id: r for r in rules}
    fired: list[str] = []
    considered_false: dict[str, str] = {}
    for step in line_item.trace:
        if step.kind in ("selector-chosen", "modifier", "premium") and step.rule_id not in fired:
            fired.append(step.rule_id)
        if step.kind == "selector-considered" and step.value is False:
            considered_false[step.rule_id] = step.detail
    chosen: list[dict] = []
    for rid in fired:
        r = by_id.get(rid)
        if r is None:
            continue
        chosen.append({"id": rid, "label": r.human_readable[:80] or rid, "ref": rule_ref(r),
                       "detail": "approved by " + (r.approver or "?")
                                 + (f" on {approved_at[rid][:10]}" if approved_at.get(rid) else "")})
    rejected: list[dict] = []
    for r in rules:
        if r.id in fired:
            continue
        if r.id in considered_false:
            reason = f"when '{r.when}' -> False"
        elif r.role == "differential":
            reason = f"when '{r.when}' -> False"
        elif r.role == "base":
            reason = "matched, lower precedence than the chosen rule"
        else:
            reason = f"when '{r.when}' -> False"
        rejected.append({"id": r.id, "label": r.human_readable[:80] or r.id,
                         "reason": reason, "ref": rule_ref(r)})
    return chosen, rejected


# --------------------------------------------------------------------------- #
# clause_retrieval: the hits used and the ones below the cutoff
# --------------------------------------------------------------------------- #
def hit_ref(h: dict) -> dict:
    return ref(h.get("doc_id"), h.get("page"), h.get("bbox"), h.get("clause"), h.get("text"))


def hit_label(h: dict) -> str:
    t = (h.get("text") or "").strip().replace("\n", " ")
    return f"p.{h.get('page')} · {t[:70]}{'…' if len(t) > 70 else ''}"


def retrieval_lists(used: list[dict], below_cutoff: list[dict], cutoff: int,
                    floor_reason: str | None = None) -> tuple[list[dict], list[dict]]:
    chosen = [{"id": f"{h.get('doc_id')}:{h.get('page')}:{i}", "label": hit_label(h),
               "ref": hit_ref(h), "value": h.get("score"),
               "detail": f"rank {i}" + (f", pinned by {h['pinned_by']}" if h.get("pinned_by") else "")}
              for i, h in enumerate(used, 1)]
    rejected = [{"id": f"{h.get('doc_id')}:{h.get('page')}:{cutoff + i}", "label": hit_label(h),
                 "ref": hit_ref(h), "value": h.get("score"),
                 "reason": f"rank {cutoff + i} > cutoff {cutoff}"}
                for i, h in enumerate(below_cutoff, 1)]
    if floor_reason:
        for c in chosen:
            c["detail"] = (c.get("detail", "") + "; " + floor_reason).strip("; ")
    return chosen, rejected
