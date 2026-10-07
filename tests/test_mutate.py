"""core/mutate.py — deterministic deliberate errors for 'Try to break it' (E3)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import mutate  # noqa: E402
from core.ruledsl import validate_rules  # noqa: E402

CASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "cases", "santacruz")
FF = "firefighters_local3535_mou"
GOVERN = [FF, "admin_group_mou", "management_mou", "chief_officers_mou"]
KNOWN = {"subject_classification", "subject_department", "subject_rank",
         "subject_base_hourly", "subject_shift", "subject_bargaining_unit",
         "hours", "date", "date_iso", "holiday_weekday", "effective_base"}


def _shipped(rid):
    with open(os.path.join(CASE, "rules", "rules_ratified.json")) as f:
        return next(r for r in json.load(f)["rules"] if r["id"] == rid)


def test_shipped_overtime_rule_yields_exactly_seven_mutants_in_a_stable_order():
    rule = _shipped(f"{FF}:overtime_premium_rate")
    ms = mutate.mutants(rule, {}, GOVERN)
    assert [m["label"] for m in ms] == [
        "compute: 1.5 -> 1.6",
        "compute: 1.5 -> 1.4",
        "drop condition: when hours > 0 -> True",
        "invert condition: not (hours > 0)",
        "move threshold: 0 -> 0.1",
        "move threshold: 0 -> -0.1",
        "re-home to another unit's contract: admin_group_mou",
    ]
    assert [m["kind"] for m in ms] == ["number", "number", "drop-condition",
                                       "invert-condition", "threshold", "threshold",
                                       "swap-contract"]
    # the same call twice gives the same list, and the input is untouched
    assert ms == mutate.mutants(rule, {}, GOVERN)
    assert rule == _shipped(f"{FF}:overtime_premium_rate")


def test_every_mutant_parses_and_passes_validation():
    rule = _shipped(f"{FF}:overtime_premium_rate")
    for m in mutate.mutants(rule, {}, GOVERN):
        assert validate_rules([m["rule"]], KNOWN) == {}, m["label"]


def test_constant_rule_yields_two_number_mutants_plus_swap_contract():
    rule = {"id": "x:days", "kind": "selector", "role": "base", "result_type": "days",
            "when": "True", "compute": "3", "citation": {"doc_id": FF, "clause": "XIV"}}
    ms = mutate.mutants(rule, {}, GOVERN)
    assert [m["label"] for m in ms] == ["compute: 3 -> 4", "compute: 3 -> 2",
                                        "re-home to another unit's contract: admin_group_mou"]
    assert ms[0]["rule"]["compute"] == "4" and ms[1]["rule"]["compute"] == "2"
    assert ms[2]["rule"]["citation"]["doc_id"] == "admin_group_mou"


def test_conjunction_drops_each_conjunct_and_swaps_classification():
    rule = {"id": "x:night", "kind": "modifier", "role": "differential",
            "when": "hours > 0 and subject_shift == 'Suppression'",
            "set": {"effective_base": "effective_base * 1.055"},
            "citation": {"doc_id": FF, "clause": "6.1"}}
    ms = mutate.mutants(rule, {"subject_shift": ["Day", "Suppression"]}, [FF])
    labels = [m["label"] for m in ms]
    assert "set effective_base: 1.055 -> 1.155" in labels
    assert "drop condition: hours > 0 (keep subject_shift == 'Suppression')" in labels
    assert "drop condition: subject_shift == 'Suppression' (keep hours > 0)" in labels
    swap = next(m for m in ms if m["kind"] == "swap-classification")
    assert swap["rule"]["when"] == "hours > 0 and subject_shift == 'Day'"
    # only one governing document: nowhere to re-home to
    assert not any(m["kind"] == "swap-contract" for m in ms)


def test_cap_bounds_the_list():
    rule = {"id": "x:many", "kind": "selector", "when": "True",
            "compute": "1 + 2 + 3 + 4 + 5 + 6 + 7 + 8 + 9 + 10 + 11 + 12 + 13 + 14 + 15",
            "citation": {"doc_id": FF, "clause": "1"}}
    assert len(mutate.mutants(rule, {}, GOVERN, cap=5)) == 5
