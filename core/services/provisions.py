"""Provision inventory and coverage (ROADMAP M1.5): how much of a contract answers with a
number, how much is drafted and waiting for a person, and what is deliberately not
modelled, with the reason.

The inventory is cases/<case>/provisions.yaml (or the file named by a `provisions:` key in
case.yaml). No file means the feature is hidden: /api/coverage says `available: false`
and the chat line, admin Coverage tab and per-section review grouping do not render.

Accepted shapes (all three normalise to a list of documents):

  # one document
  doc_id: firefighters_local3535_mou
  generated: 2026-10-28
  label: Local 3535 MOU          # optional short name for the chat line and refusals
  provisions: [ ... ]

  # several documents
  documents:
    - {doc_id: ..., provisions: [ ... ]}
    - {doc_id: ..., provisions: [ ... ]}

  # or a bare top-level list of the per-document blocks above

Each provision: id, title, article, pages, category, value_kind, status
(modelled | not_modelled), rules (rule ids), reason (when not modelled), quote.

A provision's state is derived from the rule files, never from the yaml alone:
  live          modelled, and every rule it names is ratified and approved
  awaiting      modelled, every rule exists, at least one is only proposed (or stale)
  not_modelled  declared not modelled, with its reason
  error         modelled but names no rule, or names a rule id that exists nowhere
                (a data error: shown in admin, never hidden)
Nothing is excluded from the totals.
"""
from __future__ import annotations

import os
import re

import yaml

from .rulestore import _raw_ratified
from .state import _case

DEFAULT_FILE = "provisions.yaml"


def provisions_path(case) -> str | None:
    """The inventory file: the manifest's `provisions:` key if set, else the convention."""
    p = case.path("provisions") if case.manifest.get("provisions") else \
        os.path.join(case.dir, DEFAULT_FILE)
    return p if p and os.path.exists(p) else None


def load_documents(case) -> list[dict]:
    """The inventory as a list of {doc_id, generated, label, provisions}. [] when absent."""
    path = provisions_path(case)
    if not path:
        return []
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if isinstance(data, dict) and "documents" in data:
        docs = data.get("documents") or []
    elif isinstance(data, dict):
        docs = [data]
    elif isinstance(data, list):
        docs = data
    else:
        docs = []
    return [d for d in docs if isinstance(d, dict) and d.get("doc_id")]


def short_title(case, doc: dict) -> str:
    """A short document name for one line of text: the inventory's `label`, else the
    declared title without its trailing parenthetical."""
    if doc.get("label"):
        return str(doc["label"])
    src = case.source_by_id(doc["doc_id"]) or {}
    title = str(src.get("title") or doc["doc_id"])
    return re.sub(r"\s*\([^)]*\)\s*$", "", title) or title


def _rule_states(case) -> dict[str, str]:
    """rule id -> live | stale | proposed (live wins when a rule is in both files)."""
    states: dict[str, str] = {}
    qpath = case.path("proposed_rules", "rules/rules_proposed.json")
    queue = []
    if qpath and os.path.exists(qpath):
        import json
        with open(qpath) as f:
            queue = (json.load(f) or {}).get("rules", [])
    for r in queue:
        if r.get("id"):
            states[r["id"]] = "proposed"
    for r in _raw_ratified(case):
        rid = r.get("id")
        if not rid:
            continue
        if r.get("status") == "ratified" and r.get("approver"):
            states[rid] = "live"
        elif states.get(rid) != "live":
            states[rid] = "stale"
    return states


def _rule_topics(case) -> dict[str, str]:
    """rule id -> topic, from the proposed queue and the ratified library."""
    topics: dict[str, str] = {}
    qpath = case.path("proposed_rules", "rules/rules_proposed.json")
    if qpath and os.path.exists(qpath):
        import json
        with open(qpath) as f:
            for r in (json.load(f) or {}).get("rules", []):
                if r.get("id"):
                    topics[r["id"]] = str(r.get("topic") or "")
    for r in _raw_ratified(case):
        if r.get("id"):
            topics[r["id"]] = str(r.get("topic") or "")
    return topics


def _pages(p: dict) -> list[int]:
    raw = p.get("pages") or []
    if not isinstance(raw, list):
        raw = [raw]
    out = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def coverage(case=None) -> dict:
    """GET /api/coverage: per-document counts plus every provision with its state."""
    case = case or _case()
    docs = load_documents(case)
    if not docs:
        return {"available": False, "documents": [], "rule_index": {}}
    states = _rule_states(case)
    out_docs, rule_index = [], {}
    for d in docs:
        doc_id = d["doc_id"]
        src = case.source_by_id(doc_id) or {}
        counts = {"total": 0, "live": 0, "awaiting": 0, "not_modelled": 0, "errors": 0,
                  # numeric: provisions that yield a value (value_kind != none); the rest
                  # are narrative or procedure, answered by quoting the clause.
                  "numeric": 0, "quoted": 0}
        provs = []
        for p in d.get("provisions") or []:
            if not isinstance(p, dict):
                continue
            rule_ids = [str(r) for r in (p.get("rules") or [])]
            declared = str(p.get("status") or "")
            rules = [{"id": r, "state": states.get(r, "missing")} for r in rule_ids]
            error = ""
            if declared == "not_modelled":
                state = "not_modelled"
            elif not rule_ids:
                state, error = "error", "marked modelled but names no rule"
            elif any(r["state"] == "missing" for r in rules):
                state = "error"
                error = "names a rule that exists nowhere: " + ", ".join(
                    r["id"] for r in rules if r["state"] == "missing")
            elif all(r["state"] == "live" for r in rules):
                state = "live"
            else:
                state = "awaiting"
            counts["total"] += 1
            counts["quoted" if (p.get("value_kind") or "none") == "none" else "numeric"] += 1
            counts["errors" if state == "error" else state] += 1
            item = {"id": p.get("id", ""), "title": p.get("title", ""),
                    "article": p.get("article", "") or "", "pages": _pages(p),
                    "category": p.get("category", ""), "value_kind": p.get("value_kind", ""),
                    "status": declared, "state": state, "rules": rules,
                    "reason": p.get("reason", "") or "", "quote": p.get("quote", "") or "",
                    "error": error, "doc_id": doc_id}
            provs.append(item)
            for r in rule_ids:
                rule_index.setdefault(r, {"provision": item["id"], "title": item["title"],
                                          "article": item["article"], "pages": item["pages"],
                                          "doc_id": doc_id})
        out_docs.append({"doc_id": doc_id, "title": src.get("title", doc_id),
                         "short_title": short_title(case, d),
                         "generated": str(d.get("generated", "") or ""),
                         **counts, "provisions": provs})
    return {"available": True, "documents": out_docs, "rule_index": rule_index}


# --------------------------------------------------------------------------- #
# Chat refusals (M1.5): name the provision a refused costing question is about.
# --------------------------------------------------------------------------- #
def _cue_hit(cue: str, text: str) -> bool:
    words = [re.escape(w) for w in re.sub(r"[\-_]+", " ", cue.lower()).split()]
    if not words:
        return False
    return re.search(r"(?<![\w.])" + r"\s+".join(words) + r"(?![\w])", text) is not None


def refusal_note(case, doc_ids: list[str], asked: list[str],
                 lexicon: dict[str, list[str]]) -> tuple[str, list[dict]]:
    """At most ONE sentence naming the provision a refused costing question is about.

    Best match: a provision that is not live and has a rule whose `topic` is the asked pay
    type (the same key chat routes on). Fallback: a not-live provision whose title STARTS
    with the pay type or one of its cues ("Holiday pay ..."), so a title that merely
    mentions the word ("... not holiday pay ...") is not named. Awaiting beats not
    modelled. Live provisions are skipped: a refusal with a live rule is about the
    scenario, not a missing rule."""
    try:
        cov = coverage(case)
        topics = _rule_topics(case)
    except Exception:
        return "", []
    if not cov["available"] or not asked:
        return "", []
    cues = [c for pt in asked for c in [pt, *lexicon.get(pt, [])]]
    ranked = []
    for d in cov["documents"]:
        if doc_ids and d["doc_id"] not in doc_ids:
            continue
        for p in d["provisions"]:
            if p["state"] == "live":
                continue
            by_topic = any(topics.get(r["id"]) in asked for r in p["rules"])
            head = re.sub(r"[\-_.:]+", " ", p["title"].lower()).strip()
            by_title = any(_cue_hit(c, head) and re.match(
                r"\s*" + r"\s+".join(map(re.escape, re.sub(r"[\-_]+", " ", c.lower()).split())), head)
                for c in cues)
            if not (by_topic or by_title):
                continue
            score = (0 if by_topic else 1, 0 if by_title else 1,
                     0 if p["state"] == "awaiting" else 1)
            ranked.append((score, d, p))
    if not ranked:
        return "", []
    ranked.sort(key=lambda x: x[0])
    _, d, p = ranked[0]
    where = d["short_title"] + (f" p.{p['pages'][0]}" if p["pages"] else "")
    label = f"{p['title']} ({where})"
    if p["state"] == "awaiting":
        sentence = f"{label} is drafted and awaiting approval."
    elif p["state"] == "not_modelled":
        why = (p["reason"] or "").strip().rstrip(".")
        sentence = f"{label} is not modelled" + (f": {why}." if why else ".")
    else:
        sentence = f"{label} has a data error in the provision list."
    named = [{"id": p["id"], "title": p["title"], "state": p["state"],
              "doc_id": d["doc_id"], "pages": p["pages"], "reason": p["reason"]}]
    return sentence, named
