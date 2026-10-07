# STATUS — the handoff page

One page that says what is deployed, what is proven, what is known wrong and what is
next. If you have only this repo, start here. Delete lines from "Known wrong" as fixes
land; the headings stay (tests/test_handoff_docs.py checks them).

## a. Last verified

- **When / what:** 2026-10-07, branch `ken-50-handoff` on top of `demo-2026-10-07` at
  `9129f7d` (the wave-1 merge), by the handoff coding agent (Claude Code, Fable 5.1) in a
  git worktree. Kenny Geiler (owner) had not yet reviewed it when this was written.
- **Re-verify (from the repo root, no key needed):**
  ```bash
  git status --short                                   # must print nothing
  python scripts/demo_smoke.py                         # 11 of 11 demo checks pass, exit 0
  pytest -q -p no:cacheprovider                        # 524 passed, 5 skipped on this branch (section d)
  uvicorn core.app:app --port 8000 &                   # then:
  curl -s http://127.0.0.1:8000/api/case | grep -o '"version":{[^}]*}'   # build <sha>, dirty false
  ```

## b. Names

- **Product:** Kenny.
- **GitHub:** <https://github.com/kennygeiler/kenny-docs> (public; `kennygeiler/kenny` and
  the local remote URL `kenny.git` redirect to it — `git remote -v` still prints `kenny.git`).
- **Local folder:** `~/holly` (the name predates the product; renaming is not recommended,
  the venv has absolute paths). Parallel work runs in `~/holly/.claude/worktrees/`.
- **Live URL:** <https://kenny-production.up.railway.app> (Railway project `kenny`).
- **Linear:** project [Kenny demo build 2026-10-07](https://linear.app/kennydocs/project/kenny-demo-build-2026-10-07-69a96bed42d3)
  (team KennyDocs, key `KEN`); branch names carry the issue id so Linear links them
  (this branch: [KEN-50](https://linear.app/kennydocs/issue/KEN-50)).

## c. What is running where

| Where | Commit | Date | Tree | How it got there |
|---|---|---|---|---|
| Laptop, demo | `demo-2026-10-07` (after the wave-2 merges; footer shows the hash) | 2026-10-07 | clean after each merge | `uvicorn core.app:app` from `~/holly` |
| GitHub `main` | `3b326a6` | pushed 2026-08-14 | n/a | last push before the demo build; the demo branch is **not pushed** as of this writing |
| Production (Railway) | `3b326a6` **plus an uncommitted diff** (`/privacy`, `/terms`, an auth allow-list) | built 2026-08-25 | dirty | `railway up` from the laptop; cannot be rebuilt from GitHub |

Production, further facts (from the 2026-10-07 audit; this build did not contact it):
variables set by name only — `ANTHROPIC_API_KEY` set, `KENNY_BANNER` unset,
`KENNY_LEDGER_KEY` unset (ledger unkeyed), `KENNY_REQUIRE_AUTH` unset (open posture),
`KENNY_BUILD` unset (the code there has no `version` field anyway). `/data` was seeded at
first boot, so the case data on the volume is frozen at the August state; a data fix
reaches it only through the reseed (DEPLOY.md). Its ledger holds the questions people
asked at the public link since August; export it before any redeploy.

## d. What works (and what proves it)

- The six demo questions answer correctly through `/chat`, with the number, document
  and page asserted: $640.80 (L3535 p.8); $656.82 with 12 years (p.12 + p.8); 3
  bereavement shifts (p.21); heat-pump rebate → `out_of_scope`; a regular shift →
  `refused` (only overtime approved); firefighter + Fire Marshal → $640.80 plus a
  not-covered row naming the Management MOU — `scripts/demo_smoke.py`,
  `tests/test_demo_smoke.py`.
- Of eleven known answers, six pass and all five live rules fire in one; the five
  vacation-accrual answers are *pending* until the queued tiers are approved in Review
  (`rules/rules_proposed.json`, agent-drafted; the 6-10, 14-16 and 17+ tiers need a
  `POST /admin/cell_confirm` first) — `/admin/verification`
  (`all_passing: true`, `unverified: []`), `tests/test_gate.py`, `tests/test_goldens_chat.py`.
- Every answer replays from its frozen snapshot with matching hashes —
  `GET /chat/replay/{id}` (`status: match`, 6 checks), `tests/test_replay.py`.
- The approval gate R1–R5 refuses uncovered, wrong and breaking rules; `/admin/ratify`
  needs an approver; `/admin/try_break` reports caught and surviving mutants —
  `tests/test_gate.py`, `tests/test_mutate.py`, `tests/test_approvals.py`.
- Ledger chain verifies; the tamper demo names the altered entry — `tests/test_ledger.py`,
  `tests/test_tamper_demo.py`; `/healthz` reports ledger, library and retrieval.
- OCR honesty: text origin per document, 6 disputed cells on L3535 p.22, Compare deep
  link — `tests/test_cellcheck.py`, `tests/test_text_origin.py`.
- Two skeptic reviews, fresh against the shipped rule and roster hashes, re-verifiable
  with zero spend — `/admin/skeptic`, `tests/test_skeptic*.py`, `scripts/skeptic.py`.
- The model is optional and switchable (`KENNY_LLM=off`), every model call goes through
  one schema-checked chokepoint, and the test suite cannot reach a paid API —
  `tests/test_llm_switch.py`, `tests/test_llm_schema.py`, `tests/test_hermetic.py`.
- The app shows the commit it runs — footer `build <sha>` on both pages, `version` on
  `/api/case` — `tests/test_demo_smoke.py` (K1 section).
- **Test suite:** 492 passed, 5 skipped at `9129f7d`; **524 passed, 5 skipped** on this
  branch (`pytest -q -p no:cacheprovider`, 2026-10-07, ~35 s with the models cached).
  Four of the five skips are environment-gated (OpenSearch cluster, in-band docling OCR,
  rapidocr models, sentence-transformers) and one is a real skip, listed in section e.

## e. Known wrong today

Each line names the DEMO_TICKETS id that fixes it; delete the line when it lands.

- **Owed back-fills on the shipped case** (idempotent scripts; run from `~/holly`, the
  only checkout with the local ledger):
  - `python scripts/backfill_quote_sha.py cases/santacruz` — the live rules' citations
    carry `quote_sha256: ""`, so the evidence-based stale check (A2) cannot bind them;
    until it runs, "Re-read all documents" is unsafe on the shipped case.
  - `python scripts/backfill_provenance.py cases/santacruz --approvals` — the four July
    approvals have no `authoring.ratify` event in the ledger (F2); the script appends
    back-filled events labelled as such.
  - `firefighters_local3535_mou:longevity_10yr` is live with approver
    `analyst (hand-authored 2026-10-07, …)`, a label rather than a person (J1a; the
    agent string it carried before KEN-19 is gone). To ratify it honestly: move it to
    `rules/rules_proposed.json`, approve it in the Review queue with your name (the gate
    runs), and let F2 record the approval.
  - The two skeptic reviews record `code_rev: d84d5f3-dirty` — produced on an
    uncommitted tree (G6). Re-bake on a clean commit with `scripts/skeptic.py` in a Claude
    Code session (zero API spend) so the provenance block names a real commit.
- One deliberately skipped test: `tests/test_showcase_questions.py::
  test_lookup_names_the_classification_it_could_not_find` — the police roster rows it
  used moved to `archive/santacruz_roster_police_sample.csv`; it needs a tmp-case roster
  fixture (B6 follow-up, wave 2).
- Citations carry `doc_sha256: ""` — the source-hash gate is inert on the shipped
  corpus (C7, F8; citations-polish, wave 2). The hourly rate has no citation to its
  salary-schedule row (I14).
- Rule drafting in the app is unbounded (sends most of the contract to the drafter) and
  needs a key (A3/G3, later). Effective-dated rates and a declared rounding policy are
  not implemented (B7, B8).
- The git remote is still `kenny.git`; there is no LICENSE and no SOURCES.md for the
  redistributed district PDFs (K7, K8). Dependencies are unpinned except docling (K6).
- Everything in the plan's "Not tonight" list (DEMO_TICKETS.md, line "Not tonight"):
  A3/G3, A6, B7, B8, C3, C5, C6, C8, C9, D3, D5, D7, D8, E4–E6, F4, F5, F7, G4, G5
  (remainder), G7, G8, I7, I9–I11, J1c, J2–J4, K3, K6–K8, L4–L7.
- The live link runs the August build (section c).

## f. Hazards — do not

- **Do not press "Re-read all documents"** on the shipped case until the quote-sha
  back-fill (section e) has run: the stale check cannot bind the shipped rules and the
  library empties.
- **Do not run `scripts/trace.py`** (it re-ingests and rewrites the catalog) or
  **`scripts/reset_case.py`** (it deletes the local ledger after copying it to the home
  folder; that ledger is the only approval trail for the four original rules) on `~/holly`.
- **`railway up` ships the working tree**, not a commit (section h).
- **Do not `git clean`** in `~/holly`: the ledger, snapshots and `.bak` files are ignored,
  not tracked.
- Never commit `.env`; `.dockerignore` and `.gitignore` both exclude it.

## g. Local-only state (not in git)

In `~/holly` only: `cases/santacruz/ledger.jsonl` (422 events — the July 2026 drafting and
approval trail for the four original rules, plus every local question since);
`cases/santacruz/snapshots/` (frozen answers); four `cases/santacruz/rules/
rules_ratified.json.*.bak` files (17–18 July 2026); `.env` (the API key); 17
`~/holly_backup_*` folders (older copies of the rule library and ledger from
`reset_case.py` — the only other copies of that trail; do not delete). Git worktrees
under `~/holly/.claude/worktrees/` have none of these, which is why the smoke script and
the tests work on a scratch copy.

## h. Deploy policy and the exact commands (none run yet)

Nothing deploys from a dirty tree, and nothing deploys that is not on GitHub. The handoff
chunk wrote these commands and did **not** run them; the lead runs them after the wave-2
merge and a browser pass.

```bash
# 1. Merge and push the demo branch (lead, from ~/holly)
git status --short                                    # must print nothing
git checkout demo-2026-10-07
git merge --no-ff ken-50-handoff                      # and the other wave-2 branches, in the plan's order
pytest -q -p no:cacheprovider && python scripts/demo_smoke.py
git push origin demo-2026-10-07
git tag -a demo-2026-10-07 -m "Demo build 2026-10-07" && git push origin demo-2026-10-07 --tags
# 2. Confirm the running build (laptop)
curl -s http://127.0.0.1:8000/api/case | grep -o '"version":{[^}]*}'   # sha == git rev-parse --short HEAD, dirty false
# 3. Production redeploy — OPTIONAL, only from the pushed commit, after exporting the ledger
curl -s https://kenny-production.up.railway.app/admin/ledger/export > ~/kenny-prod-ledger-$(date +%Y%m%d).jsonl
SHA=$(git rev-parse --short HEAD)
railway variable set KENNY_BUILD=$SHA --skip-deploys
railway variable set KENNY_SEED_FORCE=1 KENNY_SEED_FORCE_CONFIRM=yes --skip-deploys   # the data changed (rules, roster, case.yaml); the old case is archived on the volume
railway up --detach -m "$SHA"
curl -s https://kenny-production.up.railway.app/api/case | grep -o '"version":{[^}]*}'   # must show $SHA
railway variable delete KENNY_SEED_FORCE KENNY_SEED_FORCE_CONFIRM                     # so the next deploy does not reseed again
```

Decision recorded (K1 step 0): the call-screening `/privacy` and `/terms` pages were
removed from this app by the wave-1 chat-ui chunk (I8) and their templates kept under
`archive/call-screening-legal-pages/`. Production still serves them until it is
redeployed; if a Twilio A2P registration points at those URLs, host them with that
project first.

## i. Where things are

- `README.md` — the walkthrough and the trust model as built.
- `ARCHITECTURE.md` — behaviour (tiebreak over the PRD); `PRD.md` — intent.
- `DEPLOY.md` — Railway; `DEPLOYMENT_STRATEGY.md` — the engagement plan (not re-audited;
  DEMO_TICKETS K10).
- `DEMO_TICKETS.md` — the open backlog, with a "state after wave 1" note at the top.
- `archive/backlog-2026-08-14/` — the two closed backlogs with corrected status blocks;
  `archive/fly/` — the abandoned Fly deploy; `archive/call-screening-legal-pages/`;
  `archive/santacruz_roster_police_sample.csv`.
- `scripts/demo_smoke.py` — the demo table; `tests/` — the proofs.

## j. Next

1. Merge the remaining wave-2 chunks (`search-tree`, `vacation-accrual`,
   `tour-and-gate-demo`, `citations-polish`) in the plan's order; full suite and
   `scripts/demo_smoke.py` green after each; browser pass of the demo script.
2. Run the three owed back-fills and the longevity ratification (section e), then push
   (section h).
3. Later milestone, in order of what the demo exposes first: A3/G3 (bounded drafting),
   C7/F8 (arm source hashes), K3 (HTTP acceptance suite + CI), K6 (pinned deps), B7/B8
   (dated rates, rounding policy), K8 (LICENSE + SOURCES.md).

Decisions waiting on the owner: where the SMS legal pages live (section h); whether
production keeps a live `ANTHROPIC_API_KEY` on an open link; the licence (K8); whether
to redeploy production before or after the Later milestone.
