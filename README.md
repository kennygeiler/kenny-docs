# Kenny — Document-Grounded Costing & Policy Engine

**Current state of the repo, what is deployed where, what is known wrong:
[`STATUS.md`](STATUS.md).** Open backlog: [`DEMO_TICKETS.md`](DEMO_TICKETS.md).

**Live link: <https://kenny-production.up.railway.app>** — no sign-in; chat at `/`, ops at
`/admin`. **It runs the August 2026 build, not this one** (see STATUS.md for the redeploy
commands). The 2026-10-07 demo runs from a laptop on the `demo-2026-10-07` branch; the
footer of every page shows the commit it is running (`build <sha>`).

Kenny answers HR costing and policy questions directly from contract PDFs — with
**deterministic math**, **clause-level bounding-box citations**, a **human approval
gate**, and a **tamper-evident audit ledger**. A language model, when one is on, only
reads language and routes questions; it never computes the number.

> **The job it does:** *"Hand me a number I can defend, while the moment is still alive."*
> Trust is verified once, up front, against known answers. Every scenario computed on
> those verified rules is then instant *and* defensible.

This build ships a single corpus — the **Central Fire District of Santa Cruz County**:
four bargaining-unit MOUs (with side letters) plus the master salary schedule, all
published by the district. The four MOU PDFs in this repo are the district's scans with
an OCR text layer added (OCRmyPDF 17.8.0 with Tesseract 5.5.2, 17 July 2026), not the
district's original bytes; the salary schedule is born-digital. Every citation chip says
which (`OCR'd scan` or `text layer`), and the numeric cells of the vacation table on
Local 3535 p.22 were re-read by a second OCR engine: 6 cells disagree and are shown in
amber (Admin → Documents → *Showcase: p.22*).

**Go deeper:** [`PRD.md`](PRD.md) ([PDF](PRD.pdf)) — the product, users, scope, and trust
model, for a product audience. · [`ARCHITECTURE.md`](ARCHITECTURE.md)
([PDF](ARCHITECTURE.pdf)) — component map, data contracts, flows, security, and the
onboarding playbook. · [`DEPLOY.md`](DEPLOY.md) — putting it behind a shared URL.

The PRD owns *intent*; ARCHITECTURE owns *behaviour*. Where they overlap, ARCHITECTURE is
the tiebreak. The two PDFs were rendered from earlier revisions of those files and may
lag the Markdown.

---

## The trust model — why this is different

Four rules, enforced in the architecture rather than promised in prose:

1. **The AI reads; it never computes.** Intent parsing, rule drafting, tagging, retrieval
   ranking and the skeptic review are model touchpoints. Every dollar figure is produced
   by a deterministic engine from human-approved rules. `KENNY_LLM=off` disables every
   touchpoint even when a key is present, and the chat badge says which mode you are in.
2. **Nothing goes live without a named human.** Drafted rules sit in a review queue until
   a person approves each one against its highlighted source clause; `/admin/ratify`
   refuses an approval that names no approver, and the approval is a ledger event.
3. **Approval is checked against the known answers — exactly this, and no more.** The
   gate (`_gate_verdict` in `core/app.py`) judges the live library merged with the
   selection, before and after, all-or-nothing: (R1) no known answer may turn from pass
   or pending to *fail*; (R2) a known answer that reproduced may not stop being answered;
   (R3) a rule drafted for a scenario must make that scenario pass; (R4) **every selected
   rule must fire in a passing known answer** — a rule that fires in no known answer
   cannot go live; (R5) a live rule outside the selection that was proven before must
   still be proven. What it does not check: whether the rule reads the clause correctly
   beyond that one known answer's inputs — each rule is proved at one set of inputs
   only, and the twenty-two shipped known answers are analyst-derived from the documents, not
   payroll. *Try to break it* (Rule library) mutates a rule in memory and shows which
   deliberate errors the known answers catch and which survive.
4. **Every answer is traceable and replayable.** Each figure clicks through to the exact
   clause boxed on the rendered PDF page; every state change lands in a hash-chained
   audit ledger; every answer is frozen as a snapshot (inputs, rule text, hashes) that
   `GET /chat/replay/{query_id}` recomputes and compares.

**Who wrote the live rules.** The in-app drafter ("Draft the rules for this scenario")
produced none of them. In the author's local ledger it was run five times on this corpus
(17–18 July 2026), drafting 43, 59, 7, 22 and 6 candidate rules; four of the five runs
failed their known-answer check and nothing from any run was approved. The four original
live rules were written by the reviewing analyst against the clause text and approved
through the same gate on 2026-07-18. The fifth, `firefighters_local3535_mou:longevity_10yr`,
was hand-authored on 2026-10-07 in a Claude Code session against the catalog's p.12 text
(reviewed against the p.38 column), passes its known answer ($656.82) and the gate, and
**has not yet been ratified by a human** through the Review queue — its `approver` field is the analyst
label `analyst (hand-authored 2026-10-07, …)`, not a person's name. Two baked **skeptic reviews** (an agent that read each overtime
rule's clause and the pages around it, listed what the rule ignores and ran its
counter-examples through the engine) ship under `cases/santacruz/reviews/`; they were
produced in a Claude Code session with zero API spend, and `scripts/skeptic.py` re-verifies
every quote and replays every engine call.

---

## Quick start

```bash
python3.11 -m venv .venv && source .venv/bin/activate   # 3.11 to 3.14; macOS system python3 (3.9) is too old
pip install -r requirements.txt          # docling is heavy; see "offline" below
pytest -q                                 # about 500 tests, a few minutes; cannot reach a paid API (tests/conftest.py)
python scripts/demo_smoke.py              # re-asks every demo question, prints a table, exits non-zero on a miss
uvicorn core.app:app --reload            # http://127.0.0.1:8000 (chat) · /admin (ops)
```

**Activate Claude (optional):** put your key in `.env` (`ANTHROPIC_API_KEY=sk-...`) and
restart; the chat badge shows `LLM: Claude`. **`KENNY_LLM=off`** runs with every model
touchpoint disabled even when `.env` holds a key (badge: `LLM: off (deterministic)`); with
no key at all the badge reads `LLM: deterministic fallback`. Without a model, costing and
entitlement are fully deterministic and reproduce the known answers; policy and lookup
answers quote the retrieved clause (scoped to the named contract or the classification's
bargaining unit, and refused when the question is not about these documents); rule
drafting needs Claude. **Money math is deterministic either way.**

**Offline / lightweight:** docling pulls torch + models on first parse — but the Santa
Cruz case ships with its catalog and search index already baked, so the app answers
without ever parsing. **Do not press "Re-read all documents" on the shipped case** until
`scripts/backfill_quote_sha.py` has been run on the library (STATUS.md, "owed
back-fills"): a re-read checks each live rule's citation by page, box and quote hash, and
the shipped rules do not carry the hash yet.

---

## Demo walkthrough

**The fast way:** press **"Take the tour"** on the chat page (header, next to the LLM
badge). A guided walkthrough performs the clicks for you — it types the costing
question, opens the audit drawer, points at the citation chips, then hands off to the
admin surfaces — with a Next button to advance and Esc to exit.

The manual path below covers the same ground. The demo has two surfaces: **Chat** (`/`)
is where you ask and cost; **Admin** (`/admin`) is where documents become trusted rules.

### Part 1 — Admin: turn documents into trusted rules

Open `/admin`. The tabs, in order:

| Tab | What it does | What to expect on the shipped case |
|---|---|---|
| **1 · Documents** | The contract library: each document parsed by docling for its text, its tables and the position of every clause, then indexed. X-ray and Compare show every extracted clause boxed on its page. | Five documents (four MOUs + the master salary schedule), 1,861 clauses, each with its text origin. *Showcase: p.22* opens Compare on the Local 3535 vacation table with the 6 cells the OCR misread in amber beside the page image. |
| **2 · Verification** | The trust anchor: each card is a known answer with expected and actual, the rules that fired, and its analyst-derived source. **"Draft the rules for this scenario"** appears only on a scenario that is not passing. | Six green cards ($640.80; $656.82 with 12 years' longevity; 3 bereavement shifts for a Firefighter/Paramedic and for a Battalion Chief; 40 hours for a Division Chief; $716.16 for an Administrative Analyst). No Draft button, because nothing is pending. |
| **3 · Review queue** | The human gate. Each drafted rule is shown beside its highlighted source clause. Approval needs the approver's name and is **refused** when a selected rule fires in no known answer, when a known answer that reproduced stops reproducing, or when one comes out wrong (trust rule 3). | Empty on the shipped case. |
| **Rule library** | The five live rules (each naming its clause and approver), the gaps Kenny declined to model, **Try to break it** per rule, and the two **Skeptic reviews**. | `longevity_10yr`'s approver is an analyst label, not a person's name (see above). |
| **Audit** | The hash-chained ledger: every question, approval and ingest as an event with decision-relevant payloads, `verify()` status, head hash, export, and a **tamper demo** that alters one event in a scratch copy and names the entry that fails. | The ledger file is not in git, so a fresh clone starts empty; the July 2026 authoring trail for the four original rules exists only in the author's local ledger. Without `KENNY_LEDGER_KEY` the chain is unkeyed SHA-256: it detects edits, not a full rewrite by someone with disk access. |

### Part 2 — Chat: ask and cost, with receipts

Open `/`. The three home-page buttons show the three behaviours, and every answer opens
with a **"Read as:"** line stating how the question was interpreted (hours, pay type,
classifications, date):

- **A number** — *"Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top
  step)"* → **$640.80** (1.5× the $53.40/hr top-step rate × 8 hours), cited to the Local
  3535 MOU p.8. Add *"with 12 years of service"* → **$656.82**, with the longevity clause
  (p.12) as a second boxed citation.
- **A quoted clause** — *"How is premium overtime compensated under the Firefighters Local
  3535 MOU?"* → the p.8 clause the $640.80 answer is built on, quoted and boxed.
- **A refusal** — *"Cost an 8-hour overtime shift for a Fire Marshal (top step)"* → refused
  by name: the Management MOU has no approved rule, with a link to the Verification tab.

Also in the smoke table (`scripts/demo_smoke.py`): *"How much bereavement leave does a
firefighter get?"* → **3 shifts** from the ratified rule, Article XIV boxed on p.21; *"Cost
an 8-hour regular shift for a Firefighter/Paramedic (56 hr, top step)"* → refused, because
only an overtime rule is approved for that unit (the nearest clause is shown as text, not a
number); a firefighter **and** a Fire Marshal in one question → $640.80 for the
firefighter and a "not covered" row for the Fire Marshal; *"What is the heat pump rebate
for a 3 ton system?"* → "not covered by the 5 documents I have". A costing question with
no pay-type cue asks which pay type you mean instead of guessing.

**What to expect from a costing answer:**

1. A **single deterministic figure**, with the sum written out ($53.40/hr × 1.5 × 8 h).
2. The answer is **routed to the governing document by bargaining unit**: the 1.5×
   multiplier comes from the Local 3535 MOU and is cited to p.8. The $53.40 rate comes
   from the classification table (`cases/santacruz/data/roster.csv`), transcribed from
   the Master Salary Schedule; the rate's own citation to its schedule row is wave-2 work
   (DEMO_TICKETS I14).
3. **Click any dollar amount** → the audit drawer opens with the decision trace, the
   citations, the source clause boxed on the rendered PDF page, and an honest panel
   saying which steps a model performed (none, when the model is off).

### Expected results

The engine, rule DSL, governance and ledger in `core/` are case-agnostic: `CASE=<dir>`
points the app at another bundle. The demo UI is still Santa Cruz-specific (example
prompts in `chat.html`, tour targets in `tour.js`, department cue words in `llm.py`). The
twenty-two known answers are analyst-derived from the documents, not from payroll, and are
asserted by `pytest` through the gate and through `/chat` (`tests/test_demo_smoke.py`);
each proves its rule at one set of inputs only:

| Scenario | Prompt | Expected | Source |
|---|---|---|---|
| Overtime | 8-hour overtime shift, Firefighter/Paramedic (56 hr, top step) | **$640.80** | Local 3535 MOU p.8 (1.5×) × $53.40/hr (Master Salary Schedule) |
| Overtime + longevity | the same, with 12 years of service | **$656.82** | Local 3535 MOU p.12 (2.5% longevity) into the 1.5× rate |
| Bereavement | Firefighter/Paramedic | **3 shifts** | Local 3535 MOU Article XIV (p.21) |
| Bereavement | Battalion Chief (56 hr top step) | **3 shifts** | Local 3535 MOU Article XIV (p.21); Battalion Chief is in the Local 3535 unit (Recognition p.3) |
| Bereavement | Division Chief | **40 hours** | Chief Officers MOU p.14 |
| Overtime | 8-hour overtime shift, Administrative Analyst (top step) | **$716.16** | Admin Group MOU p.6 (1.5×) |

Management has no known answer and no rule, so a Management costing question (a Fire
Marshal, for instance) is refused with a reason.

---

## Layout

- **`core/`** — the engine and surfaces: ingest, `cellcheck` (second-engine cell
  verification), `index`/`retriever`/`rulematch` (retrieval and scoping), `ruledsl` +
  `engine` + `costing` + `governance` (rules and per-unit math), `audit` + `ledger` +
  `approvals` + `tamper_demo` (snapshots, chain, replay), `llm` (every model call through
  one schema-checked chokepoint) + `skeptic`, `version` (the build hash), and the
  chat/admin templates.
- **`cases/santacruz/`** — the bundle: `sources/` PDFs, `data/roster.csv`, `rules/`,
  `reviews/` (skeptic), `cell_checks.json`, `catalog.json`, `search_index.jsonl`,
  `case.yaml` (twenty-two known answers: six money/leave scenarios, five vacation tiers, and
  eleven E10 boundary answers at the longevity and vacation-tier edges), `taxonomy.yaml`, `prompt/`.
- **`scripts/`** — `demo_smoke.py` (the demo table), `skeptic.py` (zero-spend bake and
  re-verification), `verify_cells.py`, the back-fills (`backfill_quote_sha.py`,
  `backfill_provenance.py`, `backfill_text_origin.py`), `prepare_deploy.py`, `replay.py`.
  `trace.py` and `reset_case.py` are hazardous on the shipped case — see STATUS.md.
- **`tests/`** — acceptance proofs; `tests/conftest.py` makes the suite hermetic (no
  `.env`, a real model client cannot be constructed).
- **`archive/`** — closed backlogs with corrected status blocks, the Fly assets, the
  call-screening legal pages removed from this app, the police roster sample.

Read ARCHITECTURE §2 (the AI boundary), §6 (verification and the approval gate), §7 (error
handling) and §10 (onboarding checklist) before running against real contracts. Current
status and open work: [`STATUS.md`](STATUS.md).
