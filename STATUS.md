# STATUS — the handoff page

One page that says what is deployed, what is proven, what is known wrong and what is
next. If you have only this repo, start here. Delete lines from "Known wrong" as fixes
land; the headings stay (tests/test_handoff_docs.py checks them).

## a. Last verified

- **When / what:** 2026-10-28, a repo audit run as parallel agents in git worktrees
  (branches `audit/docs`, `audit/sec`, `audit/money`, `audit/ui`), all based on `3233baf`
  (the head of `demo-2026-10-07` and of GitHub `main`). This page was rewritten on
  `audit/docs`. The owner had not reviewed the audit branches when this was written.
- **Suite at `3233baf`:** 660 passed, 1 failed, 3 skipped. The failure is in
  `tests/test_llm_schema.py` and is fixed on an audit branch; the skips are
  environment-gated (OpenSearch cluster, in-band docling OCR, model downloads).
- **Re-verify (from the repo root, no key needed):**
  ```bash
  git status --short                                   # must print nothing
  python scripts/demo_smoke.py                         # every demo check passes, exit 0
  pytest -q -p no:cacheprovider                        # all pass but the environment-gated skips
  uvicorn core.app:app --port 8000 &                   # then:
  curl -s http://127.0.0.1:8000/api/case | grep -o '"version":{[^}]*}'   # build <sha>, dirty false
  ```

## b. Names

- **Product:** Kenny.
- **GitHub:** <https://github.com/kennygeiler/kenny-docs> (public; `kennygeiler/kenny`
  redirects to it). The local remote should point at `kenny-docs.git`; check with
  `git remote -v` and fix with
  `git remote set-url origin https://github.com/kennygeiler/kenny-docs.git`.
- **Local folder:** `~/holly` (the name predates the product; renaming is not recommended,
  the venv has absolute paths). Parallel work runs in `~/holly/.claude/worktrees/`. The
  `.claude/launch.json` config is `kenny-santacruz` (port 8401).
- **Live URL:** <https://kenny-production.up.railway.app> (Railway project `kenny`).
- **Linear (the live backlog):** project [Kenny demo build 2026-10-07](https://linear.app/kennydocs/project/kenny-demo-build-2026-10-07-69a96bed42d3)
  (team KennyDocs, key `KEN`); branch names carry the issue id so Linear links them.
  The 2026-10-07 ticket file is archived (section i); code comments that cite
  `DEMO_TICKETS.md <id>` resolve to headings there.

## c. What is running where

| Where | Commit | Date | How it got there |
|---|---|---|---|
| GitHub `main` | `e0dac03` | 2026-10-28 | PR #19 (audit fixes) and PR #20 (admin reads public) merged |
| Production (Railway) | `e0dac03` | deployed 2026-10-28 ~17:25 ET | `railway up <clean worktree of main> --path-as-root`; footer and `/api/case` show `e0dac03`, `/healthz` ok |
| `demo-2026-10-07` | `3233baf` | 2026-10-07 | the old demo branch (before it, `main` sat at `3b326a6`, the August build); `~/holly` is still checked out on it |

Production settings (by name; values are in Railway and, for the two secrets, in the
owner's `~/.kenny-prod-secrets`, mode 600): **admin-only posture** — `KENNY_REQUIRE_AUTH=1`,
`KENNY_ADMIN_PASSWORD` set, no viewer password, so chat and every admin page are public to
read and every admin change needs the password (checked 2026-10-28: `/admin` 200,
`POST /admin/ratify` and `/admin/upload` 401 without it). `KENNY_LEDGER_KEY` set (ledger
HMAC-keyed). `KENNY_LLM=off` (badge "LLM: off (deterministic)"; `ANTHROPIC_API_KEY` is still
set in Railway but unused). `KENNY_CHAT_RATE_LIMIT=1000`. `KENNY_SEED_FORCE=0` and
`KENNY_SEED_FORCE_CONFIRM=no` (the CLI's `variable delete` has no `--skip-deploys`, so they
were set off instead of deleted).

The volume was reseeded on 2026-10-28 so the keyed ledger starts clean; the previous case
and its unkeyed ledger are archived on the volume at `/data/cases/santacruz.pre-reseed.*`,
and the owner has an export of that ledger (470 events) at `~/kenny-prod-ledger-20261028-*.jsonl`.

## d. What works (and what proves it)

- The demo questions answer correctly through `/chat`, with the number, document and page
  asserted: $640.80 (L3535 p.8); $656.82 with 12 years (p.12 + p.8); 3 bereavement shifts
  (p.21); heat-pump rebate → `out_of_scope`; a regular shift → `refused` (only overtime
  approved); firefighter + Fire Marshal → $640.80 plus a not-covered row naming the
  Management MOU — `scripts/demo_smoke.py`, `tests/test_demo_smoke.py`. The three home-page
  buttons — `tests/test_examples.py`.
- Of twenty-two known answers, eight pass (six scenarios plus the longevity edges at 9 and
  10 years) and all five live rules fire in one; the fourteen vacation-accrual answers
  (five tiers, nine tier edges) are *pending* until the queued tiers are approved in Review
  (`rules/rules_proposed.json`, agent-drafted; three tiers need a `POST /admin/cell_confirm`
  first) — `/admin/verification`, `tests/test_gate.py`, `tests/test_goldens_chat.py`.
- Every answer replays from its frozen snapshot with matching hashes —
  `GET /chat/replay/{id}`, `tests/test_replay.py`.
- The approval gate R1–R5 refuses uncovered, wrong and breaking rules; `/admin/ratify`
  needs an approver; `/admin/try_break` reports caught and surviving mutants —
  `tests/test_gate.py`, `tests/test_mutate.py`, `tests/test_approvals.py`.
- Ledger chain verifies; the tamper demo names the altered entry — `tests/test_ledger.py`,
  `tests/test_tamper_demo.py`; `/healthz` reports ledger, library and retrieval.
- The live rules are bound to the text they cite: `scripts/backfill_quote_sha.py` ran on
  the shipped library (`d2fd14b`), and source-PDF hashes are armed (`bfec348`, C7) —
  `tests/test_integration_leftovers.py`, `tests/test_provenance_shipped.py`. The
  `backfill_provenance.py --approvals` script is proven additive and idempotent on a copy.
- OCR honesty: text origin per document, 6 disputed cells on L3535 p.22, Compare deep
  link — `tests/test_cellcheck.py`, `tests/test_text_origin.py`. Provenance and hashes of
  the five PDFs: [`SOURCES.md`](SOURCES.md).
- Two skeptic reviews, re-verified at clean commit `f358af6` with zero spend
  (`8bbc9ea`) — `/admin/skeptic`, `tests/test_skeptic*.py`, `scripts/skeptic.py`.
- The model is optional and switchable (`KENNY_LLM=off`), every model call goes through
  one schema-checked chokepoint, and the suite cannot reach a paid API —
  `tests/test_llm_switch.py`, `tests/test_llm_schema.py`, `tests/test_hermetic.py`.
- The app shows the commit it runs — footer `build <sha>`, `version` on `/api/case`.
- `tests/test_showcase_questions.py::test_lookup_names_the_classification_it_could_not_find`
  runs again on a tmp-case roster (`d2fd14b`).

## e. Known wrong today

Delete a line when it lands. Items marked *owner* need Kenny.

- `firefighters_local3535_mou:longevity_10yr` is live with approver
  `analyst (hand-authored 2026-10-07, …)`, a label rather than a person. To ratify it
  honestly: move it to `rules/rules_proposed.json`, approve it in the Review queue with
  your name (the gate runs), and let the approval land in the ledger.
- Not verified: whether `python scripts/backfill_provenance.py cases/santacruz --approvals`
  was run on the local ledger in `~/holly` (the four July approvals need a back-filled
  `authoring.ratify` event). The ledger is local-only; check there.
- The two skeptic reviews record `code_rev: d84d5f3-dirty` for the session that wrote them
  (an uncommitted tree); they carry a clean re-verification at `f358af6`. A fresh bake on a
  clean commit is optional.
- I14 done (M1.4, branch m1/rates): every roster rate cites its salary-schedule row.
- From the 2026-10-07 backlog, not re-checked in this audit: in-app rule drafting is
  unbounded and needs a key (A3/G3); effective-dated rates and a declared rounding policy are missing (B7, B8);
  dependencies are unpinned except docling (K6).
- No LICENSE (K8; *owner*). SOURCES.md now covers the redistributed district PDFs.

## f. Hazards — do not

- **"Re-read all documents"** on the shipped case: the live rules now carry quote hashes,
  so the stale check can bind them, but a re-read of the shipped case has not been
  exercised since. Try it on a copy (`CASE=<copy>`) first.
- **Do not run `scripts/trace.py`** (it re-ingests and rewrites the catalog) or
  **`scripts/reset_case.py`** (it deletes the local ledger after copying it to the home
  folder; that ledger is the only approval trail for the four original rules) on `~/holly`.
- **`railway up` ships the working tree**, not a commit (section h).
- **Do not `git clean`** in `~/holly`: the ledger, snapshots and `.bak` files are ignored,
  not tracked.
- Never commit `.env`; `.dockerignore` and `.gitignore` both exclude it.

## g. Local-only state (not in git)

In `~/holly` only: `cases/santacruz/ledger.jsonl` (the July 2026 drafting and approval
trail for the four original rules, plus every local question since);
`cases/santacruz/snapshots/` (frozen answers); four `cases/santacruz/rules/
rules_ratified.json.*.bak` files (17–18 July 2026); `.env` (the API key); 17
`~/holly_backup_*` folders (older copies of the rule library and ledger from
`reset_case.py` — the only other copies of that trail; do not delete). Git worktrees
under `~/holly/.claude/worktrees/` have none of these, which is why the smoke script and
the tests work on a scratch copy.

## h. Deploy policy and the exact commands

Nothing deploys from a dirty tree, and nothing deploys that is not on GitHub. The last
deploy ran on 2026-10-28 (`e0dac03`, section c) and moved production to the admin-only
posture. The commands below are the ones that deploy used; for the next deploy skip the
reseed flags (the ledger is keyed now) and keep the same secrets.

**Deploy from a clean worktree of the pushed commit, and name it:** `railway up` uploads
the linked project's folder (`~/holly`), not the current directory. On 2026-10-28 a plain
`railway up` from a worktree shipped `~/holly`'s old checkout, which crashed on the new
settings. Use `railway up <worktree path> --path-as-root --detach -m "$SHA"`.

```bash
# 1. Merge and push (from ~/holly)
git status --short                                    # must print nothing
pytest -q -p no:cacheprovider && python scripts/demo_smoke.py
git push origin <branch>
# 2. Export the production ledger before touching anything
curl -s https://kenny-production.up.railway.app/admin/ledger/export > ~/kenny-prod-ledger-$(date +%Y%m%d).jsonl
# 3. Set the posture (DEPLOY.md), then deploy the pushed commit
SHA=$(git rev-parse --short HEAD)
railway variable set KENNY_BUILD=$SHA KENNY_REQUIRE_AUTH=1 KENNY_ADMIN_PASSWORD='<owner picks>' \
  KENNY_LEDGER_KEY="$(openssl rand -base64 32)" KENNY_LLM=off \
  KENNY_SEED_FORCE=1 KENNY_SEED_FORCE_CONFIRM=yes --skip-deploys   # reseed: the volume's ledger is unkeyed
railway up "$PWD" --path-as-root --detach -m "$SHA"   # from a clean worktree of main
# 4. Check
curl -s https://kenny-production.up.railway.app/api/case | grep -o '"version":{[^}]*}'   # must show $SHA
curl -s -o /dev/null -w '%{http_code}\n' https://kenny-production.up.railway.app/admin   # must not be 200 without the password
curl -sf https://kenny-production.up.railway.app/healthz                                 # ledger ok
railway variable delete KENNY_SEED_FORCE KENNY_SEED_FORCE_CONFIRM                       # so the next deploy keeps the volume
```

Keep the `KENNY_LEDGER_KEY` value somewhere safe: the chain cannot be re-verified without
it. The reseed is required this time: production's ledger was written without a key, and a
keyed app fails `verify()` on it (`/healthz` 503). The reseed archives the old case and
ledger on the volume; the export in step 2 is the off-volume copy.

Decision recorded (2026-10-07): the call-screening `/privacy` and `/terms` pages were
removed from this app and their templates kept under `archive/call-screening-legal-pages/`.

## i. Where things are

- `README.md` — what it is, the live link, running locally, the trust rules.
- `ARCHITECTURE.md` — behaviour (tiebreak over the PRD); §11 is the shipped case (OCR
  provenance, who wrote the live rules, admin tabs, demo questions, expected results).
  `PRD.md` — intent. `PRD.pdf` and `ARCHITECTURE.pdf` were re-rendered from the Markdown on
  2026-10-28 with `scripts/make_prd_pdf.py` (it writes to `~/Kenny_*.pdf`; copy them in).
  Re-render after any edit to either file.
- `SOURCES.md` — the five district PDFs: titles, provenance, SHA-256.
- `DEPLOY.md` — Railway and the auth posture; `DEPLOYMENT_STRATEGY.md` — the engagement
  plan (not re-audited). `DEMO_SCRIPT.md` — the demo walkthrough.
- `archive/backlog-2026-10-07/DEMO_TICKETS.md` — the 2026-10-07 ticket file (119 tickets,
  ticket ids cited in code comments). The live backlog is Linear (section b).
- `archive/backlog-2026-08-14/` — the two August backlogs with corrected status blocks;
  `archive/fly/` — the abandoned Fly deploy; `archive/call-screening-legal-pages/`;
  `archive/santacruz_roster_police_sample.csv`.
- `scripts/demo_smoke.py` — the demo table; `tests/` — the proofs.

## j. Next

1. Owner: ratify `longevity_10yr` in the Review queue with your name (section e).
2. The plan is [`ROADMAP.md`](ROADMAP.md): the core is M1 (full Local 3535 rulebook), M5
   (accuracy score) and M4 (check against real published pay), tracked as milestones in
   Linear project P-KEN-9 (the workspace is at the free plan's issue limit).
3. Later, from the archived backlog via Linear: A3/G3 (bounded drafting), K3 (HTTP acceptance
   suite + CI), K6 (pinned deps), B7/B8 (dated rates, rounding policy).

Decisions waiting on the owner: whether production should turn the model back on
(`KENNY_LLM` unset; the key is still in Railway) or drop the key; **a licence for the repo** (none
chosen; no LICENSE file exists).
