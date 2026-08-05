# Decisions Log

## 2026-08-02 — Stack choice
Chose Python/FastAPI for backend, React (Vite) for frontend, Postgres for storage.
Rationale: fastest stack for the author; FastAPI's typing/Pydantic makes the
localization algorithm's data contracts explicit and easy to test.

## 2026-08-02 — Topology inference approach (implemented)

**Decision:** For the ~60% of DTs with no seq_on_line/parent_pole_id, infer
line topology using a minimum spanning tree (Prim's algorithm) rooted at the
DT, with haversine distance as edge weight.

**Rejected alternatives:**
- Nearest-neighbor chaining: simpler, but doesn't handle branches/spurs well
  and can produce disconnected or crossed paths.
- Coarse DT-level-only localization for the unknown 60%: safer, but throws
  away information we can estimate, and the brief explicitly rewards a
  reasoned attempt over a refusal to try.
- Outage-history-based learning: mentioned as future work below — needs
  weeks of live data we don't have in a 7-day exercise.

**Why MST:** real LT line layout minimizes copper run, so a minimum spanning
tree rooted at the transformer is a physically reasonable prior for how the
poles are actually wired, not just "closest point" wire (which is a
description, not a claim about correctness).

**Confidence scoring:** each inferred edge gets confidence =
1 - (edge_length / (edge_length + local_median_edge_length)), clipped to
[0.05, 0.98]. Short, unambiguous edges score high; edges comparable in length
to the local spacing (i.e. genuinely uncertain which pole is upstream) score
lower. Average confidence on synthetic data: ~0.52.

**Known failure modes:**
- Two parallel LT lines running close together (e.g. opposite sides of a
  street) can have poles cross-assigned between lines, since MST only sees
  geometry, not which physical conductor a pole is on.
- A branch/spur can attach to the wrong point on the main run if two
  candidate attachment points are similarly close.
- The algorithm has no way to know about a pole that is geographically close
  but electrically on a different circuit entirely (e.g. crosses under
  another feeder's poles).

**What the system does differently for known vs. inferred topology:**
- `topology_source = "known"`: localization can report an exact span
  (`localization_type = "span_exact"`) between two specific poles.
- `topology_source = "inferred"`: localization reports a span but flags it
  as an estimate, and the UI shows the confidence score with it
  (`localization_type = "span_range"` when confidence is below a threshold,
  see ARCHITECTURE.md for the exact cutoff once localization is implemented).

**What I would ask the department for, if I could:** a phased pole-order
survey for the DTs with lowest inferred confidence first (i.e. use our own
uncertainty score to prioritize where a physical walk-the-line survey is
worth the department's time), rather than a blanket survey of all 60%.

## 2026-08-02 — Seeding is currently manual
`docker compose up` alone does not yet seed the database — generate_data.py
and topology_inference.py must be run manually via `docker compose exec`.
This violates gate G3 (seeded on startup) and needs fixing before submission.
Tracked as a known TODO, to be resolved by hooking both scripts into the
FastAPI startup event, gated behind an env var (SEED_ON_STARTUP) so repeat
restarts don't regenerate data and wipe manually-created state.

## 2026-08-02 — Simulator device state is in-memory only (known limitation)
The simulator's per-device heartbeat scheduling state (_device_state in
simulator.py) lives only in the backend process's memory, not the database.
A backend restart silently resets which devices are "due" for a heartbeat,
though real ingested telemetry in Postgres is unaffected. Acceptable for a
7-day exercise and a single review session; would need a persisted
simulator_state table for anything longer-running or multi-instance.
Discovered when a dead-sensor test failed with "no active device to fail"
immediately after a backend restart, before any tick had re-initialized
device state.

## 2026-08-02 — Fixed: scheduled-outage ID collision
inject_scheduled_outage() originally generated IDs from simulated time only
(SO-SIM-{sim_time}), which collided when two outages were injected within
the same simulated second — caused a real 500 error (Postgres unique
constraint violation) during testing. Fixed by appending a random 4-digit
suffix. Caught by reading the actual backend traceback rather than assuming
the endpoint was broken in some other way.

## 2026-08-02 — Reload desync between sim clock and DB telemetry (same root cause as above)
Editing backend files while the stack is running triggers uvicorn --reload,
which resets in-memory simulator state (_sim_clock, _device_state) but does
NOT touch already-ingested telemetry_events rows in Postgres. This can
produce a state where sim_now appears to be BEFORE the device_ts of real
telemetry already in the DB, which briefly showed up as negative
seconds_since_last_seen values in the resolver's debug output. Not a bug in
the resolver — a test-hygiene issue. Workaround during development: after
any backend code change, run DELETE FROM telemetry_events + POST
/simulator/reset before re-testing, to get a clean, internally-consistent
baseline. Confirmed as a pre-submission checklist item: always POST
/simulator/reset (and clear telemetry) before recording the demo video, so
the demo starts from a known-clean state.

## 2026-08-02 — Fixed: boundary detection double-counted poles across overlapping incidents
First version of detect_boundaries_for_dt() checked each dark pole's
IMMEDIATE parent state independently. Because power_lost messages only
arrive ~70% of the time, a single real fault produces a patchwork of
poles whose own telemetry still looks "live" even though they are
electrically downstream of the real break. This caused the same poles to
be claimed by multiple different "frontier" boundaries — a DT fault
affecting 29 poles was reported as 7 overlapping boundaries with heavy
duplicate pole membership. This is exactly the "one alert per dark pole"
failure mode the brief calls out as the biggest scoring risk.

Fixed by replacing the per-pole independent check with a single top-down
recursive walk per DT, starting from the DT root. The first dark/stale
pole encountered on any path is the frontier; every pole below it is
swept into that same incident regardless of its own individual telemetry
state, because a fault upstream affects everything downstream by
definition — whether or not each pole's own dying message happened to
get through. Verified: injecting a 29-pole DT fault now produces exactly
one boundary with 28 unique affected poles (root pole excluded correctly
as the last-confirmed-live point).

## 2026-08-02 — Observed edge case: a true DT fault can present as span_range
When the pole closest to the DT (first in topology order) happens to be
one of the ~30% whose dying message is lost, it still reads as "live" in
our resolver even though the whole DT is actually dark. The algorithm then
correctly, but perhaps confusingly, reports this as a large span_range
fault rather than a dt-type fault, because from telemetry's perspective
there IS one confirmed-live pole. This is arguably still the right
behavior — the confidence and affected_pole_count in the output make clear
this is a large-blast-radius incident regardless of label — but it means
the UI should not treat "type" as a hard guarantee of the true physical
fault category, only as our best label given available evidence. Noted as
a known limitation rather than "fixed," since there's no way to distinguish
this from a real large span fault without more reliable delivery.

## 2026-08-02 — Noise filtering: dead sensor vs. real fault
Implemented the core physical rule from 01-problem-context.md Section 2: a
single isolated stale/dark pole with LIVE children downstream is physically
impossible as a real line fault, since power cannot skip a pole. In
detect_boundaries_for_dt(), a STALE pole (silence only, no confirmed
power_lost message) is checked against its children's states before being
treated as a candidate fault:
- If it has live children and no down children: classified as
  "sensor_fault", not ticketed as an outage, and the walk continues past
  it as if it were live (no real evidence the line broke here).
- If it's a leaf, or has down children too (outage propagating downstream):
  treated as a real candidate fault, but at a DISCOUNTED confidence (x0.7)
  since it's inferred from silence rather than a confirmed power_lost
  signal.
A DARK pole (confirmed power_lost received) is always treated as direct
evidence and never discounted this way -- the distinction between "we were
told" and "we inferred from silence" is preserved end to end.

Verified: injecting inject_dead_sensor on a pole with live children,
advancing sim time past the stale threshold, correctly produces a
sensor_fault entry and ZERO span/dt boundaries -- confirming no false
positive ticket. This required a real backend-service call and two rounds
of test errors (mistakenly running the noise-injection call with a literal
unsubstituted "PASTE_ID_HERE" instead of the real pole ID) before landing
a valid test -- worth remembering to always sanity-check curl commands
before trusting their (empty) results.

## 2026-08-02 — Scheduled outage suppression, with expiry
Boundaries are checked against the scheduled_outages table before being
surfaced. A boundary is suppressed only while an active outage's window
[start, end + 40min overrun buffer] covers sim_now -- the 40-minute buffer
matches the spec's stated routine overrun (20-40 min). Once that buffer
passes, suppression is lifted automatically, even though the feed's `end`
timestamp has technically passed -- this directly implements the spec's
warning against treating the scheduled-outage feed as gospel.

Outages the feed lists but which never actually darkened any poles (the
~1-in-10 "cancelled but not updated" case) require no special handling at
all: suppression only ever applies to boundaries built from real detected
telemetry, never inferred from the feed directly, so a phantom feed entry
with no real darkness simply has nothing to suppress.

Verified end to end: injected a real 60-min scheduled DT outage, confirmed
the resulting fault was suppressed at sim_time well within the window,
confirmed it remained suppressed through the 40-min grace period, and
confirmed it correctly reappeared as an ACTIVE, unsuppressed fault once
sim_time passed the buffer -- proving the system escalates a fault that
outlives its scheduled window rather than silently trusting the feed
forever.

Caught one real bug during this step: a full-file rewrite of
localization.py did not actually persist to disk on the first attempt
(likely a shell buffering/paste issue with the heredoc), which surfaced as
a TypeError only when the new include_suppressed parameter was called via
the API. Caught by grep-verifying the function signature on disk
immediately after the write, before re-testing -- now standard practice
for every file overwrite in this project.

## 2026-08-02 — Fixed: telemetry noise fragmented one DT fault into multiple incidents
Discovered while persisting Incidents: injecting a full DT fault (133
poles) produced 3 separate incidents instead of 1. Root cause: shortly
after a fault, ~30% of affected poles' power_lost messages haven't arrived
yet and not enough sim time has passed for the stale threshold to kick in,
so a handful of poles scattered through the tree still read "live" purely
from missing telemetry, not real state. Each false-live reading became an
accidental extra frontier in the top-down walk, splitting one real fault
into several.

First attempt: loosened the DT-level fast path from "zero live poles" to
"fewer than 15% live." This was the wrong fix -- a live-fraction threshold
requires guessing the exact noise rate in advance (~27% expected here,
not 15%), and the right number changes with drop rate and network size.
Reverted.

Second, better fix: kept the original strict DT fast path, and instead
added a POST-PROCESSING merge step (_merge_fragmented_dt_boundaries).
After per-DT boundary detection, if a DT produced multiple span-type
boundaries whose COMBINED affected poles cover >=60% of that DT's total
poles, they are merged into one dt-level incident, with a note explaining
the merge and how many boundaries/what coverage triggered it. This is
more robust than a live-fraction guess because it reasons from what was
actually observed (real overlap/coverage) rather than an assumed noise
rate, and it still allows genuinely separate simultaneous faults on
different parts of a large DT to remain distinct incidents when their
combined footprint is small relative to the whole DT.

Verified: the same 133-pole DT fault now produces exactly 1 incident,
type "dt", confidence 0.9, with confidence_reason explicitly stating it
was merged from 3 fragmented boundaries at 98% coverage.

Coverage threshold (60%) is a judgment call, not derived from the spec --
noted here as an assumption per the FAQ's guidance that documented
assumptions are treated as correct answers.

## 2026-08-02 — Fixed: single-boundary DTs weren't eligible for DT-level upgrade
_merge_fragmented_dt_boundaries originally required 2+ span boundaries
before considering a DT-level merge (len(span_boundaries) <= 1 returned
early). This meant a DT whose fault happened to produce exactly ONE span
boundary -- even one covering ~100% of that DT's poles -- was never
relabeled as a whole-DT fault. Found while testing feeder-level merging:
faulting all 4 DTs on feeder F-01-03 initially produced only 1 feeder-
correct incident (D-0036, which had fragmented into 2 boundaries) plus 3
separate span-type incidents for the other DTs (D-0007, D-0013, D-0040),
each of which had happened to produce only 1 span boundary despite
covering their entire DT.

Fixed by changing the guard to len(span_boundaries) == 0, so a single
span boundary at high coverage is now also upgraded to a dt-type incident,
with note text adjusted to say "single span boundary already covers X%"
rather than the more general merge language. Re-verified: all 4 DTs on
F-01-03 now correctly collapse into exactly ONE feeder-level incident
covering all 276 affected poles.

## 2026-08-02 — Milestone 5: ticket lifecycle verified
Full state machine (detected -> acknowledged -> crew_assigned -> resolved
-> verified -> closed) tested end to end. Two things confirmed working as
designed:
1. mark-resolved is REJECTED (409) if any affected pole is still dark/
   stale per live telemetry -- a crew cannot simply claim a fix is done.
2. verify independently re-checks telemetry before allowing the
   resolved -> verified transition.

Noted UI/product implication: fault_id (simulator, in-memory, resets on
backend restart) and ticket/incident id (Postgres, persistent) are
separate ID spaces that can coincidentally look similar (both small
integers). The operator console (Milestone 6) will only ever surface
ticket IDs; fault_id remains an internal simulator/testing concept, never
shown to the "operator" persona, to avoid this exact confusion.

## 2026-08-02 — AI feature: incident dispatch summaries
Chose plain-English dispatch-note generation as the one AI-shaped feature,
deliberately scoped away from localization itself (which remains 100%
deterministic graph traversal, per the brief's explicit warning against
using an LLM for fault-finding). The AI call takes an already-computed
Incident record and produces a short operator-facing explanation; it has
no ability to change where the fault is reported.

Degrades gracefully: if ANTHROPIC_API_KEY is unset or the API call fails
for any reason (network, timeout, malformed response), the endpoint falls
back to a template string built from the same fields, and reports which
path was used via a "source" field ("ai" vs "template") so the UI/reviewer
can tell which they're looking at. Verified this fallback path directly,
since the deployed reviewer environment will not have our API key --
exactly the scenario 02-data-and-systems.md Section 5 warns about for
geocoding, applied here to the AI feature as well.

Cost: one short Claude API call per incident, on demand (never per
telemetry message) -- a few hundred tokens, well under a cent per call.
