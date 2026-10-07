"""Refusal answers for the costing path (DEMO_TICKETS.md I6).

A refusal is read by a buyer, not an engineer: it names the contract by its declared
title (never a document id), says in one plain sentence why nothing was computed, and
points at an admin tab that exists. One helper builds every `mode: blocked` response
so the four refusal sites in core/app.py cannot drift apart again (they did: two of
them sent the reader to "Admin → Ingest" and "Admin → Rule review", tabs that do not
exist, and all four printed literal markdown asterisks around a document id).

The admin tabs are `docs`, `verify`, `review`, `lib`, `audit` (admin.html `TABS`);
tests/test_chat_correctness.py parses that list out of the served page and checks
every `next.href` here lands on one of them.
"""
from __future__ import annotations

# Admin tab anchors (admin.html: `const TABS = [...]`; `location.hash` selects one).
TAB_VERIFY = "/admin#verify"
TAB_LIBRARY = "/admin#lib"
TAB_DOCUMENTS = "/admin#docs"

NEXT = {
    "no_rules": {"label": "See what has been verified", "href": TAB_VERIFY},
    "stale_rules": {"label": "Open the rule library", "href": TAB_LIBRARY},
    "not_covered": {"label": "Open the rule library", "href": TAB_LIBRARY},
    "provenance": {"label": "See the documents", "href": TAB_DOCUMENTS},
    # costing-correctness (B2) and agentic (A4) refusal kinds, folded in at integration
    "no_contract": {"label": "See the documents", "href": TAB_DOCUMENTS},
    "rule_error": {"label": "Open the rule library", "href": TAB_LIBRARY},
}


def doc_meta(case, doc_id: str) -> dict:
    """Same shape as core/app.py::_doc_meta — duplicated here so this module does not
    import the app (and the app's refusal sites stay a one-line call)."""
    s = case.source_by_id(doc_id) or {}
    return {"doc_id": doc_id, "title": s.get("title", doc_id),
            "department": s.get("department"), "doc_type": s.get("doc_type")}


def _join(names: list[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def blocked(case, qid: str, chosen_docs: list[str], subjects: list[dict],
            reason: str, detail: str | None = None, stale: list | None = None) -> dict:
    """Build a `mode: blocked` chat response.

    reason: 'no_rules' | 'stale_rules' | 'not_covered' | 'provenance' | 'no_contract'
            | 'rule_error'.
    detail: the engine's or integrity check's own words, quoted where the reason
            needs them (not_covered, provenance).
    """
    docs = [doc_meta(case, d) for d in chosen_docs]
    title = _join([d["title"] for d in docs]) or "this contract"
    names = _join([str(s.get("name") or s.get("classification") or "")
                   for s in (subjects or [])])
    who = names or "this classification"

    if reason == "no_rules":
        message = (f"I can't cost this yet. {title} is the contract that covers {who}, "
                   "and no costing rule from it has been approved by a person. Nothing "
                   "is computed until someone approves a rule against its clause. I can "
                   "still quote the contract.")
    elif reason == "stale_rules":
        n = len(stale or [])
        message = (f"{_plural(n, 'approved rule')} for {title} "
                   f"{'is' if n == 1 else 'are'} on hold because the document was "
                   "re-read after they were approved. A person has to re-verify them "
                   "before costing resumes. I can still quote the contract.")
    elif reason == "not_covered":
        why = f" ({detail})" if detail else ""
        message = (f"The approved rules for {title} do not cover this case{why}. "
                   "Nothing is computed from a rule that does not apply. I can still "
                   "quote the contract.")
    elif reason == "provenance":
        why = f" {detail}" if detail else ""
        message = ("I can't cost this: the source documents no longer match what the "
                   f"rules for {title} were approved against.{why} Re-read the "
                   "documents and re-verify the rules before answering.")
    elif reason == "no_contract":
        why = f"{detail}. " if detail else ""
        message = (f"{why}I only cost a classification under a contract that governs "
                   "its bargaining unit on the shift date. I can still quote the "
                   "documents.")
    elif reason == "rule_error":
        why = f" ({detail})" if detail else ""
        message = (f"I can't cost this: an approved rule for {title} failed to "
                   f"evaluate{why}. Nothing was computed. A person has to fix that rule "
                   "before costing resumes. I can still quote the contract.")
    else:  # pragma: no cover — a programming error, not a user-facing state
        raise ValueError(f"unknown refusal reason {reason!r}")

    return {"query_id": qid, "needs_confirmation": False, "mode": "blocked",
            "chosen_doc": ", ".join(chosen_docs), "chosen_docs": docs,
            "reason": reason, "message": message, "next": dict(NEXT[reason])}
