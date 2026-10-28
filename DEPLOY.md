# Deploying Kenny to a shared link (Railway)

**What this is for.** Putting the prototype behind a URL so people can try it. It is
**not** a production posture — see "What this deploy is not" below, and say so out loud
when you share the link.

**Default posture: admin-only.** Chat and every admin page are public to read, so a
visitor can take the tour and see verification, the review queue, the rule library and the
ledger. Every admin action that changes something (approve, upload, re-read, draft, confirm
a cell, load the demo draft) needs the admin password. "Try to break it" stays public: it
changes nothing on disk. The Dockerfile sets `KENNY_REQUIRE_AUTH=1`, so an image deployed
without the secrets below refuses to boot rather than coming up open.

| Variable | Required | What it does |
|---|---|---|
| `KENNY_REQUIRE_AUTH=1` | default in the Dockerfile | Turns the auth layer on. Set `0` only for a deliberately open demo. |
| `KENNY_ADMIN_PASSWORD` | yes | Password for `/admin/*`. |
| `KENNY_LEDGER_KEY` | yes | HMAC key for the audit ledger. Generate with `openssl rand -base64 32`; keep it, the chain cannot be re-verified without it. |
| `KENNY_VIEWER_PASSWORD` | no | Also puts chat behind a password. Unset = public chat. Must differ from the admin password; set alone (no admin password) the app refuses to start. |
| `KENNY_LLM=off` | recommended for a public link | Disables every model touchpoint even when `ANTHROPIC_API_KEY` is set, so a public chat cannot spend the key. |

```bash
railway variable set KENNY_REQUIRE_AUTH=1 KENNY_ADMIN_PASSWORD='<choose one>' \
  KENNY_LEDGER_KEY="$(openssl rand -base64 32)" KENNY_LLM=off --skip-deploys
```

**An existing unkeyed ledger.** Adding `KENNY_LEDGER_KEY` to a deploy whose ledger was
written without one makes `verify()` fail on the first unkeyed event, so `/healthz`
returns 503. Export the ledger first (`GET /admin/ledger/export`), then redeploy once with
`KENNY_SEED_FORCE=1 KENNY_SEED_FORCE_CONFIRM=yes` (archives the old case and ledger on the
volume and starts a keyed one), then delete those two variables. Production today is in
this state.

Protections that stay on in every posture:

- **Chat rate limit** (default 20/min per client) — an open `/chat` with a model on is the
  operator's Anthropic budget.
- **Admin rate limit** on `/admin/draft_scenario`, `/admin/ingest` and `/admin/upload`
  (`KENNY_ADMIN_RATE_LIMIT`, default 10/min per client). Clients are told apart by the first
  `X-Forwarded-For` hop; the entrypoint runs uvicorn with `--proxy-headers`.
- **The app runs end to end with no key.** Costing and entitlements are deterministic. Add
  `ANTHROPIC_API_KEY` (and drop `KENNY_LLM=off`) only once you accept that anyone who can
  reach chat can spend it, rate-limited.

With `KENNY_REQUIRE_AUTH=0` (open) anyone with the URL can ratify rules, upload or replace
corpus PDFs and read the ledger. Fine for a throwaway demo; not fine once real decisions
live on the volume.

## What is deployed today

Production runs commit `2802ad9` from `demo-2026-10-07`, deployed 2026-10-07 with `railway
up` (footer and `/api/case` show `2802ad9`, checked 2026-10-28). It predates the admin-only
mode: it runs **open** (`KENNY_REQUIRE_AUTH` unset) with a live `ANTHROPIC_API_KEY` and the
model on. Moving it to the posture above needs the variables set and a redeploy; the steps
are in `STATUS.md` section h.

## Deploy (Railway)

```bash
railway init -n kenny          # once: create the project (or `railway link`)
railway volume add -m /data    # persistent case tree: ledger, catalog, ratified rules
git status --short             # must print nothing: `railway up` uploads the WORKING TREE, not a commit
SHA=$(git rev-parse --short HEAD)
railway variable set KENNY_BUILD=$SHA --skip-deploys   # the footer and /api/case report this hash
railway up --detach -m "$SHA"  # builds the Dockerfile (~15 min first time: torch + models)
railway domain                 # mint the public URL
curl -s https://<app>.up.railway.app/api/case | grep -o '"version":{[^}]*}'   # must show $SHA
```

Two facts the commands rely on. **`railway up` ships the working tree**, so a dirty tree
deploys code that exists nowhere else; deploy only from a pushed commit, and the
`KENNY_BUILD` variable is how the image learns its hash (`.dockerignore` excludes `.git/`,
so the image cannot ask git; `docker build --build-arg KENNY_BUILD=$SHA .` does the same
locally). **The `/data` volume keeps `case.yaml`, the roster, the rules, the catalog and
the ledger from first boot**, so a data fix in git reaches production only through the
reseed (`KENNY_SEED_FORCE=1` with `KENNY_SEED_FORCE_CONFIRM=yes`, below), which archives
the old case and its ledger beside it. Export the production ledger first
(`GET /admin/ledger/export`).

The image bakes the parsed Santa Cruz corpus and model weights at build time
(`scripts/prepare_deploy.py` refuses to ship an image whose known-answer scenarios
fail). First boot seeds `/data` from the image; after that the volume wins, so
ratifications and the ledger survive redeploys. Railway mounts volumes root-owned —
the entrypoint chowns `/data` and immediately drops to the unprivileged `kenny` user.

Optional variables (`railway variable set KEY=value`; check `railway variable --help` for
your CLI version's spelling):

| Variable | Effect |
|---|---|
| `KENNY_BUILD` | The short commit hash the footer and `/api/case` report (set per deploy, above) |
| `ANTHROPIC_API_KEY` | Turns on live Claude for routing/drafting (public spend — see above) |
| `KENNY_LLM=off` | Disables every model touchpoint even when a key is set; the chat badge says so |
| `KENNY_CHAT_RATE_LIMIT` | Chat requests per client per minute (default 20) |
| `KENNY_ADMIN_RATE_LIMIT` | Draft/ingest/upload requests per client per minute (default 10) |
| `KENNY_BANNER` | Standing banner text on both surfaces |
| `KENNY_REQUIRE_AUTH`, `KENNY_ADMIN_PASSWORD`, `KENNY_LEDGER_KEY`, `KENNY_VIEWER_PASSWORD` | The auth posture, above |

## Checks after deploy

```bash
curl -sf https://<app>.up.railway.app/healthz     # {"status":"ok"}
```

`/healthz` runs the ledger `verify()`, so a broken chain shows up as an unhealthy
instance instead of as a wrong answer discovered months later.

## Operating it

- **Ledger backup:** `railway ssh "cat /data/cases/santacruz/ledger.jsonl" > ledger.bak`.
  Single disk, no replication — treat it as losable.
- **Rule changes live on the volume, not in git.** Ratifying through the hosted admin
  writes `/data/.../rules_ratified.json`, which the repo never sees. Pull it back if you
  want to keep it.
- **Reseed** (replace the volume's case with the image's): set `KENNY_SEED_FORCE=1` and
  `KENNY_SEED_FORCE_CONFIRM=yes`, redeploy, then unset both. The old case is archived
  beside it on the volume, never deleted.

## What this deploy is **not**

Do not describe it as production or as 508-conformant.

- **Shared passwords, not identity.** The ledger records `actor: "admin"` for whoever
  knew the admin password. Real use needs SSO + per-user attribution.
- **One instance, one volume.** No failover, no replication, no backup schedule.
- **Rate limit is per-process.** Caps API spend and stops a runaway loop; not an attacker.
- **Synthetic-ish corpus.** Everything on the link is illustrative. Hence the banner.
- **Unaudited accessibility** (PRD §5.1a): built to WCAG 2.1 AA patterns, no VPAT.

## Fly.io (archived)

The original take-home deploy targeted Fly; those assets live in `archive/fly/` with
notes. The Fly app that exists in the account was never successfully deployed and holds
no data.
