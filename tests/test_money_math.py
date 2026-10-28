"""Money math: exact decimal evaluation, one declared rounding policy, and a flag's
alternate that is comparable to its primary (audit: float-then-round, flag pair)."""
import os

import pytest
import yaml

from core.engine import RoundingPolicy, calculate, rounding_policy
from core.ruledsl import Rule, eval_expr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _r(**kw):
    kw.setdefault("status", "ratified")
    kw.setdefault("approver", "t")
    kw.setdefault("citation", {"doc_id": "d", "clause": "1"})
    return Rule.from_dict(kw)


def _ot(rate, hours, **extra):
    rule = _r(id="ot", kind="selector", role="base", when="True",
              compute="subject_base_hourly * 1.5 * hours", **extra)
    return calculate({"hours": hours}, [{"name": "A", "base_hourly": rate}], [rule])


# --- exact decimal evaluation ---------------------------------------------------- #
def test_extension_floats_round_down_is_now_exact():
    """1.07 x 1.5 x 3 = 4.815 exactly. In float it is 4.8149999999999995, which the old
    float-then-Decimal(str()) path rounded to 4.81. Exact arithmetic gives 4.82."""
    assert 1.07 * 1.5 * 3 != 4.815                     # the float bug being guarded
    assert _ot(1.07, 3).total == 4.82


def test_point_one_plus_point_two_compares_equal_in_a_when():
    """A condition on money must not depend on float noise: 0.1 + 0.2 == 0.3."""
    assert 0.1 + 0.2 != 0.3
    assert eval_expr("a + b == 0.3", {"a": 0.1, "b": 0.2}) is True


def test_int_division_is_exact_not_float():
    v = eval_expr("300 * 12 / 2920", {})
    assert not isinstance(v, float)
    assert str(v).startswith("1.232876712328767123287671")


def test_ints_and_strings_pass_through():
    assert eval_expr("x * 2 + 1", {"x": 10}) == 21
    assert type(eval_expr("x * 2 + 1", {"x": 10})) is int
    assert eval_expr("'Sat' in ('Sat', 'Sun')", {}) is True
    assert eval_expr("min(a, 3) + max(1, 2)", {"a": 2.5}) == eval_expr("4.5", {})


def test_premiums_rounded_once_each_and_summed_exactly():
    """Three premiums of 1/3 each round to 0.33; the line is 0.99 (sum of rounded
    terms), never re-rounded and never 1.00 (round of the unrounded sum)."""
    base = _r(id="b", kind="selector", role="base", when="True", compute="0")
    ps = [_r(id=f"p{i}", kind="selector", role="premium", when="True", compute="1 / 3")
          for i in range(3)]
    res = calculate({}, [{"name": "A"}], [base, *ps])
    assert res.line_items[0].total == 0.99
    assert res.total == 0.99


def test_outputs_stay_float_for_snapshot_hashes():
    """Decimals never leak out: snapshots/ledger hash the same JSON shape as before."""
    res = _ot(53.40, 8)
    assert res.total == 640.80 and type(res.total) is float
    rd = res.to_dict()
    for li in rd["line_items"]:
        assert type(li["total"]) is float
        for step in li["trace"]:
            assert not any(type(v).__name__ == "Decimal" for v in step["inputs"].values())
            assert type(step["value"]).__name__ != "Decimal"


# --- the declared rounding policy ------------------------------------------------ #
def test_shipped_case_declares_the_policy():
    with open(os.path.join(ROOT, "cases", "santacruz", "case.yaml")) as f:
        manifest = yaml.safe_load(f)
    pol = rounding_policy(manifest)
    assert pol == RoundingPolicy(places=2, mode="half_up", granularity="term")


def test_policy_defaults_when_absent():
    assert rounding_policy({}) == RoundingPolicy()
    assert rounding_policy(None) == RoundingPolicy()
    assert RoundingPolicy.from_config(3).places == 3     # legacy int argument


def test_half_up_vs_half_even():
    rule = _r(id="s", kind="selector", role="base", when="True",
              compute="subject_base_hourly * 1")
    subj = [{"name": "A", "base_hourly": 2.665}]
    assert calculate({}, subj, [rule]).total == 2.67                         # default half_up
    assert calculate({}, subj, [rule], {"mode": "half_even"}).total == 2.66
    # 2.675 is 2.67499999… in binary; the policy sees the decimal 2.675 -> 2.68.
    assert calculate({}, [{"name": "A", "base_hourly": 2.675}], [rule]).total == 2.68


@pytest.mark.parametrize("bad", [{"mode": "truncate"}, {"granularity": "line"},
                                 {"places": 9}])
def test_unsupported_policy_refused(bad):
    with pytest.raises(ValueError):
        RoundingPolicy.from_config(bad)


# --- the flag pair compares like with like --------------------------------------- #
def test_flag_alternate_includes_the_same_premiums():
    base = _r(id="ot", kind="selector", role="base", when="True",
              compute="subject_base_hourly * 1.5 * hours",           # 53.40*1.5*8 = 640.80
              flags=[{"when": "True", "message": "1.5x or 2x?",
                      "alternate": "subject_base_hourly * 2 * hours"}])  # 854.40
    uniform = _r(id="u", kind="selector", role="premium", when="True", compute="25.005")
    res = calculate({"hours": 8}, [{"name": "A", "base_hourly": 53.40}], [base, uniform])
    li = res.line_items[0]
    flag = li.flags[0]
    assert flag["primary"] == li.total == 665.81                 # 640.80 + 25.01
    assert flag["alternate"] == 879.41                       # base + same premium
    assert type(flag["alternate"]) is float
