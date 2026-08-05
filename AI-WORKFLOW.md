# AI Workflow

Built end-to-end in conversation with Claude (Anthropic), used as an
interactive pair-programmer/architect across every milestone: schema
design, the topology-inference algorithm, the localization engine, the
ticket lifecycle, the operator console UI, deployment configuration, and
this documentation.

## What was delegated vs. written/verified by hand

Claude drafted nearly all first-pass code (models, routers, the simulator,
the localization algorithm, the React UI, Docker/Render configs). Every
piece was then actually run against real data by me before being trusted:
curl-testing every new endpoint, inspecting real database state via psql,
reading actual backend logs on failures rather than guessing, and manually
driving the simulator through realistic fault/repair/noise scenarios. I
did not write raw algorithm code from scratch, but I ran, understood, and
in several cases diagnosed the actual root cause of bugs before Claude
proposed or I accepted a fix.

## Concrete cases where AI output was wrong, misleading, or needed correction

1. **DT-fault fragmentation bug (found by me running a real test, not
   suggested by the AI):** injecting a full 133-pole DT fault produced 3
   separate incidents instead of 1. Claude's first fix attempt (a
   live-fraction threshold, 15%) was based on a guess rather than the
   actual expected noise rate (~27-30% given the spec's stated loss rate),
   and was the wrong general approach -- fragile to network size and drop
   rate. I pushed back implicitly by continuing to test, which surfaced a
   second, related gap (single-boundary DTs not being upgraded to
   `dt`-type even at ~100% coverage) that the first fix hadn't addressed.
   The final fix (coverage-based post-processing merge) is more robust and
   is what's in the repo now. See DECISIONS.md for the full before/after.

2. **A `main.py` edit was described but never actually applied to disk**
   (twice, at different points in the build) -- confirmed by running
   `cat backend/app/main.py` directly rather than trusting that a prior
   `cat > file << EOF` command had succeeded. Both times, the actual bug
   (a missing router import) only became visible from a real 404 in the
   browser/curl output and the corresponding clean backend startup log
   (no traceback), which is what told us the failure was "route never
   registered," not "route crashed."

3. **A concatenated `requirements.txt` line broke the Render deploy**
   (`faker==29.0.0pytest==8.3.3` on one line, from an `echo >>` without a
   preceding newline). Caught from the actual Render build log, not
   anticipated in advance -- a reminder that AI-suggested shell one-liners
   for file editing need their actual on-disk result checked, not assumed
   correct from the command alone.

## Roughly how much of the final code is AI-generated

The large majority of the codebase (schema, routers, localization
algorithm, simulator, frontend) was AI-drafted. Essentially none of it
was accepted without being run and its actual output inspected first --
every milestone in this build followed a "write it, run it, paste the
real output back, fix what's actually wrong" loop rather than accepting
code on faith. I can explain the MST topology-inference algorithm, the
top-down boundary-detection walk, the coverage-based fragmentation merge,
and the telemetry-verified ticket resolution logic without referring back
to the code, including why each design choice was made over the
alternatives that were considered and rejected (documented in
DECISIONS.md).

## Best example of the working session

The DT-fragmentation bug (above) is the strongest example: it wasn't
something I asked Claude to check for -- it surfaced from actually running
a test I chose to run (a full DT fault, expecting one incident), noticing
the real output didn't match the expected physical behavior, and then
iterating through two different fix strategies (one rejected, one kept)
based on reasoning about *why* the first approach was fragile rather than
just re-running until a test passed.
