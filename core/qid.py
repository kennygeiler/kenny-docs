"""Server-issued query ids (DEMO_TICKETS.md A1).

A query id names a frozen snapshot file and threads a query's ledger events together,
so it must never be chosen by the client: a client-picked id could point the snapshot
writer at another file ('../rules/rules_ratified') or re-open a frozen record. Every
POST /chat mints its own id here. A client may still SEND an id — the chat page re-sends
the previous answer's id on "which department?" / "which document?" follow-ups — but it
is only ever a reference: when it is well-formed and the ledger already knows it, the
new query records `continues: <that id>` and the audit trail follows the link back.
"""
from __future__ import annotations

import re
import uuid

QID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_HOPS = 5  # how far /chat/audit follows `continues` links back


def mint() -> str:
    return uuid.uuid4().hex[:12]


def is_qid(value) -> bool:
    return isinstance(value, str) and bool(QID_RE.match(value))


def continuation(led, ref) -> str | None:
    """The id a follow-up continues, or None. Only an id this ledger has already
    recorded counts — an unknown or malformed value is ignored, never trusted."""
    if not is_qid(ref):
        return None
    try:
        return ref if led.for_query(ref) else None
    except Exception:
        return None


def parent_of(events: list[dict]) -> str | None:
    """The `continues` reference recorded on a query's own chat.prompt event."""
    for ev in events:
        if ev.get("type") == "chat.prompt":
            ref = (ev.get("payload") or {}).get("continues")
            return ref if is_qid(ref) else None
    return None


def trail_with_parents(led, query_id: str, max_hops: int = MAX_HOPS) -> tuple[list[dict], list[str]]:
    """Events for `query_id` with its ancestors' events in front (oldest first), plus the
    list of ancestor ids nearest-first. Bounded by `max_hops` so a forged cycle cannot
    make the endpoint spin."""
    own = led.for_query(query_id)
    chain: list[str] = []
    parents: list[list[dict]] = []
    seen = {query_id}
    ref = parent_of(own)
    while ref and ref not in seen and len(chain) < max_hops:
        seen.add(ref)
        evs = led.for_query(ref)
        if not evs:
            break
        chain.append(ref)
        parents.append(evs)
        ref = parent_of(evs)
    ordered: list[dict] = []
    for evs in reversed(parents):
        ordered.extend(evs)
    ordered.extend(own)
    return ordered, chain
