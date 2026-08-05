# Deployment

## Prerequisites
- Docker Desktop (tested on macOS, Apple Silicon)
- No other local installs required -- Postgres, Python, and Node all run in containers

## Run it locally

    git clone https://github.com/iam-deepeshs/outage-locator
    cd outage-locator
    docker compose up --build

First build takes a few minutes (pulling Postgres/Python/Node base images).
The database auto-seeds on first startup (synthetic pole/transformer
network + topology inference) -- no manual commands needed.

## Verify it worked
- `curl http://localhost:8000/health` -> `{"status":"ok",...}`
- `curl http://localhost:8000/network/stats` -> pole/transformer counts
- http://localhost:8000/docs -> interactive API explorer
- http://localhost:5173 -> operator console UI

## Live deployment

- **Frontend (open this one):** https://outage-locator-frontend.onrender.com
- **Backend API:** https://outage-locator-backend.onrender.com
- Hosted on Render's free tier via `render.yaml` (Blueprint deploy: Postgres +
  Docker web service + static site, all from one file, no manual dashboard
  config beyond the initial connect).

**Cold start note:** the free-tier backend spins down after ~15 minutes of
inactivity and takes 30-60 seconds to wake on the next request. If the app
looks unresponsive on first load, wait a moment and retry -- this is
expected free-tier behavior, not a broken deploy (observed directly during
testing: a request during cold-start returned a network-level "failed to
fetch" in the browser, and succeeded normally on retry a few seconds later).

## Environment variables

| Variable | Purpose | Required | Default |
|---|---|---|---|
| DATABASE_URL | Postgres connection string | Yes | set automatically by render.yaml / docker-compose.yml |
| SEED_ON_STARTUP | Auto-generate synthetic network if DB is empty | No | `true` |
| ANTHROPIC_API_KEY | Enables AI-generated dispatch notes | No | unset -> falls back to template summaries |
| VITE_API_URL | Backend URL the frontend calls | Yes (frontend build-time) | set by render.yaml |

Commit `.env.example` at the repo root documents these; no secrets are
committed to the repo (`ANTHROPIC_API_KEY` is entered directly in Render's
dashboard as a Blueprint sync-false variable, never in `render.yaml` itself).

## How to verify the deployed system works

1. Open the frontend URL above in a private/incognito window (no login).
2. Expand "Simulator controls" at the bottom.
3. Click "Advance time (+20 min)" once or twice.
4. Enter a DT id (e.g. `D-0005`) and click "Inject DT fault".
5. Click "Run localization".
6. A new incident card should appear in the list within a few seconds.
7. Click the card -- affected poles should highlight red on the schematic map.
8. Click "Acknowledge" -> "Assign crew" -> "Mark resolved" (this should be
   REJECTED with a 409 error, since the fault hasn't been repaired yet --
   that rejection is a feature, not a bug: proves resolution is
   telemetry-verified, not button-driven).
9. Use `/simulator/repair/{fault_id}` (via curl, or add to the simulator
   panel) to actually restore the affected poles, then retry "Mark
   resolved" -- it should now succeed, followed by "Verify" and "Close".

## Reset to a clean state

**Local:**

    docker compose down -v

Wipes the Postgres volume; next `docker compose up` auto-reseeds.

**Deployed (Render):** connect to the Render Postgres instance via its
provided external connection string and run:

    DELETE FROM telemetry_events;
    DELETE FROM scheduled_outages;
    DELETE FROM incidents;

Then hit `/simulator/reset` on the live backend URL. Poles/transformers/
feeders are left in place (they don't need regenerating), but a full reset
(`DELETE FROM poles; DELETE FROM transformers; DELETE FROM feeders;` too)
followed by a backend restart will trigger a full auto-reseed from empty.

## Troubleshooting

**`FATAL: database "outage" does not exist` looping in db logs on first
local startup.** The healthcheck's default `pg_isready -U outage` checks a
database matching the username, but our DB is named `outage_locator`. Fixed
by explicitly setting `pg_isready -U outage -d outage_locator` in
`docker-compose.yml`'s healthcheck. If seen, run `docker compose down -v`
and rebuild.

**A local file edit doesn't seem to take effect / silent 404 on a new
route.** `uvicorn --reload` (local dev only) resets the process, which also
resets in-memory simulator state (`_sim_clock`, `_device_state`,
`_injected_faults`) -- a fault_id or sim time from before an edit is no
longer valid after the reload. Always re-verify a file's actual on-disk
content (`cat`/`grep`) immediately after any `cat > file << EOF` rewrite
before re-testing; this project hit that exact class of bug multiple times
during development (see DECISIONS.md) where a heredoc write silently didn't
land and testing continued against stale code.

**Render build fails with `pip install` error on a garbled requirements.txt
line (e.g. two packages concatenated on one line).** Happened once during
this project's deploy from `echo "package==version" >> requirements.txt`
run without a file already ending in a newline. Fix: ensure every line in
`requirements.txt` is a single clean `package==version`; use
`printf '\npackage==version\n' >> file` rather than bare `echo >>` when
appending, to guarantee a newline boundary.

**Render Blueprint says "render.yaml not found on main branch."** The file
was created locally but never `git add`ed/committed/pushed -- a `git status`
check confirming it's tracked, followed by push, resolves this. Happened
once during this project's deploy.

**Cold-start "Failed to fetch" on the deployed frontend's first action
after idle.** See "Cold start note" above -- retry after a few seconds.

**Port conflicts locally.** If `5432`, `8000`, or `5173` are already in use
on your machine, stop the conflicting process or change the host-side port
mapping in `docker-compose.yml` (left-hand side of each `"host:container"`
pair).

**ARM vs x86.** All base images used (`python:3.12-slim`, `node:20-slim`,
`postgres:16`) publish multi-arch manifests and were built/tested on Apple
Silicon (ARM64) without issue; no known ARM/x86-specific problems
encountered in this project.
