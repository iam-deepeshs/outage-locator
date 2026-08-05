# Outage Locator — KSPDB Fault Detection & Localization

A system that detects and localizes low-tension power faults from pole-level
telemetry, for the (fictional) Karnataka State Power Distribution Board.

**Status: work in progress.** Built incrementally against a 7-day take-home
brief. See DECISIONS.md for what's done, what's stubbed, and what's cut.

## What it does (target scope)
- Ingests pole-liveness telemetry
- Groups dark-pole signals into located, deduplicated fault incidents
- Distinguishes real outages from dead sensors and scheduled load-shedding
- Runs tickets through detected → acknowledged → crew assigned → resolved →
  verified → closed, with telemetry-based auto-verification
- Ships a fault simulator to drive the whole pipeline end-to-end
- [AI feature — to be added]

## Quick start
    git clone <repo-url>
    cd outage-locator
    docker compose up --build

Then:
- Backend API: http://localhost:8000 (docs at /docs)
- Frontend: http://localhost:5173

Seed the synthetic network (until this is automated on startup):
    docker compose exec backend python -m app.generate_data
    docker compose exec backend python -m app.topology_inference

## Live deployment
Frontend (open this one): https://outage-locator-frontend.onrender.com
Backend API: https://outage-locator-backend.onrender.com

Note: free-tier backend sleeps after ~15 min idle; first request after
that may take 30-60s to wake up. See DEPLOYMENT.md for details.

## Demo video
[link -- added after recording]

## Documentation map
- `ARCHITECTURE.md` — system design, data model, localization algorithm, API surface
- `DEPLOYMENT.md` — environment variables, exact run commands, troubleshooting
- `DECISIONS.md` — decision log, assumptions, what's cut
- `AI-WORKFLOW.md` — how AI tools were used building this
