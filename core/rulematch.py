"""Anchor ratified rules to retrieved evidence, and scope retrieval by bargaining
unit or named document (DEMO_TICKETS.md B3, B6).

Why this module exists. The entitlement path used to match a ratified rule to a
retrieved chunk by (doc_id, clause label). 1,857 of the 1,869 shipped chunks have an
empty clause label and the rules cite free-text labels ('XIV', 'Bereavement Leave
(p.14)'), so the engine path could never fire and every entitlement question fell
through to a department-wide quote — and department 'fire' covers TWO bargaining
units, so 'a firefighter' was answered from the Chief Officers' contract.

Everything here is deterministic and query-time only (no re-ingest):

  * geometry   — bbox_overlap / anchored: a rule is bound to the page and box it
                 cites, so it matches the chunk that occupies that box, whatever label
                 the ingest gave it.
  * scope      — named_documents / units_for / docs_for: a question is scoped by the
                 document it names, else by the bargaining unit of the classification
                 it names, else by department, else the corpus. 'district' (the
                 salary schedule) is added to every lookup and never offered as a
                 clarify option.
  * query      — retrieval_query strips the tokens that named the scope and the
                 question frame ('what does ... say about'), so the document's own
                 name cannot out-rank its clauses.
  * floor      — coverage: idf-weighted share of the query's content terms present in
                 a hit. Below OUT_OF_SCOPE_FLOOR the question is refused as not in
                 these documents, instead of quoting the least-irrelevant preamble.
  * pinning    — pin_rule_clauses: when a ratified rule's topic is in the question,
                 its cited chunk is put first, so the policy quote and the costing
                 answer box the same clause.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from . import governance, llm
from .index import _TOKEN_RE, tokenize

MIN_OVERLAP = 0.5          # a chunk must cover half of the smaller box to be "the" clause
ANCHOR_MAX_RANK = 3        # a cited chunk this high in the hits selects its rule
OUT_OF_SCOPE_FLOOR = 0.4   # best-hit coverage below this -> not in these documents


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #
def _norm(b) -> tuple[float, float, float, float] | None:
    """PDF boxes arrive as [x0, y0, x1, y1] with y0 > y1 (PDF origin is bottom-left) or
    the other way round, depending on who wrote them. Normalise with min/max."""
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return None
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def bbox_overlap(a, b) -> float:
    """Intersection area / area of the SMALLER box, in [0, 1]. The smaller-box
    denominator means a clause chunk fully inside a wider rule box scores 1.0, and a
    rule box fully inside a page-sized chunk scores 1.0 too — both are 'the same
    evidence'. Disjoint, missing or degenerate boxes score 0.0."""
    na, nb = _norm(a), _norm(b)
    if na is None or nb is None:
        return 0.0
    ax0, ay0, ax1, ay1 = na
    bx0, by0, bx1, by1 = nb
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    if smaller <= 0:
        return 0.0
    return min(1.0, inter / smaller)


def _cit(rule) -> tuple[str, Any, list]:
    c = getattr(rule, "citation", None)
    if c is None and isinstance(rule, dict):
        c = rule.get("citation") or {}
    if isinstance(c, dict):
        return c.get("doc_id", ""), c.get("page"), c.get("bbox") or []
    return getattr(c, "doc_id", ""), getattr(c, "page", None), getattr(c, "bbox", None) or []


def anchored(rule, hits: list[dict], min_overlap: float = MIN_OVERLAP
             ) -> tuple[int, float] | None:
    """(1-based rank, overlap) of the first hit on the rule's cited document AND page
    whose box overlaps the cited box by at least `min_overlap`; None when no hit is
    that evidence. Rank is what the caller gates on: evidence the search ranked near
    the top is the clause the question is about."""
    doc_id, page, bbox = _cit(rule)
    for rank, h in enumerate(hits, 1):
        if h.get("doc_id") != doc_id or h.get("page") != page:
            continue
        ov = bbox_overlap(bbox, h.get("bbox") or [])
        if ov >= min_overlap:
            return rank, ov
    return None


# --------------------------------------------------------------------------- #
# document names and scope
# --------------------------------------------------------------------------- #
_YEAR_RANGE_RE = re.compile(r"\b(19|20)\d{2}\s*[–—-]\s*(19|20)?\d{2,4}\b|\b(19|20)\d{2}\b")
_PAREN_RE = re.compile(r"\([^)]*\)")
# Words a title carries that do not NAME the document: strip them from the tail so
# 'Chief Officers Association MOU' also answers to 'Chief Officers'.
_GENERIC_TAIL = frozenset({"mou", "mous", "memorandum", "understanding", "agreement",
                           "contract", "association", "the", "of", "and", "with"})
_DOC_WORDS = r"(?:mou|mous|contract|agreement|memorandum|document|documents|schedule)"


def doc_short_title(src: dict) -> str:
    """'Firefighters Local 3535 MOU 2025–2028 (with Salary Schedule)' ->
    'Firefighters Local 3535 MOU'. What a person would call the document."""
    t = _PAREN_RE.sub(" ", src.get("title") or src.get("id") or "")
    t = _YEAR_RANGE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip(" ,-–—")


def doc_phrases(src: dict) -> list[str]:
    """Lower-case phrases that name this document in a question: the short title and
    its tail-stripped variants, the bargaining unit, the unit's last two words when it
    has three or more ('Local 3535'), explicit `aliases` from case.yaml, and 'salary
    schedule' for a salary schedule. Longest first so the most specific wins."""
    phrases: set[str] = set()
    short = doc_short_title(src).lower()
    toks = short.split()
    while toks:
        phrases.add(" ".join(toks))
        if toks[-1] in _GENERIC_TAIL:
            toks.pop()
        else:
            break
    unit = (src.get("bargaining_unit") or "").replace("-", " ").strip().lower()
    if unit:
        phrases.add(unit)
        ut = unit.split()
        if len(ut) >= 3:
            phrases.add(" ".join(ut[-2:]))
    for a in src.get("aliases") or []:
        if str(a).strip():
            phrases.add(str(a).strip().lower())
    if src.get("doc_type") == "salary-schedule":
        phrases.add("salary schedule")
    phrases.discard("")
    return sorted(phrases, key=lambda p: (-len(p), p))


def _phrase_re(phrase: str) -> re.Pattern:
    p = re.escape(phrase).replace(r"\ ", r"\s+")
    if " " in phrase:
        return re.compile(rf"\b{p}\b", re.I)
    # A single word ('management') names a document only next to a document word:
    # 'the management MOU', 'MOU for management'. Bare, it is just a topic word.
    return re.compile(rf"\b{p}\b\W+(?:\w+\W+){{0,2}}{_DOC_WORDS}\b"
                      rf"|\b{_DOC_WORDS}\b\W+(?:\w+\W+){{0,2}}{p}\b", re.I)


def named_documents(sources: list[dict], text: str) -> list[tuple[dict, str]]:
    """The declared documents a question names, each with the phrase that named it."""
    t = (text or "").replace("-", " ")
    out: list[tuple[dict, str]] = []
    for src in sources:
        for phrase in doc_phrases(src):
            if _phrase_re(phrase).search(t):
                out.append((src, phrase))
                break
    return out


def source_for_label(sources: list[dict], label: str | None) -> dict | None:
    """A clarify option comes back through the same `department` field the old
    department clarify used. Accept a document id, its full title or its short
    title, so the post-back scopes to that one document."""
    if not label:
        return None
    key = str(label).strip().lower()
    for src in sources:
        if key in {str(src.get("id", "")).lower(), str(src.get("title", "")).lower(),
                   doc_short_title(src).lower()}:
            return src
    return None


@dataclass
class Scope:
    doc_ids: list[str]
    how: str                              # document | unit | department | corpus | none
    consumed: set[str] = field(default_factory=set)   # query stems that named the scope
    units: list[str] = field(default_factory=list)
    named: list[str] = field(default_factory=list)    # doc ids named in the question
    subjects: list[dict] = field(default_factory=list)  # roster rows the question named
    department: str | None = None


def units_for(case, prompt: str, subjects: list[dict] | None = None,
              label: str | None = None) -> tuple[list[str], list[dict], str]:
    """(bargaining units, named roster rows, how) for a question.

    Named classifications win ('a firefighter' -> Firefighter (56 hr, top step) ->
    firefighters-local-3535). Else a document the question names (or a clarify
    post-back label) gives its unit. Else nothing: the caller asks who it is for and
    never searches a whole department, because one department can hold two units."""
    sources = case.manifest.get("sources", [])
    rows = subjects if subjects is not None else case.subjects()
    named = llm._resolve_classifications(prompt, rows)
    chosen = [s for s in rows if s.get("name") in set(named)]
    units = sorted({s.get("bargaining_unit") for s in chosen if s.get("bargaining_unit")})
    if units:
        return units, chosen, "classification"
    src = source_for_label(sources, label)
    docs = [src] if src else [s for s, _ in named_documents(sources, prompt)]
    units = sorted({d.get("bargaining_unit") for d in docs if d.get("bargaining_unit")})
    if units:
        return units, [], "document"
    return [], [], ""


def rank_stems(rows: list[dict]) -> set[str]:
    """Stems of the named rows' labels and rank words — what a question spent on
    saying WHO it is about ('Division Chief (top step)' -> division, chief, top, step)."""
    out: set[str] = set()
    for s in rows:
        out |= set(tokenize(str(s.get("name") or "")))
        out |= set(tokenize(str(s.get("rank") or "")))
    return out


def docs_for(case, ingested: set[str], prompt: str, department: str | None = None,
             lookup: bool = False, subjects: list[dict] | None = None) -> Scope:
    """Which documents a policy/lookup question may be answered from, in this order:
    a document named in the question (or the clarify post-back label) -> that
    document; a named classification -> its unit's governing documents; a stated
    department -> that department's documents (plus citywide); else the corpus.
    A lookup always adds the salary-schedule documents, which sit under department
    'district' and were unreachable from any department scope."""
    sources = case.manifest.get("sources", [])
    rows = subjects if subjects is not None else case.subjects()
    consumed: set[str] = set()
    scope: list[str] = []
    how = "corpus"
    units: list[str] = []
    named: list[str] = []
    chosen: list[dict] = []
    dept: str | None = None

    src = source_for_label(sources, department)
    if src:
        scope, how, named = [src["id"]], "document", [src["id"]]
        consumed |= set(tokenize(doc_short_title(src)))
    else:
        found = named_documents(sources, prompt)
        if found:
            scope, how = [s["id"] for s, _ in found], "document"
            named = list(scope)
            for s, phrase in found:
                consumed |= set(tokenize(phrase))
        else:
            resolved = llm._resolve_classifications(prompt, rows)
            chosen = [s for s in rows if s.get("name") in set(resolved)]
            units = sorted({s.get("bargaining_unit") for s in chosen
                            if s.get("bargaining_unit")})
            if units:
                gov = governance.resolve(units, None, sources)
                scope, how = list(gov.doc_ids), "unit"
                if not lookup:
                    # The classification named the scope; its words must not then
                    # out-rank the clause ('holidays for a firefighter' searches
                    # 'holidays'). A lookup keeps them: the rate row carries them.
                    consumed |= rank_stems(chosen)
            elif department and department in case.departments():
                dept = department
                scope, how = list(case.docs_for_department(department)), "department"
                consumed |= set(tokenize(department))
            else:
                scope = list(case.docs_for_department(None))
    if lookup:
        for s in sources:
            if s.get("doc_type") == "salary-schedule" and s["id"] not in scope:
                scope.append(s["id"])
    scope = [d for d in scope if d in ingested]
    return Scope(scope, how if scope else "none", consumed, units, named, chosen, dept)


# --------------------------------------------------------------------------- #
# retrieval query and relevance floor
# --------------------------------------------------------------------------- #
# Question-frame words: they shape the sentence, never the clause. Stems compared
# through the same tokenizer the index uses.
_FRAME = ("what does do did say says said about how much many long is are was were "
          "the a an of for and or to in on at by be been get gets got give gives "
          "i me my we our you your it its their they them there this that these "
          "those tell explain describe regarding according under per can could would "
          "should will shall may might who whom whose when where which why please "
          "entitled entitlement receive receives mou mous contract agreement "
          "memorandum understanding document documents policy policies "
          "hi hello hey thanks thank ok okay")
_FRAME_STEMS = frozenset(tokenize(_FRAME)) | frozenset(_FRAME.split())


def retrieval_query(prompt: str, consumed: set[str] | None = None) -> tuple[str, list[str]]:
    """(query string, content stems). The query keeps the prompt's own words (the
    backend tokenizes again); the stems are what coverage() measures. Empty when the
    question has no content left — 'hi', or only the name of a document."""
    consumed = consumed or set()
    words, stems = [], []
    for w in _TOKEN_RE.findall((prompt or "").lower()):
        st = tokenize(w)
        if not st:
            continue
        s = st[0]
        if s in consumed or s in _FRAME_STEMS or w in _FRAME_STEMS:
            continue
        words.append(w)
        if s not in stems:
            stems.append(s)
    return " ".join(words), stems


def idf_table(backend) -> dict[str, float] | None:
    """Per-term idf over the backend's chunks (local backends only; None otherwise,
    in which case coverage() weights every term equally). Cached on the backend
    object against its index signature so a re-bake refreshes it."""
    chunks_fn = getattr(backend, "_chunks", None)
    if chunks_fn is None:
        return None
    sig = backend._sig() if hasattr(backend, "_sig") else None
    cached = getattr(backend, "_rulematch_idf", None)
    if cached and cached[0] == sig:
        return cached[1]
    chunks = chunks_fn()
    N = len(chunks)
    df: Counter = Counter()
    for c in chunks:
        for t in set(c.get("_tokens") or tokenize(c.get("text", ""))):
            df[t] += 1
    table = {t: math.log(1 + (N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
    table["__unseen__"] = math.log(1 + (N + 0.5) / 0.5) if N else 1.0
    try:
        backend._rulematch_idf = (sig, table)
    except Exception:
        pass
    return table


def coverage(stems: list[str], text: str, idf: dict[str, float] | None = None) -> float:
    """idf-weighted share of the query's content stems present in `text`, in [0, 1].
    A rare term the hit lacks ('rebate', 'weather') costs far more than a common one
    it has ('santa', 'cruz'), which is what separates an off-corpus question from a
    paraphrase of an in-corpus one."""
    if not stems:
        return 0.0
    present = set(tokenize(text))
    unseen = (idf or {}).get("__unseen__", 1.0)
    w = {t: ((idf or {}).get(t, unseen) if idf else 1.0) for t in stems}
    total = sum(w.values())
    if total <= 0:
        return 0.0
    return sum(v for t, v in w.items() if t in present) / total


def best_coverage(stems: list[str], hits: list[dict], idf: dict[str, float] | None) -> float:
    return max((coverage(stems, h.get("text", ""), idf) for h in hits), default=0.0)


# --------------------------------------------------------------------------- #
# pinning a ratified rule's clause
# --------------------------------------------------------------------------- #
def topic_in_prompt(rule, stems: list[str] | set[str]) -> bool:
    topic = getattr(rule, "topic", None)
    if topic is None and isinstance(rule, dict):
        topic = rule.get("topic")
    tt = tokenize(str(topic or ""))
    return bool(tt) and all(t in set(stems) for t in tt)


def chunk_lookup(backend, cat) -> Callable[[str, Any], list[dict]]:
    """A (doc_id, page) -> chunks function over whatever holds the text: the local
    backend's in-memory chunks when it has them, else the catalog's clauses."""
    chunks_fn = getattr(backend, "_chunks", None)
    if chunks_fn is not None:
        by_page: dict[tuple, list[dict]] = {}
        for c in chunks_fn():
            by_page.setdefault((c.get("doc_id"), c.get("page")), []).append(c)
        return lambda d, p: by_page.get((d, p), [])
    return lambda d, p: [c for c in cat.clauses(d) if c.get("page") == p]


def cited_chunk(rule, lookup: Callable[[str, Any], list[dict]],
                min_overlap: float = MIN_OVERLAP) -> dict | None:
    """The chunk that occupies the rule's cited box, best overlap first."""
    doc_id, page, bbox = _cit(rule)
    best, best_ov = None, 0.0
    for c in lookup(doc_id, page):
        ov = bbox_overlap(bbox, c.get("bbox") or [])
        if ov >= min_overlap and ov > best_ov:
            best, best_ov = c, ov
    return best


def pin_rule_clauses(rules, hits: list[dict], stems: list[str], scope: list[str],
                     lookup: Callable[[str, Any], list[dict]]) -> tuple[list[dict], list[dict]]:
    """Put the cited chunk of every in-scope ratified rule whose topic is in the
    question at the front of `hits`, marked `pinned_by`. Returns (hits, pins) where
    pins = [{rule_id, doc_id, page, overlap}]."""
    pins: list[dict] = []
    front: list[dict] = []
    rest = list(hits)
    for r in rules:
        doc_id, page, bbox = _cit(r)
        if doc_id not in scope or not topic_in_prompt(r, stems):
            continue
        c = cited_chunk(r, lookup)
        if c is None:
            continue
        ov = bbox_overlap(bbox, c.get("bbox") or [])
        rest = [h for h in rest
                if not (h.get("doc_id") == doc_id and h.get("page") == page
                        and bbox_overlap(h.get("bbox") or [], c.get("bbox") or []) >= MIN_OVERLAP)]
        score = max((float(h.get("score") or 0) for h in hits), default=0.0)
        front.append({"doc_id": doc_id, "clause": c.get("clause"), "page": page,
                      "bbox": c.get("bbox"), "score": round(score, 4),
                      "text": (c.get("text") or "")[:400], "kind": c.get("kind") or "text",
                      "pinned_by": getattr(r, "id", None) or (r.get("id") if isinstance(r, dict) else None)})
        pins.append({"rule_id": front[-1]["pinned_by"], "doc_id": doc_id, "page": page,
                     "overlap": round(ov, 3)})
    return front + rest, pins


# --------------------------------------------------------------------------- #
# rule selection for the entitlement path
# --------------------------------------------------------------------------- #
def select_rules(candidates, hits: list[dict], stems: list[str],
                 max_rank: int = ANCHOR_MAX_RANK) -> tuple[list, list[dict], list[str]]:
    """Choose the ratified rules that answer the question from `candidates` (already
    filtered to the unit's documents and to non-currency types).

    A rule is selected when its cited chunk is among the top `max_rank` hits (via
    'evidence') or its topic word is in the question (via 'topic' — covers a retrieval
    miss). When the selected rules span several topics the best-ranked topic wins;
    a tie is returned as `ambiguous` for the caller to ask about."""
    picked: list[tuple[Any, dict]] = []
    for r in candidates:
        a = anchored(r, hits)
        via = None
        if a and a[0] <= max_rank:
            via = "evidence"
        elif topic_in_prompt(r, stems):
            via = "topic"
        if via:
            picked.append((r, {"rule_id": r.id, "via": via,
                               "hit_rank": a[0] if a else None,
                               "overlap": round(a[1], 3) if a else None,
                               "topic": r.topic}))
    topics = sorted({m["topic"] for _, m in picked})
    if len(topics) <= 1:
        return [r for r, _ in picked], [m for _, m in picked], []
    best_rank: dict[str, int] = {}
    for _, m in picked:
        rank = m["hit_rank"] or (max_rank + 1)
        best_rank[m["topic"]] = min(best_rank.get(m["topic"], 10 ** 6), rank)
    low = min(best_rank.values())
    winners = [t for t, rk in best_rank.items() if rk == low]
    if len(winners) > 1:
        return [], [], winners
    keep = winners[0]
    return ([r for r, m in picked if m["topic"] == keep],
            [m for _, m in picked if m["topic"] == keep], [])


# --------------------------------------------------------------------------- #
# lookup rows and stub wording
# --------------------------------------------------------------------------- #
def classification_words(subjects: list[dict]) -> list[set[str]]:
    """The rank words of each named roster row ('Fire Captain' -> {fire, captain}),
    which a published-rate row must contain to be floated for that question."""
    out: list[set[str]] = []
    for s in subjects:
        rank = s.get("rank") or s.get("name") or ""
        toks = set(tokenize(str(rank)))
        if toks and toks not in out:
            out.append(toks)
    return out


def row_matches(hit: dict, word_sets: list[set[str]]) -> bool:
    present = set(tokenize(hit.get("text", "")))
    return any(ws <= present for ws in word_sets)


def compose_quote(hits: list[dict], title_of: Callable[[str], str],
                  lookup: bool = False, max_rows: int = 3) -> str:
    """No-key wording for a policy or lookup answer: 'From <title>, p.<page>: <text>'
    when the clause label is empty, '§<clause>' kept when the ingest found one. A
    lookup quotes up to `max_rows` rows so both the 56-hour and 40-hour rates of a
    classification are visible.

    A bare heading is never the answer: "Designated Holidays are as follows: -",
    "XIIL. Sick Leave", "D. Longevity" were quoted verbatim as the whole reply. Such a
    hit (a handful of tokens, or ending in ':' or '-') is skipped for the next
    substantive hit — or, when the next hit sits on the same page of the same
    document, the heading is kept as a lead-in to that chunk."""
    def one(h: dict) -> str:
        clause = str(h.get("clause") or "").strip()
        head = f"Per §{clause}" if clause_label_ok(clause) else \
            f"From {title_of(h.get('doc_id', ''))}"
        if h.get("page") is not None:
            head += f", p.{h['page']}"
        return f"{head}: {h.get('text', '')}"
    if not lookup:   # a lookup's rows are short by nature ("Step C $52.10"); keep them
        hits = substantive_hits(hits)
    if not hits:
        return ""
    if lookup:
        return "\n".join(one(h) for h in hits[:max_rows])
    return one(hits[0])


# A clause label that is only a number with an optional two-decimal fraction is a
# captured dollar or percent figure ("300.00" from a uniform-allowance table), not a
# section — "Per §300.00" read as a citation to nowhere. Real labels carry letters or
# dotted section numbers ("12.1", "XIII", "D").
_FIGURE_LABEL_RE = re.compile(r"^\d+(\.\d{2})?$")
HEADING_MAX_TOKENS = 6


def clause_label_ok(clause: str) -> bool:
    clause = (clause or "").strip()
    return bool(clause) and not _FIGURE_LABEL_RE.match(clause)


def is_heading(hit: dict) -> bool:
    """A hit that is only a heading: very short, or trailing a ':' or '-' lead-in."""
    text = str(hit.get("text") or "").strip()
    if not text:
        return True
    if text.endswith((":", "-", "–", "—")):
        return True
    return len(text.split()) < HEADING_MAX_TOKENS


def substantive_hits(hits: list[dict]) -> list[dict]:
    """The quotable hits, in rank order: heading-only hits are dropped, except that a
    heading immediately followed (in rank) by a chunk on the same page of the same
    document is merged into that chunk as its lead-in. The chunk's label, page and
    box are kept so the citation still points at the substantive text."""
    out: list[dict] = []
    skip = False
    for i, h in enumerate(hits):
        if skip:
            skip = False
            continue
        if not is_heading(h):
            out.append(h)
            continue
        nxt = hits[i + 1] if i + 1 < len(hits) else None
        if nxt and not is_heading(nxt) and nxt.get("doc_id") == h.get("doc_id") \
                and nxt.get("page") == h.get("page"):
            lead = str(h.get("text") or "").strip()
            out.append(dict(nxt, text=f"{lead} {str(nxt.get('text') or '').strip()}"))
            skip = True
    if not out and hits:
        return list(hits)   # nothing but headings: quote them rather than nothing
    return out
