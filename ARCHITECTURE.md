# Architecture

## Data flow
Pole device (IoT) --HTTPS POST--> /telemetry --> telemetry_events (Postgres, append-only)
|
v
resolve_pole_states() [localization.py]
(live/dark/unknown/stale per pole, using
device_id+seq for ordering/dedup)
|
v
detect_boundaries_for_dt() per transformer
(top-down tree walk, finds live/dark frontier,
classifies span_exact / span_range / dt / sensor_fault)
|
v
_merge_fragmented_dt_boundaries() +
_merge_feeder_level() (coverage-based
consolidation so noise doesn't fragment
one fault into many incidents)
|
v
_apply_schedule_suppression() (checks against
scheduled_outages, with overrun grace buffer)
|
v
sync_incidents_from_boundaries() --> incidents table
|
v
/tickets API --> Operator console (React)
(acknowledge -> crew_assigned -> resolved
-> verified -> closed, verification
re-checks live telemetry, not a button)


Ingestion interface note: in production, pole devices publish over NB-IoT to
an MQTT broker rather than HTTPS. For this exercise we exposed a plain HTTPS
POST endpoint (`/telemetry`) with the same payload shape as the spec. To
adapt to MQTT in production, a broker-subscribing worker process would
translate each MQTT message into the same `TelemetryEvent` write our
`/telemetry` handler performs today -- the ingest interface is intentionally
decoupled from the rest of the pipeline (everything downstream reads from
`telemetry_events`, not from the transport), so this swap wouldn't touch
localization, tickets, or the UI at all.

## Data sourcing and ingestion

- **At-least-once delivery / duplicates:** `/telemetry` is append-only and
  accepts every message it receives, including duplicates, without
  attempting to reject them at write time. Deduplication happens at READ
  time in `resolve_pole_states()`, using `(device_id, seq)` -- the spec's
  one reliable ordering tool -- rather than `device_ts` (skewed up to ±90s)
  or `received_ts` / arrival order (affected by network delay and retries).
  We take the highest-`seq` event per pole as authoritative.
- **Out-of-order / clock skew:** every telemetry row stores both `device_ts`
  (the device's own, untrustworthy clock) and `received_ts` (our server
  clock, always monotonic and trustworthy for "when did we learn this").
  Ordering/state decisions use `seq`, not either timestamp; `device_ts` is
  only used for staleness detection (comparing against simulated/real "now"),
  never for cross-device ordering.
- **Bursts:** ingest is a single, stateless `INSERT` per message with no
  synchronous cross-row locking or lookup, so it scales linearly with
  Postgres's own write throughput rather than any application-level
  bottleneck. We did not load-test the stated 500 msg/s sustained / 5,000-
  in-10s burst targets in this exercise (see Performance Targets below) --
  flagged honestly rather than claimed.

## Storage and internal model

Postgres via SQLAlchemy. Core tables:

- `feeders`, `transformers`, `poles` -- the network registry, matching the
  CSV schemas in the brief's data contract exactly.
- `telemetry_events` -- append-only raw ingest log, as described above.
- `scheduled_outages` -- mirrors the mocked department feed.
- `incidents` -- the output of localization; consumed by the ticket
  lifecycle API.

### Topology representation

`poles.seq_on_line` / `poles.parent_pole_id` hold registry ground truth,
present for ~40% of DTs. For the other ~60%, `poles.inferred_parent_pole_id`,
`poles.topology_source`, and `poles.topology_confidence` are computed
separately by a one-time MST (minimum spanning tree, Prim's algorithm)
inference pass rooted at each DT -- see `topology_inference.py` and
`DECISIONS.md` for full reasoning, rejected alternatives, and known failure
modes (parallel lines can be cross-assigned; branches can attach to the
wrong point).

**Why this shape and not another:** keeping known and inferred topology in
separate columns, rather than merging them into one `parent_pole_id`, means
the localization layer can always distinguish "the department told us this"
from "we guessed this," and can degrade its confidence/precision honestly
based on which one applies to a given span.

## The localization algorithm

Implemented in `backend/app/localization.py`. Runs in five stages:

1. **Current-state resolver** (`resolve_pole_states`): for each pole,
   resolves `live` / `dark` / `stale` / `unknown` from the latest telemetry
   event by `seq`. A pole with no device is always `unknown`. A pole whose
   last event was `energized=true` but who hasn't been heard from in over
   ~37.5 simulated minutes (2.5x the heartbeat interval + jitter) is
   `stale` rather than assumed dark -- silence alone is never treated as
   direct evidence of an outage.

2. **Boundary detection** (`detect_boundaries_for_dt`): a single top-down
   recursive walk per DT, starting from the DT root. The first dark/stale
   pole encountered on any path is a frontier; everything below it is
   swept into that incident regardless of its own individual telemetry
   state (a fault upstream affects everything downstream by definition,
   whether or not each pole's own dying message happened to arrive). This
   design specifically avoids an earlier bug where checking each dark
   pole's *immediate* parent independently caused the same poles to be
   claimed by multiple overlapping "incidents" whenever message loss made
   a mid-chain pole falsely look live -- see `DECISIONS.md` for the full
   incident.

   - `dark` (confirmed `power_lost` message) is always treated as direct,
     trustworthy evidence.
   - `stale` (silence only) is cross-checked against children before being
     trusted: a stale pole with LIVE children is physically impossible as
     a real fault (power cannot skip a pole) and is classified as
     `sensor_fault` instead -- not ticketed as an outage. A stale pole
     that is a leaf, or whose children are also down, is treated as a real
     candidate fault but at a discounted confidence (×0.7), since it's
     inferred from absence rather than a positive signal.
   - Whether a boundary is reported as an exact span (`span_exact`) or a
     range (`span_range`) depends on `topology_source`: known topology
     gives an exact live/dark pole pair; inferred topology (or a missing
     parent, e.g. a no-device pole sitting on the boundary) gives a range
     with the topology inference's own confidence score attached.

3. **Fragmentation merge** (`_merge_fragmented_dt_boundaries`,
   `_merge_feeder_level`): telemetry noise (the ~30% dying-message loss
   rate) can cause one real DT-wide or feeder-wide fault to fragment into
   several boundaries, because a handful of poles' `power_lost` messages
   simply never arrive in time. Rather than guessing a live-fraction
   noise threshold upfront (an earlier attempt at this, reverted -- see
   `DECISIONS.md`), we merge coverage-based: if multiple span boundaries
   under one DT collectively cover ≥60% of that DT's poles, or a single
   span boundary already covers most of a DT, they're consolidated into
   one `dt`-type incident. The same logic runs one level up for feeders:
   if every DT on a feeder is fully down, the DT-level incidents merge
   into one `feeder`-type incident. This directly implements the brief's
   "one alert per fault, not per symptom" requirement at both scales.

4. **Scheduled-outage suppression** (`_apply_schedule_suppression`): a
   boundary is suppressed only while an active scheduled outage's window
   `[start, end + 40min overrun buffer]` covers the current time -- the
   40-minute buffer matches the spec's stated routine 20-40 min overrun.
   Once that buffer passes, suppression lifts automatically even if the
   feed's `end` time has technically passed, so a fault that outlives its
   scheduled window is escalated rather than permanently hidden. Outages
   the feed lists but which never actually darkened any poles (the ~10%
   "cancelled but not updated" case) require no special handling: there's
   simply no real boundary to suppress, since suppression only ever
   applies to boundaries built from actual telemetry, never inferred
   directly from the feed.

5. **Persistence** (`sync_incidents_from_boundaries`): upserts results
   into the `incidents` table. Matches against existing OPEN incidents by
   (type, dt_id/feeder_id, boundary pole) so a fault that's still ongoing
   on a re-run updates the existing record rather than spawning a
   duplicate ticket. `sensor_fault` results are intentionally never
   written as incidents -- they're noise-filtering output, not something
   the control room should see as a ticket.

**Complexity:** boundary detection is O(n) per DT (single tree traversal
over that DT's poles); across the whole network it's O(total poles), run
once per localization pass. The fragmentation/feeder merge steps are
O(DTs) and O(feeders) respectively on top of that. This is deliberately a
plain graph traversal, not a model -- deterministic, instant, free, and
fully explainable, per the brief's explicit steer away from using an LLM
for localization itself.

**Known failure cases** (in addition to the topology-inference failure
modes in `DECISIONS.md`):
- A true DT-wide fault where the pole closest to the DT happens to be one
  of the ~30% with a lost dying message will present as a large
  `span_range` rather than a `dt`-type incident, since from telemetry's
  perspective there IS one confirmed-live pole. The confidence and
  affected-pole-count still correctly reflect a large-blast-radius
  incident; only the `type` label can be misleading. Documented as a known
  limitation rather than "fixed," since there's no way to distinguish this
  from a genuinely large span fault without more reliable message delivery.
- The 60% coverage threshold for fragmentation merging is a judgment call,
  not derived from the spec (see `DECISIONS.md`).

## Noise handling

- **Dead sensor vs. real outage:** see stage 2 above (live-children check).
- **Scheduled outages:** see stage 4 above (grace-buffered suppression with
  automatic expiry).
- **Debouncing:** the stale-vs-live threshold (2.5x heartbeat interval)
  itself acts as a debounce -- a single missed heartbeat doesn't trigger
  any state change.
- **False-positive story:** every incident's `confidence_reason` field
  states in plain language why the system believes what it believes
  (e.g. "exact known topology", "geometric topology inference", "merged
  from N fragmented span boundaries covering X% of this DT's poles",
  "stale -- inferred from silence"). The operator console surfaces this
  directly rather than showing a bare confidence number.

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | /health | Liveness check |
| GET | /network/stats | Pole/transformer counts, known vs inferred topology |
| GET | /network/transformers | List all transformers |
| GET | /network/poles?dt_id= | List poles, optionally filtered by transformer |
| POST | /telemetry | Ingest one telemetry event from a pole device |
| GET | /telemetry/recent?limit= | Debug: view recent ingested telemetry |
| POST | /simulator/tick?minutes= | Advance simulated time, fire due heartbeats |
| GET | /simulator/status | Current simulated time |
| POST | /simulator/reset | Reset simulator clock and device state |
| POST | /simulator/fault/span?dt_id= | Inject a span fault under a DT |
| POST | /simulator/fault/dt?dt_id= | Inject a DT-level fault |
| POST | /simulator/fault/feeder?feeder_id= | Inject a feeder-level fault |
| POST | /simulator/repair/{fault_id} | Repair a previously injected fault |
| GET | /simulator/faults | Ground truth of injected faults (test/debug only) |
| POST | /simulator/noise/dead-sensor?pole_id= | Inject a sensor failure with power still on |
| POST | /simulator/noise/scheduled-outage?scope=&target_id= | Inject a scheduled outage |
| POST | /simulator/noise/duplicate?pole_id= | Re-send a duplicate/out-of-order message |
| GET | /debug/localization/pole-states | Per-pole resolved state |
| GET | /debug/localization/boundaries | Raw boundary detection output |
| POST | /debug/localization/run | Run full localization, sync to Incidents |
| GET | /debug/localization/incidents | List all Incidents (debug view) |
| GET | /tickets?status= | List tickets, optionally filtered by status |
| GET | /tickets/{id} | Get one ticket |
| POST | /tickets/{id}/acknowledge | detected -> acknowledged |
| POST | /tickets/{id}/assign-crew | acknowledged -> crew_assigned |
| POST | /tickets/{id}/mark-resolved | crew_assigned -> resolved (rejected if poles still dark) |
| POST | /tickets/{id}/verify | resolved -> verified (re-checks telemetry) |
| POST | /tickets/{id}/close | verified -> closed |
| POST | /tickets/auto-verify-sweep | Bulk-check all resolved tickets, auto-verify if telemetry confirms |
| GET | /ai/incidents/{id}/summary | AI-generated (or template-fallback) dispatch note |

Interactive OpenAPI docs (auto-generated, not hand-maintained) available at
`/docs` on the deployed backend.

## UI reasoning

The operator console leads with the **incident list**, not the map --
per the brief, the first thing a 2am operator needs is "something broke,
where, how bad" as text they can scan, not a map they have to visually
parse under time pressure. Each incident card shows: type badge,
pincode (the thing a crew dispatcher actually needs), status, affected
pole count, and a plain-language confidence band (High/Medium/Low) rather
than a bare percentage, plus the next required action as a single button.

The map is secondary and schematic, not tile-based (a legitimate choice
per the FAQ) -- it plots real pole GPS coordinates in an SVG viewport,
avoiding any external tile-service dependency (and its associated
deploy/API-key failure modes) entirely. Selecting an incident highlights
its affected poles directly on the map so an operator can see the actual
shape of the outage, not just a pin.

**Deliberately left off the main screen:** raw telemetry logs, topology
confidence scores for every individual pole, and the simulator controls
(collapsed behind a toggle, since they're a testing/demo tool, not part
of the real operator workflow -- clearly labeled as such).

**Decision most likely to be wrong:** the 60% coverage threshold for
merging fragmented boundaries into one incident is a judgment call with
no basis in the spec; a different value might better balance false-merges
against false-splits on a real network with a different loss-rate profile.

## The AI feature

Plain-English dispatch-note generation (`/ai/incidents/{id}/summary`).
Turns an already-computed, deterministic `Incident` record into a short,
operator-facing explanation via one Claude API call. Deliberately scoped
away from localization itself, per the brief's explicit warning against
using an LLM for fault-finding -- it has no ability to change where a
fault is reported, only to describe an already-decided result in plain
language.

**Cost:** one short API call per incident, on demand (never per telemetry
message) -- a few hundred input/output tokens, well under a cent per call
at current pricing.

**When the model is unavailable or wrong:** if `ANTHROPIC_API_KEY` is
unset, or the API call fails for any reason (network, timeout, malformed
response), the endpoint falls back to a template string built
deterministically from the same fields, and reports which path was used
via a `source` field (`"ai"` vs `"template"`) so the caller/reviewer can
tell which they're looking at. This was verified directly in this
deployment, since the reviewer's environment will not have our API key.

## Performance targets

| Metric | Target | Status |
|---|---|---|
| Fault occurrence -> localized ticket visible in UI | < 120s (p95) | Not formally load-tested; observed sub-second in manual testing at this network scale (~3,000 poles) since localization is a single in-memory tree walk, not a search |
| Ingest throughput sustained | ≥ 500 msg/s | Not load-tested |
| Ingest burst tolerated | 5,000 msgs in 10s | Not load-tested |
| Operator console load, incident list | < 2s | Observed comfortably under 2s at this scale; not formally benchmarked |
| Restoration -> ticket auto-verified | < 120s | Verified functionally correct (see DECISIONS.md); exact timing not benchmarked |

Per the brief's own guidance, these are stated honestly as un-load-tested
rather than claimed -- the priority in the available time went to
correctness (localization edge cases, noise filtering) over throughput
benchmarking. With more time, the next step would be a synthetic load
generator hitting `/telemetry` directly at the target rate while
measuring end-to-end latency to a visible ticket.
