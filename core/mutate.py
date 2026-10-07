"""Deliberate-error generator for the 'Try to break it' check (DEMO_TICKETS.md E3).

Pure and I/O-free. Given one rule dict, produce a short, deterministic list of
plausible wrong versions of it — the kinds of slip a drafter (human or model) actually
makes: a multiplier off by a step, a guard dropped or inverted, a threshold nudged, a
classification compared against the wrong label, a clause cited from another unit's
contract. The app runs every known answer against each mutant; a mutant no known
answer catches tells the reviewer exactly which part of the rule is untested.

No model call, no randomness, nothing written: the same rule always yields the same
mutants in the same order, so the result table is reproducible and testable.
"""
from __future__ import annotations

import ast
import copy
from typing import Any


def _step(v: float) -> float:
    """One step for a numeric literal: 1 when integer-valued and >= 2, else 0.1."""
    return 1 if float(v).is_integer() and abs(v) >= 2 else 0.1


def _numeric_consts(tree: ast.AST) -> list[ast.Constant]:
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)]


def number_mutants(expr: str) -> list[tuple[str, str]]:
    """[(label, new_expr)] for each numeric literal in `expr`, plus and minus one step.
    Returns [] when the expression does not parse (validation reports that separately)."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return []
    out: list[tuple[str, str]] = []
    for i, n in enumerate(_numeric_consts(tree)):
        v = n.value
        step = _step(v)
        for delta in (+step, -step):
            t2 = ast.parse(expr, mode="eval")
            c2 = _numeric_consts(t2)[i]
            nv = round(v + delta, 6)
            c2.value = int(nv) if (isinstance(v, int) and float(nv).is_integer()) else nv
            out.append((f"{v} -> {c2.value}", ast.unparse(t2)))
    return out


def _conjuncts(expr: str) -> list[str]:
    """The top-level `and` conjuncts of a condition, as source strings ([] if not an and)."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return []
    body = tree.body
    if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And):
        return [ast.unparse(v) for v in body.values]
    return []


def _subject_string_compares(expr: str) -> list[tuple[str, str]]:
    """[(fact, literal)] for each `subject_x == 'Literal'` (either side) in `expr`."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return []
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left] + list(node.comparators)
        names = [s.id for s in sides if isinstance(s, ast.Name) and s.id.startswith("subject_")]
        lits = [s.value for s in sides if isinstance(s, ast.Constant) and isinstance(s.value, str)]
        for nm in names:
            for lit in lits:
                found.append((nm, lit))
    return found


def _swap_literal(expr: str, old: str, new: str) -> str:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == old:
            node.value = new
            break
    return ast.unparse(tree)


def mutants(rule: dict, field_values: dict[str, list[str]] | None,
            other_docs: list[str] | None, cap: int = 24) -> list[dict[str, Any]]:
    """Deterministic list of {label, kind, rule} for one rule dict.

    Order (stable, so a result table reads the same every time):
      number            each numeric literal in `compute`, then in each `set` expression,
                        plus and minus one step
      drop-condition    `when` -> 'True'; for `a and b`, each conjunct dropped
      invert-condition  `not (<when>)`
      threshold         each numeric literal in `when`, plus and minus one step
      swap-classification  each string literal compared with a `subject_*` fact,
                        replaced by the next value in `field_values[fact]`
      swap-contract     citation.doc_id -> the next governing document after the rule's
                        own in `other_docs` (cyclic); skipped when there is none

    `cap` bounds the list for a rule with many literals so the check stays sub-second.
    The rule dict itself is never modified.
    """
    out: list[dict[str, Any]] = []
    field_values = field_values or {}
    other_docs = list(other_docs or [])

    def add(label: str, kind: str, patch: dict) -> None:
        m = copy.deepcopy(rule)
        m.update(patch)
        out.append({"label": label, "kind": kind, "rule": m})

    # number: compute, then set expressions
    if rule.get("compute"):
        for lab, e in number_mutants(str(rule["compute"])):
            add(f"compute: {lab}", "number", {"compute": e})
    for k, v in (rule.get("set") or {}).items():
        for lab, e in number_mutants(str(v)):
            add(f"set {k}: {lab}", "number", {"set": {**rule["set"], k: e}})

    when = str(rule.get("when", "True") or "True")
    if when.strip() != "True":
        # drop-condition
        add(f"drop condition: when {when} -> True", "drop-condition", {"when": "True"})
        parts = _conjuncts(when)
        if len(parts) > 1:
            for i, p in enumerate(parts):
                rest = " and ".join(parts[:i] + parts[i + 1:])
                add(f"drop condition: {p} (keep {rest})", "drop-condition", {"when": rest})
        # invert-condition
        add(f"invert condition: not ({when})", "invert-condition", {"when": f"not ({when})"})
        # threshold
        for lab, e in number_mutants(when):
            add(f"move threshold: {lab}", "threshold", {"when": e})
        # swap-classification
        for fact, lit in _subject_string_compares(when):
            vals = list(field_values.get(fact) or [])
            if lit in vals and len(vals) > 1:
                nxt = vals[(vals.index(lit) + 1) % len(vals)]
            elif vals and lit not in vals:
                nxt = vals[0]
            else:
                continue
            add(f"swap classification: {fact} {lit!r} -> {nxt!r}", "swap-classification",
                {"when": _swap_literal(when, lit, nxt)})

    # swap-contract
    own = (rule.get("citation") or {}).get("doc_id") or ""
    candidates = [d for d in other_docs if d and d != own]
    if candidates:
        if own in other_docs:
            i = other_docs.index(own)
            ring = other_docs[i + 1:] + other_docs[:i]
            nxt = next(d for d in ring if d != own)
        else:
            nxt = candidates[0]
        cit = dict(rule.get("citation") or {})
        cit["doc_id"] = nxt
        add(f"re-home to another unit's contract: {nxt}", "swap-contract", {"citation": cit})

    return out[:cap]
