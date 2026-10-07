"""Question-level facts: the query parameters a rule may reference (DEMO_TICKETS J1a).

A rule may use any scalar key of the case's extraction.yaml `output_shape` (caseio
.known_facts admits them), so the engine must be handed a value for EVERY such key on
every run — a ratified rule that references `years_of_service` must evaluate to "does
not apply" when the question gives no years, never raise RuleError (which chat turned
into HTTP 500). These helpers seed those defaults from the case's own declaration, so
adding a question fact is one YAML line plus a parser, never an engine change.

Why these facts are not roster columns: the roster is classifications, not people
(caseio.subjects). Years of service belong to a person, so they can only come from the
question and are labelled as unverified wherever they are shown.
"""
from __future__ import annotations

from typing import Any

# The four parameters chat has always supplied. Kept here so the seed is one list.
STANDARD_PARAMS: dict[str, Any] = {"hours": 0.0, "date": "", "date_iso": "",
                                   "holiday_weekday": ""}

# Numeric output_shape types in extraction.yaml (anything else is text/list).
_NUMERIC = {"float", "int", "number"}


def numeric_question_facts(case) -> list[str]:
    """Scalar numeric keys declared in extraction.yaml output_shape, in file order,
    minus the standard params (so `hours` is never listed twice)."""
    shape = _output_shape(case)
    return [k for k, t in shape.items()
            if isinstance(t, str) and t.lower() in _NUMERIC and k not in STANDARD_PARAMS]


def query_defaults(case) -> dict[str, Any]:
    """Every query param a rule may reference, at its safe default. Seed engine params
    with this so no rule can hit an undefined name."""
    out = dict(STANDARD_PARAMS)
    for k in numeric_question_facts(case):
        out[k] = 0.0
    return out


def engine_extras(case, params: dict) -> dict[str, float]:
    """The parsed values of the extra numeric question facts, coerced to float, with
    0.0 for anything missing or unparsable. Merge into eng_params after the standard
    four; never trust a non-numeric value from a parser."""
    out: dict[str, float] = {}
    for k in numeric_question_facts(case):
        try:
            out[k] = float(params.get(k) or 0.0)
        except (TypeError, ValueError):
            out[k] = 0.0
    return out


def _output_shape(case) -> dict:
    import os
    try:
        import yaml
        path = case.path("extraction")
        if not path or not os.path.exists(path):
            return {}
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        return dict(cfg.get("output_shape") or {})
    except Exception:
        return {}
