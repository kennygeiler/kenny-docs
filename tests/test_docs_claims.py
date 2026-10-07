"""The written claims about the approval gate match what the code computes (E7).

A README that promises a stronger gate than exists is the first thing a buyer catches.
These are the exact sentences the audit found to be false; they must stay gone."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_readme_does_not_overclaim_the_gate():
    readme = _read("README.md")
    assert "A rule can't ship unless it reproduces a known-correct answer" not in readme
    assert "verified acceptance tests" not in readme
    assert "caught real drafting errors" not in readme
    assert "fires in no known answer" in readme


def test_prd_states_the_gate_as_built():
    prd = _read("PRD.md")
    assert "A rule goes live only\n   after it reproduces" not in prd
    assert "A known answer is a real paystub" not in prd
    assert "fires in a known answer" in prd


def test_architecture_describes_the_five_rules():
    arch = _read("ARCHITECTURE.md")
    assert "Approval is a regression guard" not in arch
    assert "R4 a selected rule fires in no" in arch
    assert "/admin/try_break" in arch
