"""The ratification gate: known-answer checks, coverage, the gate verdict, cell checks
and the try-to-break mutation run.
"""
from __future__ import annotations

from .. import cellcheck, governance, queryfacts
from ..engine import NoRuleApplies, calculate, rounding_policy
from ..ruledsl import SHIFT_BASES, Rule
from .rulestore import _live_dicts, _proposed_queue, _with_live
from .state import _catalog


def _check_golden(case, rule_dicts: list[dict], golden: dict) -> tuple[bool, dict]:
    """Run the candidate rules against one known answer (analyst-derived on the shipped
    case; a real paystub once the district supplies one).

    Returns (ok, detail). `detail["status"]` is one of:
      "pass"    — reproduces the known answer.
      "fail"    — produces a DIFFERENT number. Blocks. This is what the gate is for.
      "pending" — produces no number at all: no rule in this library covers the
                  scenario. NOT a failure, and must not block.

    The pending/fail distinction is load-bearing. A golden covers one bargaining unit;
    a corpus has several. Treating "no rule covers this yet" as a failure meant approving
    the police rules was blocked by the public-works golden, and every corpus had to be
    ratified in one atomic all-or-nothing action — which is precisely the workflow the
    review queue exists to avoid. A library that cannot answer a scenario REFUSES at run
    time, which is safe; a library that answers it wrongly is the actual danger. Only the
    latter blocks. Completeness is reported by the Verification tab, not enforced here.
    """
    try:
        rules = case.assign_scope(
            [Rule.from_dict({**r, "status": "ratified", "approver": "candidate"})
             for r in rule_dicts])
        names = set(golden.get("subjects") or [])
        subs = [s for s in case.subjects() if not names or s.get("name") in names]
        # Mirror run time: governance narrows rules to the document(s) that actually
        # govern these subjects, so rules from other units' MOUs can't contaminate the
        # check (a corpus has many MOUs; only one governs a given employee).
        units = sorted({s.get("bargaining_unit") for s in subs if s.get("bargaining_unit")})
        # A golden verifies ONE kind of answer. Only rules of that result_type compete,
        # mirroring run time (a "5 days" bereavement selector must not hijack a money
        # golden). Pay is one chapter of the rulebook, not all of it. The type filter
        # runs BEFORE supersession, in the same order as chat (see the costing path), so
        # an amendment contributing only a non-currency rule cannot trigger supersession
        # here and not there (E2).
        want = golden.get("result_type", "currency")
        rules = [r for r in rules if r.result_type == want]
        # A known answer that names its `topic` is checked against the base rules of
        # that topic only, mirroring run time: a costing question reaches only the base
        # rules of the pay type it asked for (costing.cost_by_unit), and an entitlement
        # question reaches one topic (rulematch.select_rules). Without this, a unit with
        # two entitlements of the same type ("11.08 hours of sick leave", "7.38 hours of
        # vacation") would answer one known answer with the other's rule. Differentials
        # and premiums are not filtered (longevity still reaches the overtime base).
        # A known answer with no topic keeps the old behaviour: every base competes.
        topic = str(golden.get("topic") or "").strip()
        if topic:
            rules = [r for r in rules if r.role != "base" or r.topic == topic]
        if units:
            gov = governance.resolve(units, golden.get("params", {}).get("date_iso"),
                                     case.manifest.get("sources", []))
            if gov.resolved:
                rules = [r for r in rules
                         if (not r.citation.doc_id) or (r.citation.doc_id in gov.doc_ids)]
                rules, _ = governance.apply_supersession(
                    rules, case.manifest.get("sources", []), gov.doc_ids)
        # Seed every declared query param so a rule referencing a legitimate fact the
        # golden didn't set (e.g. date_iso) evaluates to a safe default instead of
        # exploding. Mirrors run time, where chat always supplies all of them.
        params = {"hours": 0.0, "date": "", "date_iso": "", "holiday_weekday": ""}
        params.update(queryfacts.query_defaults(case))  # J1a: every declared question fact
        params.update(golden.get("params", {}))
        # A currency scenario is a shift cost — same basis filter as run time, so approving
        # a whole MOU (uniform allowance, medical, life insurance) does not blow the check
        # up with a year of benefits. Non-currency scenarios (days/hours entitlements) are
        # not periodic pay and are not filtered.
        scope = SHIFT_BASES if want == "currency" else None
        res = calculate(params, subs, rules, rounding_policy(case.manifest), basis_scope=scope)
        expected = float(golden["expected_total"])
        actual = float(res.total)
        ok = abs(actual - expected) < 0.005
        # A wrong number is only a FAILURE when the scenario is fully authored. If a
        # governing document contributes no rules yet (a side letter whose replacement
        # rule nobody has drafted), the number is computed from an incomplete library —
        # that is "pending", not "the rules are wrong". Observed live: ratifying the 2025
        # base-MOU rules made the 2026 side-letter scenario read FAIL at 4388.80, because
        # the 6.5% amendment rule did not exist yet. Fail must mean exactly one thing —
        # authored rules that disagree with the paystub — or the Verification tab trains
        # people to ignore red.
        unauthored = []
        if not ok and units and gov.resolved:
            covered = {r.citation.doc_id for r in rules}
            unauthored = [d for d in gov.doc_ids if d not in covered]
        status = "pass" if ok else ("pending" if unauthored else "fail")
        detail = {"scenario": golden.get("name", "golden"), "expected": expected,
                  "actual": actual, "status": status,
                  "per_subject": {li.subject: li.total for li in res.line_items},
                  # Which selector actually WON for each subject. On a failure this is the
                  # actionable fact: "compare the rule against its clause" is only useful
                  # if the reviewer knows WHICH rule fired.
                  "chosen": sorted({li.rule_id for li in res.line_items}),
                  # EVERY rule that contributed to the number — the winning base plus any
                  # differential or premium that fired. Coverage ("which known answer
                  # proves this rule?") is computed from this, so a live premium that
                  # never fires is reported as unproven instead of hiding behind the base
                  # that won (E1).
                  "fired": sorted({t.rule_id for li in res.line_items for t in li.trace
                                   if t.kind in ("modifier", "selector-chosen", "premium")})}
        if unauthored:
            detail["note"] = (f"Not fully authored: {', '.join(unauthored)} governs this "
                              f"scenario but has no approved rules yet. Draft the rules "
                              f"for this scenario to complete it.")
        return (ok or bool(unauthored)), detail
    except NoRuleApplies as e:
        # Nothing in this library covers the scenario. Run time would refuse, which is
        # the correct and safe outcome — so this cannot block a ratification.
        return True, {"scenario": golden.get("name", "golden"), "status": "pending",
                      "expected": golden.get("expected_total"), "actual": None,
                      "error": str(e),
                      "note": "No live rule covers this scenario yet, so Kenny refuses "
                              "to cost it rather than guessing. Approve the rules for "
                              "this unit and this check will run."}
    except Exception as e:
        return False, {"scenario": golden.get("name", "golden"), "status": "fail",
                       "error": str(e),
                       "expected": golden.get("expected_total"), "actual": None}


def _coverage(case, rule_dicts: list[dict]) -> tuple[dict, dict]:
    """(statuses, proved_by) for a candidate library.

    statuses[name]   the _check_golden detail for each known answer.
    proved_by[id]    the names of the PASSING known answers in which rule `id` actually
                     fired. A rule absent from proved_by is proven by nothing: no known
                     answer exercises it, so nothing says it is right (E1).
    """
    statuses: dict[str, dict] = {}
    proved: dict[str, list[str]] = {}
    for g in case.golden_cases():
        _, d = _check_golden(case, rule_dicts, g)
        statuses[g["name"]] = d
        if d.get("status") == "pass":
            for rid in d.get("fired") or []:
                proved.setdefault(rid, []).append(g["name"])
    return statuses, proved


def _gate_verdict(case, selected: list[dict], approver: str, led=None,
                  attempt: str | None = None) -> dict:
    """The known-answer gate, as one decision (E1). Used by /admin/ratify and, with
    `attempt='mutation-test'`, by /admin/try_break so a deliberate error is refused in
    the gate's own words.

    Judges the live library MERGED BY ID with `selected`, before and after. All-or-nothing:
      R1  a known answer is 'fail' after and was not 'fail' before   (pass/pending -> fail)
      R2  a known answer was 'pass' before and is not 'pass' after   (pass -> pending)
      R3  a `_scenario`-tagged target must pass
      R4  every selected rule must fire in a passing known answer   (coverage)
      R5  a live rule outside the selection that was proven before must still be proven
    'Pending' elsewhere (a unit nobody has authored yet) never blocks: incremental,
    unit-by-unit authoring must stay possible. Completeness is the Verification tab's job.

    Returns {"approved": bool, "response": <the HTTP body when blocked>, "proved_by": {...},
             "checks": [detail...]}. Ledger events are written only when `led` is given;
             a mutation test passes led=None and records one summary event itself.
    """
    def _block(reason: str, response: dict, **extra) -> dict:
        if led is not None:
            led.append("authoring.blocked",
                       {"reason": reason, "approver": approver,
                        **({"attempt": attempt} if attempt else {}), **extra},
                       actor="admin")
        return {"approved": False, "response": {"ratified": [], **response},
                "proved_by": {}, "checks": checks, "ledger_checks": ledger_checks}

    checks: list[dict] = []
    ledger_checks: list[tuple[int | None, dict]] = []   # (golden_check seq, detail) for F2
    before_st, proved_before = _coverage(case, _live_dicts(case))
    before = {n: d.get("status") for n, d in before_st.items()}
    candidate = _with_live(case, selected)
    after_st, proved_after = _coverage(case, candidate)
    targets = {r.get("_scenario") for r in selected if r.get("_scenario")}
    for golden in case.golden_cases():
        name = golden["name"]
        detail = after_st[name]
        status = detail.get("status")
        ok = status in ("pass", "pending")
        checks.append(detail)
        if led is not None:
            gev = led.append("authoring.golden_check", {"passed": ok, **detail}, actor="admin")
            ledger_checks.append((gev["seq"], detail))
        was = before.get(name)
        # R1: a wrong number where there was a right one, or none.
        if status == "fail" and was != "fail":
            if was == "pass":
                why = (f"this would BREAK a check that was passing. “{name}” dropped "
                       f"to {detail.get('actual')} (known answer {detail.get('expected')}). "
                       f"Remove the rule that changed it.")
            else:
                why = (f"“{name}” must come to {detail.get('expected')}, but these "
                       f"rules produce {detail.get('actual')}. Compare each rule against "
                       f"its clause.")
            return _block("known answer fails", {"golden_failed": detail,
                                                 "warning": "Nothing was approved — " + why},
                          scenario=name, was=was, now=status)
        # R2: a known answer that reproduced would no longer be answered at all.
        if was == "pass" and status != "pass":
            return _block("uncovers a known answer",
                          {"golden_failed": detail,
                           "warning": f"Nothing was approved — “{name}” was "
                                      f"reproduced and would no longer be answered at all. "
                                      f"A rule you selected replaces the one that proved it."},
                          scenario=name, was=was, now=status)
        # R3: the scenario these rules are FOR must actually reproduce its answer.
        if name in targets and status != "pass":
            actual = detail.get("actual")
            fired = detail.get("chosen") or detail.get("fired") or []
            how = (f"could not be computed ({detail.get('error', 'unknown')})"
                   if actual is None else
                   f"produced {actual}" + (f" — the rule that fired was “{fired[0]}”"
                                           if fired else ""))
            return _block("target scenario did not reproduce",
                          {"golden_failed": detail,
                           "warning": f"Nothing was approved — “{name}” must come "
                                      f"to {detail.get('expected')}, but the drafted rules "
                                      f"{how}. Compare each rule against its clause, or "
                                      f"re-draft the scenario."},
                          scenario=name, was=was, now=status)
    # R4 / R5: coverage. A rule nothing proves cannot go live, and approving must not
    # leave a previously-proven live rule proven by nothing.
    sel_ids = [r.get("id") for r in selected]
    uncovered = [i for i in sel_ids if i not in proved_after]
    orphaned = sorted(i for i in proved_before if i not in proved_after and i not in sel_ids)
    if uncovered or orphaned:
        return _block("not exercised by any known answer",
                      {"uncovered": uncovered, "orphaned": orphaned,
                       "warning": "Nothing was approved — no known answer exercises: "
                                  + ", ".join(uncovered + orphaned)
                                  + ". A rule nothing proves cannot go live. Add a known "
                                    "answer that uses it, or untick it."},
                      uncovered=uncovered, orphaned=orphaned)
    if (cb := _cell_gate(case, selected, _block)): return cb   # vacation-accrual (J1b/D2): a right number on an unconfirmed OCR cell  # noqa: E701
    return {"approved": True, "response": None,
            "proved_by": {i: proved_after[i] for i in sel_ids}, "checks": checks,
            "ledger_checks": ledger_checks}


# --- gate ---
# 'Try to break it' (DEMO_TICKETS.md E3): mutate one rule in memory, run every known
# answer against each mutant, and report which deliberate errors the known answers catch.
# No model call, nothing written to any rule file; one ledger event records the attempt.
def _format_amount(value, result_type: str) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if result_type == "currency":
        return f"${v:,.2f}"
    if v.is_integer():
        return f"{int(v)} {result_type}".strip()
    return f"{v:g} {result_type}".strip()


def _rule_for_break(case, rule_id: str) -> dict | None:
    """The rule to mutate: the proposed queue's copy first, else the live library's."""
    for r in _proposed_queue().get("rules", []):
        if r.get("id") == rule_id:
            return r
    for r in _live_dicts(case):
        if r.get("id") == rule_id:
            return r
    return None


def _try_break(case, rule: dict) -> dict:
    """Run the mutation check for one rule dict. Pure apart from reading the case."""
    from ..mutate import mutants as _mutants
    rid = rule.get("id")
    want = rule.get("result_type", "currency")
    govern = [s["id"] for s in case.manifest.get("sources", [])
              if s.get("doc_type") in ("MOU", "amendment")]
    base_lib = _with_live(case, [rule])
    base_st, base_proved = _coverage(case, base_lib)
    rows = []
    for m in _mutants(rule, case.field_values(), govern):
        lib = [m["rule"] if r.get("id") == rid else r for r in base_lib]
        after_st, _ = _coverage(case, lib)
        caught_by = None
        for name, b in base_st.items():
            a = after_st[name]
            if b.get("status") == "pass" and a.get("status") != "pass":
                caught_by = {"scenario": name, "expected": b.get("expected"),
                             "actual": a.get("actual"), "status": a.get("status"),
                             "expected_text": _format_amount(b.get("expected"), want),
                             "actual_text": _format_amount(a.get("actual"), want)}
                break
        # The gate's own refusal, so the screen shows what Approve would have said.
        verdict = _gate_verdict(case, [m["rule"]], "mutation-test", None,
                                attempt="mutation-test")
        rows.append({"label": m["label"], "kind": m["kind"], "caught": caught_by is not None,
                     "caught_by": caught_by,
                     "refusal": (verdict["response"] or {}).get("warning")
                     if not verdict["approved"] else None,
                     "when": m["rule"].get("when"), "compute": m["rule"].get("compute")})
    caught = sum(1 for r in rows if r["caught"])
    fires = rid in base_proved
    if not fires or caught == 0:
        verdict_word = "not actually tested"
    elif caught == len(rows):
        verdict_word = "tested"
    else:
        verdict_word = "partly tested"
    return {"rule_id": rid, "result_type": want, "total": len(rows), "caught": caught,
            "verdict": verdict_word, "fires_in": base_proved.get(rid, []),
            "survivors": [r["label"] for r in rows if not r["caught"]],
            "mutants": rows}


# --------------------------------------------------------------------------- #
# --- vacation-accrual ---
# DEMO_TICKETS.md J1b (+ the "confirm value" slice of D2): five tiered vacation-accrual
# rules cite ONE cell each of the OCR-damaged p.22 table ({page, row, col, quoted_text,
# stored_text} under citation.cell). The ratify gate refuses a rule whose cited cell is
# disputed until a person confirms the value on the page image; the confirmation is
# written beside the stored and re-read values (never over them) and ledgered.
def _cell_gate(case, selected: list[dict], block) -> dict | None:
    """The hook _gate_verdict calls last: cellcheck.gate_rules over the selection.
    Returns the gate's blocked verdict (via `block`) or None when every cited cell is
    evidence. Runs after the known-answer checks on purpose — a wrong number is still
    refused as a wrong number; this refuses a RIGHT number resting on an unread cell."""
    errors = cellcheck.gate_rules(case.dir, _catalog(case), selected)
    if not errors:
        return None
    first = next(iter(errors.values()))[0]
    return block("unverified OCR cell",
                 {"rejected": errors,
                  "warning": "Nothing was approved — the evidence behind "
                             + ", ".join(errors) + " is an OCR cell nobody has confirmed: "
                             + first},
                 rejected=errors)


def _cell_gate_rows(case, rules: list[dict]) -> list[dict]:
    """Per-rule cell evidence, for the admin surface: what the gate would say today."""
    cat = _catalog(case)
    out = []
    for r in rules:
        cit = r.get("citation") or {}
        ref = cit.get("cell") or None
        cell = None
        if ref:
            try:
                cell = cellcheck.find_cell(case.dir, cit.get("doc_id", ""),
                                           int(ref.get("page", cit.get("page"))),
                                           int(ref["row"]), int(ref["col"]))
            except (KeyError, TypeError, ValueError):
                cell = None
        errs = cellcheck.gate_rule(case.dir, cat, r)
        out.append({"rule_id": r.get("id"), "doc_id": cit.get("doc_id"),
                    "page": cit.get("page"), "cell_ref": ref,
                    "cell": ({k: cell.get(k) for k in ("col", "header", "stored", "reread",
                                                       "score", "status", "confirmed")}
                             if cell else None),
                    "errors": errs, "approvable": not errs})
    return out
