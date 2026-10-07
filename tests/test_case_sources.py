"""Bind the hand-entered Santa Cruz case data to the documents' own text (DEMO_TICKETS B5).

Every fact below is read back from the SHIPPED catalog.json (the OCR'd text layer of the
real contracts), not from the case manifest it checks:

  - each MOU's declared governance window equals its own Term clause;
  - every roster row belongs to a unit some document governs;
  - every roster rank is listed in its unit's Recognition clause;
  - every roster rate is printed in the salary schedule or the unit's MOU.

No model, no key, no network.
"""
import csv
import difflib
import json
import os
import re
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.caseio import load_case  # noqa: E402

CASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "cases", "santacruz")

_TERM_RE = re.compile(
    r"effective\s+([A-Za-z]+\s+\d{1,2},\s*\d{4}),?\s+through\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})",
    re.I)


def _case():
    return load_case(CASE_DIR)


def _catalog() -> dict:
    with open(os.path.join(CASE_DIR, "catalog.json")) as f:
        docs = json.load(f)["documents"]
    return {d["doc_id"]: d for d in docs}


def _page_text(doc: dict, page: int) -> str:
    return " ".join(c.get("text") or "" for c in doc.get("clauses", [])
                    if c.get("page") == page)


def _doc_text(doc: dict) -> str:
    return " ".join(c.get("text") or "" for c in doc.get("clauses", []))


def _roster() -> list[dict]:
    with open(os.path.join(CASE_DIR, "data", "roster.csv"), newline="") as f:
        return list(csv.DictReader(f))


def _iso(printed: str) -> str:
    return datetime.strptime(re.sub(r"\s+", " ", printed), "%B %d, %Y").date().isoformat()


def _mous() -> list[dict]:
    return [s for s in _case().manifest["sources"] if s.get("doc_type") == "MOU"]


# --------------------------------------------------------------------------- #
# Term clauses
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("src", _mous(), ids=lambda s: s["id"])
def test_declared_terms_match_term_clause(src):
    """case.yaml effective_start/end must equal the 'effective <date>, through <date>'
    sentence on the page the source declares as its Term clause."""
    term = src.get("term") or {}
    assert term.get("page"), f"{src['id']} declares no term page"
    text = _page_text(_catalog()[src["id"]], term["page"])
    m = _TERM_RE.search(text)
    assert m, f"{src['id']} p.{term['page']}: no 'effective ..., through ...' sentence"
    assert src["effective_start"] == _iso(m.group(1)), (src["id"], m.group(1))
    assert src["effective_end"] == _iso(m.group(2)), (src["id"], m.group(2))


def test_every_mou_has_a_term_and_recognition_declaration():
    for src in _mous():
        assert (src.get("term") or {}).get("page"), src["id"]
        rec = src.get("recognition") or {}
        assert rec.get("page") and rec.get("ranks"), src["id"]


# --------------------------------------------------------------------------- #
# Roster <-> governing document
# --------------------------------------------------------------------------- #
def test_every_roster_unit_has_a_governing_source():
    governed = {s.get("bargaining_unit") for s in _mous()}
    orphans = [r["classification"] for r in _roster()
               if r["bargaining_unit"] not in governed]
    assert orphans == [], f"roster rows with no contract on file: {orphans}"


def _recognition_items(src: dict) -> list[str]:
    """Positions named in the Recognition clause on the declared page: the list after
    'consists of the following ...:' split on commas/'and'."""
    text = _page_text(_catalog()[src["id"]], src["recognition"]["page"])
    m = re.search(r"consists of the following[^:]*:\s*(.+?)(?:\.\s|$)", text, re.I | re.S)
    assert m, f"{src['id']}: no 'consists of the following ...:' list on p.{src['recognition']['page']}"
    raw = re.split(r",|\band\b", m.group(1))
    return [re.sub(r"\s+", " ", x).strip(" .") for x in raw if x.strip(" .")]


def _fuzzy_in(rank: str, items: list[str], cutoff: float = 0.85) -> bool:
    r = rank.lower()
    for it in items:
        it_l = it.lower()
        # OCR prints 'Paramadics' and pluralises; also accept a listed position that
        # carries a schedule prefix ('40-hour Battalion Chief').
        if difflib.SequenceMatcher(None, r, it_l).ratio() >= cutoff:
            return True
        if it_l.endswith(r) and difflib.SequenceMatcher(None, r, it_l[-len(r):]).ratio() >= cutoff:
            return True
    return False


@pytest.mark.parametrize("src", _mous(), ids=lambda s: s["id"])
def test_declared_recognition_ranks_appear_in_the_clause(src):
    items = _recognition_items(src)
    missing = [rk for rk in src["recognition"]["ranks"] if not _fuzzy_in(rk, items)]
    assert missing == [], f"{src['id']}: declared ranks not in Recognition clause: {missing}; clause lists {items}"


def test_roster_rank_is_in_its_units_recognition_clause():
    by_unit = {s["bargaining_unit"]: s for s in _mous()}
    bad = []
    for row in _roster():
        src = by_unit.get(row["bargaining_unit"])
        if not src:
            bad.append((row["classification"], "no governing source"))
            continue
        declared = src["recognition"]["ranks"]
        if row["rank"] not in declared:
            bad.append((row["classification"], f"rank {row['rank']!r} not declared for {src['id']}"))
            continue
        if not _fuzzy_in(row["rank"], _recognition_items(src)):
            bad.append((row["classification"], f"rank {row['rank']!r} not in {src['id']} Recognition text"))
    assert bad == [], bad


def test_battalion_chief_is_a_local_3535_classification():
    """The regression B5 fixed: Battalion Chief rows were filed under chief-officers,
    whose Recognition clause lists Division Chiefs only."""
    rows = [r for r in _roster() if r["rank"] == "Battalion Chief"]
    assert rows, "roster lost its Battalion Chief rows"
    assert {r["bargaining_unit"] for r in rows} == {"firefighters-local-3535"}
    coa = next(s for s in _mous() if s["id"] == "chief_officers_mou")
    assert not _fuzzy_in("Battalion Chief", _recognition_items(coa))


# --------------------------------------------------------------------------- #
# Roster rates are printed somewhere in the corpus
# --------------------------------------------------------------------------- #
def test_every_roster_rate_is_printed_in_the_corpus():
    cat = _catalog()
    schedule = _doc_text(cat["master_salary_schedule"])
    by_unit = {s["bargaining_unit"]: s["id"] for s in _mous()}
    unprinted = []
    for row in _roster():
        needle = f"${float(row['base_hourly']):.2f}"
        mou_text = _doc_text(cat[by_unit[row["bargaining_unit"]]]) if row["bargaining_unit"] in by_unit else ""
        if needle not in schedule and needle not in mou_text:
            unprinted.append((row["classification"], needle))
    assert unprinted == [], f"rates that appear nowhere in the corpus: {unprinted}"


def test_police_sample_rows_are_archived_not_live():
    live = [r["classification"] for r in _roster() if r["department"] == "police"]
    assert live == []
    archived = os.path.join(os.path.dirname(CASE_DIR), "..", "archive",
                            "santacruz_roster_police_sample.csv")
    assert os.path.exists(archived)


# --------------------------------------------------------------------------- #
# The trust anchor states its own assumptions
# --------------------------------------------------------------------------- #
def test_overtime_rules_and_the_640_80_golden_state_their_assumptions():
    case = _case()
    g = next(g for g in case.golden_cases() if g["expected_total"] == 640.80)
    assert len(g.get("assumptions") or []) == 2
    assert any("182" in a for a in g["assumptions"])
    with open(os.path.join(CASE_DIR, "rules", "rules_ratified.json")) as f:
        rules = {r["id"]: r for r in json.load(f)["rules"]}
    for rid in ("firefighters_local3535_mou:overtime_premium_rate",
                "admin_group_mou:overtime_premium_rate"):
        assert len(rules[rid].get("assumptions") or []) == 2, rid
    assert rules["firefighters_local3535_mou:overtime_premium_rate"]["assumptions"] == g["assumptions"]


def test_governance_resolves_a_dated_question_today():
    """Add 'on October 7' to a known-answer question and it must still resolve: every
    MOU window covers 2026-10-07 for its unit (the old title-derived windows did not)."""
    from core import governance
    sources = _case().manifest["sources"]
    for unit in ("firefighters-local-3535", "admin-group", "management", "chief-officers"):
        gov = governance.resolve([unit], "2026-10-07", sources)
        assert gov.resolved, (unit, gov.reason)
