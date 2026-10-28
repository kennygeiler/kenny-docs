# Archived: the 2026-10-07 demo backlog

`DEMO_TICKETS.md` is the ticket file written for the 2026-10-07 demo build: a code audit,
119 tickets, and the wave-1/wave-2 plan that built it. It sat at the repo root until
2026-10-28, when it was moved here (it was about 600 KB, most of the repo's doc text). The
body is unchanged apart from its links; `git log --follow` shows its history.

The live backlog is Linear (STATUS.md section b). Code and test comments that cite
`DEMO_TICKETS.md <id>` or `DEMO_TICKETS <id>` (for example `DEMO_TICKETS.md A2`) refer to
the `### <id>.` headings in this file; `tests/test_handoff_docs.py` checks they resolve.
The ticket bodies were written before wave 1 merged, so their file:line references are
stale; re-locate by symbol. The current state is always [`/STATUS.md`](../../STATUS.md).
