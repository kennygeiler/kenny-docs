"""Costing correctness helpers (DEMO_TICKETS B1, B2, B4).

Three deterministic pieces that sit between the parsed question and the engine:

  detect_pay_type  — WHICH pay branch the question asks for (overtime, regular, holiday,
                     callback, ...). Lexicon-driven, whole-word, with negation. The model
                     is never consulted: a wrong branch is a wrong multiplier.
  extract_hours    — the hour candidates the question actually states, with roster labels
                     masked so the '56' in 'Firefighter/Paramedic (56 hr, top step)' can
                     never become the shift length.
  cost_by_unit     — per-subject governance: each bargaining unit resolves to ITS contract
                     and ITS ratified rules; an uncovered unit gets a reason, never another
                     unit's rule.

Everything here is pure data in, dicts out; the ledger writes are done through the
`led` handle the caller passes so the trail stays in one chain.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from . import governance
from .engine import NoRuleApplies, calculate
from .ruledsl import SHIFT_BASES, Rule

# Fallback lexicon when the case's extraction.yaml declares none. The case file wins:
# vocabulary is case data, not code (PRD §4).
DEFAULT_PAY_TYPES: dict[str, list[str]] = {
    "overtime": ["overtime", "over time", "time and a half", "1.5x", "ot"],
    "regular": ["regular", "straight time", "normal shift", "scheduled shift"],
    "holiday": ["holiday"],
    "callback": ["callback", "call back"],
    "standby": ["standby", "on call"],
    "out_of_class": ["out of class", "acting", "upgrade"],
}
DEFAULT_HOURS_MAX = 96.0

_NEGATORS = r"(?:no|not|non|without)"


def pay_type_lexicon(extraction_cfg: dict | None) -> dict[str, list[str]]:
    lex = (extraction_cfg or {}).get("pay_types")
    if isinstance(lex, dict) and lex:
        return {str(k): [str(c) for c in (v or [])] for k, v in lex.items()}
    return dict(DEFAULT_PAY_TYPES)


def hours_limit(extraction_cfg: dict | None) -> float:
    try:
        return float((((extraction_cfg or {}).get("limits") or {}).get("hours") or {})
                     .get("max") or DEFAULT_HOURS_MAX)
    except (TypeError, ValueError):
        return DEFAULT_HOURS_MAX


def _norm(text: str) -> str:
    # Hyphens read as spaces so 'over-time', 'straight-time', 'call-back' hit the
    # two-word cues; everything else is left for the whole-word regexes.
    return re.sub(r"[\-_]+", " ", (text or "").lower())


def _cue_re(cue: str) -> re.Pattern:
    words = [re.escape(w) for w in _norm(cue).split()]
    return re.compile(r"(?<![\w.])" + r"\s+".join(words) + r"(?![\w])")


def detect_pay_type(prompt: str, lexicon: dict[str, list[str]] | None = None) -> dict:
    """-> {asked: [types in prompt order], negated: [types], cues: {type: cue}}.

    Whole-word, case-insensitive. 'no|not|non|without <cue>' removes that type; if the
    negation leaves nothing asked, the question is read as 'regular' (a shift with no
    premium named is a regular shift). 'holiDAY' can never match 'day' and 'OT' is a
    word of its own, so 'quOTe' is not overtime.
    """
    lex = lexicon or DEFAULT_PAY_TYPES
    text = _norm(prompt)
    first_pos: dict[str, int] = {}
    cue_hit: dict[str, str] = {}
    negated: list[str] = []
    for ptype, cues in lex.items():
        for cue in cues:
            if not cue:
                continue
            pat = _cue_re(cue)
            positive = None
            for m in pat.finditer(text):
                # A cue preceded by a negator ('no overtime', 'without holiday pay',
                # 'non-overtime') is a denial of that branch, not a request for it.
                before = text[:m.start()]
                if re.search(r"\b" + _NEGATORS + r"(?:\s+\w+)?\s*$", before):
                    if ptype not in negated:
                        negated.append(ptype)
                    continue
                if positive is None or m.start() < positive:
                    positive = m.start()
            if positive is not None and (ptype not in first_pos or positive < first_pos[ptype]):
                first_pos[ptype] = positive
                cue_hit[ptype] = cue
    asked = [t for t, _ in sorted(first_pos.items(), key=lambda kv: kv[1]) if t not in negated]
    if not asked and negated and "regular" in lex:
        asked = ["regular"]
        cue_hit["regular"] = "no " + "/".join(negated)
    return {"asked": asked, "negated": negated, "cues": {t: cue_hit[t] for t in asked}}


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty one": 21, "twenty two": 22,
    "twenty three": 23, "twenty four": 24,
}
_HOUR_UNIT = r"(?:hours?|hrs?|h)\b"
_HOURS_RE = re.compile(r"(?<![\w.,\-])(\d+(?:\.\d+)?)\s*-?\s*" + _HOUR_UNIT, re.I)
_INVALID_HOURS_RE = re.compile(r"(?<=[,\-])(\d+(?:\.\d+)?)\s*-?\s*" + _HOUR_UNIT, re.I)
_WORD_HOURS_RE = re.compile(
    r"\b(" + "|".join(sorted((k.replace(" ", r"[\s\-]") for k in _NUMBER_WORDS),
                             key=len, reverse=True)) + r")[\s\-]+" + _HOUR_UNIT, re.I)


def mask_labels(prompt: str, labels: list[str]) -> str:
    """Blank every roster label and every parenthetical descriptor, keeping the string
    length so positions still line up with the original prompt."""
    text = prompt
    for lab in sorted((l for l in labels if l), key=len, reverse=True):
        idx = text.lower().find(lab.lower())
        while idx >= 0:
            text = text[:idx] + " " * len(lab) + text[idx + len(lab):]
            idx = text.lower().find(lab.lower())
    return re.sub(r"\([^)]*\)", lambda m: " " * len(m.group(0)), text)


def extract_hours(prompt: str, labels: list[str] | None = None) -> dict:
    """-> {candidates: [float, in prompt order], invalid: [str]}.

    Roster labels and parentheticals are masked first, so '(56 hr, top step)' is never
    a candidate. A number glued to '-' or ',' ('-8-hour', '2,5 hours') is reported as
    invalid rather than guessed at.
    """
    text = mask_labels(prompt or "", labels or [])
    found: list[tuple[int, float]] = []
    for m in _HOURS_RE.finditer(text):
        found.append((m.start(), float(m.group(1))))
    for m in _WORD_HOURS_RE.finditer(text):
        key = re.sub(r"[\s\-]+", " ", m.group(1).lower())
        found.append((m.start(), float(_NUMBER_WORDS[key])))
    found.sort()
    candidates: list[float] = []
    for _, h in found:
        if h not in candidates:
            candidates.append(h)
    invalid = [m.group(0).strip() for m in _INVALID_HOURS_RE.finditer(text)]
    # '2,5 hours': the leading '2' is not a candidate either — the whole token is bad.
    return {"candidates": candidates, "invalid": invalid}


# --------------------------------------------------------------------------- #
# Per-unit governance + costing (B2 + B1)
# --------------------------------------------------------------------------- #
def _title(case, doc_id: str) -> str:
    return (case.source_by_id(doc_id) or {}).get("title") or doc_id


def _no_contract_reason(case, unit: str, date_iso: str | None, sources: list[dict]) -> str:
    reason = f"No contract on file covers '{unit}'"
    if date_iso:
        for s in sources:
            if s.get("bargaining_unit") == unit and s.get("doc_type") in ("MOU", "amendment"):
                reason += (f" on {date_iso}; {s.get('title') or s.get('id')} runs "
                           f"{s.get('effective_start') or '?'} to {s.get('effective_end') or '?'}")
                break
    return reason


def base_topics(rules: list[Rule]) -> list[str]:
    """Topics of the currency base rules in a rule set, in a stable order."""
    seen: list[str] = []
    for r in rules:
        if r.role == "base" and r.result_type == "currency" and r.pay_basis in SHIFT_BASES:
            t = r.topic or "(untyped)"
            if t not in seen:
                seen.append(t)
    return seen


def cost_by_unit(case, cat, led, qid: str, subjects: list[dict], eng_params: dict,
                 date_iso: str | None, asked_types: list[str],
                 doc_integrity: Callable[[Any, Any, list[str], list], list[str]],
                 raw_ratified: Callable[[Any], list[dict]]) -> list[dict]:
    """Group subjects by bargaining unit and cost each group under ITS OWN contract.

    Returns one outcome dict per unit, in order of first appearance:
      {bargaining_unit, subjects, status, reason, doc_ids, rules_used, line_items,
       approved_topics, stale}
    status: ok | no_contract | no_rules | stale | integrity | no_rule_for_pay_type |
            no_rule_for_scenario
    A doc is eligible ONLY if governance returns it for that unit and date; there is no
    retrieval fallback and no caller-supplied document on this path.
    """
    sources = case.manifest.get("sources", [])
    all_rules = case.rules()
    groups: dict[str, list[dict]] = {}
    for s in subjects:
        groups.setdefault(str(s.get("bargaining_unit") or ""), []).append(s)

    outcomes: list[dict] = []
    for unit, members in groups.items():
        names = [str(s.get("name")) for s in members]
        out: dict = {"bargaining_unit": unit, "subjects": names, "status": "ok",
                     "reason": "", "doc_ids": [], "rules_used": [], "line_items": [],
                     "approved_topics": [], "approved_rules": [], "stale": [],
                     "trace_items": []}
        outcomes.append(out)
        if not unit:
            out["status"] = "no_contract"
            out["reason"] = ("No contract on file covers "
                             + ", ".join(f"'{n}'" for n in names)
                             + " (no bargaining unit on the roster)")
            continue
        gov = governance.resolve([unit], date_iso, sources)
        led.append("governance.resolve",
                   {"units": gov.units, "bargaining_unit": unit, "date": gov.date,
                    "resolved": gov.resolved, "matched": gov.matched, "reason": gov.reason,
                    "subjects": names},
                   actor="chat", query_id=qid)
        if not gov.resolved:
            out["status"] = "no_contract"
            out["reason"] = _no_contract_reason(case, unit, date_iso, sources)
            continue
        out["doc_ids"] = list(gov.doc_ids)
        # The unit's own documents only. A rule with no doc_id used to match everyone —
        # that wildcard is exactly how one unit's clause reached another unit's row.
        rules = [r for r in all_rules
                 if r.citation.doc_id and r.citation.doc_id in gov.doc_ids
                 and r.result_type == "currency"]
        rules, dropped = governance.apply_supersession(rules, sources, gov.doc_ids)
        if dropped:
            led.append("governance.supersession", {"dropped": dropped, "bargaining_unit": unit},
                       actor="engine", query_id=qid)
        titles = ", ".join(_title(case, d) for d in gov.doc_ids)
        if not rules:
            stale = [r.get("id") for r in raw_ratified(case)
                     if r.get("status") == "stale"
                     and (r.get("citation") or {}).get("doc_id") in gov.doc_ids]
            out["stale"] = stale
            if stale:
                out["status"] = "stale"
                out["reason"] = (f"Not covered — the rules for {titles} are pending "
                                 f"re-verification ({len(stale)} rule(s) marked stale)")
            else:
                out["status"] = "no_rules"
                out["reason"] = f"Not covered — {titles} has no approved rule"
            continue
        problems = doc_integrity(case, cat, gov.doc_ids, rules)
        if problems:
            out["status"] = "integrity"
            out["reason"] = ("Not covered — the source documents no longer match what "
                             "the rules were ratified against: " + "; ".join(problems))
            continue
        approved = base_topics(rules)
        out["approved_topics"] = approved
        out["approved_rules"] = [
            {"rule_id": r.id, "topic": r.topic, "citation": r.citation.to_dict(),
             "human_readable": r.human_readable}
            for r in rules if r.role == "base" and r.pay_basis in SHIFT_BASES]
        # B1: a base rule competes only if the question asked for its pay branch. If any
        # asked branch has no ratified base rule here, this unit cannot be costed — the
        # overtime formula must not price a regular or holiday shift.
        if asked_types:
            missing = [t for t in asked_types if t not in approved]
            if missing:
                out["status"] = "no_rule_for_pay_type"
                out["reason"] = (f"No approved {'/'.join(missing)} rule for {titles}; "
                                 f"approved for this unit: {', '.join(approved) or 'none'}")
                continue
            rules = [r for r in rules
                     if r.role != "base" or r.topic in asked_types]
        try:
            # A shift-cost question includes hourly and per-shift pay only — never a
            # year of benefits (see engine.calculate basis_scope).
            res = calculate(eng_params, members, rules, case.rounding_places(),
                            basis_scope=SHIFT_BASES)
        except (NoRuleApplies, ValueError) as e:
            out["status"] = "no_rule_for_scenario"
            out["reason"] = f"The approved rules for {titles} don't cover this scenario ({e})"
            continue
        used_ids = {li.rule_id for li in res.line_items}
        for li in res.line_items:
            for step in li.trace:
                used_ids.add(step.rule_id)
        out["rules_used"] = [r for r in rules if r.id in used_ids]
        rd = res.to_dict()
        for li in rd["line_items"]:
            li["bargaining_unit"] = unit
            li["governing_docs"] = list(gov.doc_ids)
        out["line_items"] = rd["line_items"]
        out["trace_items"] = res.line_items
    return outcomes


def sum_lines(line_items: list[dict], places: int = 2) -> float:
    """Total of the covered lines only — half-up, like the engine."""
    from decimal import ROUND_HALF_UP, Decimal
    q = Decimal(10) ** -places
    total = sum((Decimal(str(li.get("total", 0))) for li in line_items), Decimal(0))
    return float(total.quantize(q, rounding=ROUND_HALF_UP))


def interpretation(hours: float | None, pay_type: str | None, subjects: list[str],
                   date_iso: str | None, year_stated: bool, parsed_via: str,
                   checks: list[dict] | None = None) -> dict:
    """The 'Read as:' payload (B4): what the engine was actually handed."""
    if date_iso:
        date_note = date_iso if year_stated else f"{date_iso} (year assumed)"
    else:
        date_note = "no date given"
    h = f"{hours:g} h" if hours else "? h"
    if not subjects:
        who = "no classification"
    elif len(subjects) <= 3:
        who = ", ".join(subjects)
    else:
        who = f"{len(subjects)} classifications"
    read_as = " · ".join([h, pay_type or "pay type not stated", who, date_note])
    return {"hours": hours, "pay_type": pay_type, "subjects": list(subjects),
            "date_iso": date_iso, "date_note": date_note, "parsed_via": parsed_via,
            "checks": checks or [], "read_as": read_as}
