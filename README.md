# Kenny

Kenny answers HR costing and policy questions straight from union contract PDFs. The math is
deterministic, every number cites the clause it came from, and no rule goes live without a
named human.

The shipped case is the Central Fire District of Santa Cruz County: four MOUs and the master
salary schedule ([`SOURCES.md`](SOURCES.md)). Current state, what is deployed and what is
known wrong: [`STATUS.md`](STATUS.md).

## Try it live

**<https://kenny-production.up.railway.app>** — chat at `/`, ops at `/admin`. The footer shows
the commit it runs (`build <sha>`). Press the three buttons on the home page:

| Question | What you should see |
|---|---|
| Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step) | **$640.80** — $53.40/hr × 1.5 × 8 h, cited to the Local 3535 MOU p.8 |
| How is premium overtime compensated under the Firefighters Local 3535 MOU? | The p.8 clause the $640.80 is built on, quoted and boxed on the page: "premium overtime compensation at the rate of one and one half … times the employee's 'regular rate of pay'" |
| Cost an 8-hour overtime shift for a Fire Marshal (top step) | Refused by name: the Management MOU has no approved rule |

Click any dollar amount to open the audit drawer: the decision trace, the clause boxed on the
PDF page, and which steps (if any) a model performed. *Take the tour* in the header does the
clicks for you.

## Run it locally

```bash
python3.11 -m venv .venv && source .venv/bin/activate   # Python 3.11+; macOS system python3 is too old
pip install -r requirements.txt
python scripts/demo_smoke.py              # re-asks every demo question, exits non-zero on a miss
uvicorn core.app:app --reload            # http://127.0.0.1:8000 (chat) · /admin
pytest -q                                 # optional; a few minutes, never reaches a paid API
```

No key is needed: costing and entitlements are fully deterministic and reproduce the known
answers. To turn the model on, put `ANTHROPIC_API_KEY=sk-...` in `.env`; `KENNY_LLM=off`
keeps it off even with a key. The chat badge says which mode you are in.

The case ships with its catalog and search index baked, so no PDF is parsed on start.

## How trust works

1. **The AI reads; it never computes.** A model, when on, parses intent, drafts rules and
   ranks retrieval. Every dollar figure comes from a deterministic engine over approved rules.
2. **Nothing goes live without a named human.** Drafted rules wait in a review queue beside
   their source clause; approval needs a name and is recorded in the ledger. One shipped
   rule has not been through this yet (`longevity_10yr`, see STATUS.md).
3. **Approval is checked against known answers.** A rule that fires in no known answer
   cannot go live, and no approval may break an answer that already reproduced. Each rule is
   proved at one set of inputs; the known answers are analyst-derived, not payroll.
4. **Every answer is traceable and replayable.** Citations box the exact clause on the page,
   every change lands in a hash-chained ledger, and `GET /chat/replay/{query_id}` recomputes
   any answer from its frozen snapshot.

## Go deeper

- [`STATUS.md`](STATUS.md) — the handoff page: what runs where, what is wrong, what is next.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) ([PDF](ARCHITECTURE.pdf)) — how it is built. ARCHITECTURE §6 is the approval
  gate; ARCHITECTURE §11 is the shipped case: OCR provenance, who wrote the live rules, the
  admin tabs, every demo question and the expected-results table.
- [`PRD.md`](PRD.md) ([PDF](PRD.pdf)) — the product, users and scope (ARCHITECTURE wins where they overlap).
- [`DEPLOY.md`](DEPLOY.md) — putting it behind a shared URL.
- [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) — the demo walkthrough and talking points.

The PDFs were rendered from the Markdown on 2026-10-28 (`scripts/make_prd_pdf.py`); the
Markdown wins if they differ.
