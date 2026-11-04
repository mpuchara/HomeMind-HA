# U05b.4c.1f — continuation handoff

Worktree `HomeMind-HA-universal-232` on
`codex/universal-u05b4c1f-qualified-evidence`. Draft
[#368](https://github.com/mpuchara/HomeMind-HA/pull/368) targets the head of
Draft #367 at `c4adb44512f8c1954a8cefdd7970710b249591d2`.
The [base workflow](https://github.com/mpuchara/HomeMind-HA/actions/runs/41816293453)
passed for that exact base SHA. Runtime change
`8f73b665345c19110302c0f1e57f1665aadf4527` adjusts the optional collector's idle
admission wake. The fanout matrix was run with the harness in
`73836492a0ae61bb31b3c4b3111ac38a9d24079c`.

## Resume from WSL

Raw local measurements and logs are in
`/home/mpuchara/homemind-u05b4c1f`. Keep existing files and use a fresh output
name. Run timed workloads serially with the existing disposable Linux venv.

```bash
source ~/homemind-perf-venv/bin/activate
python tools/benchmark_qualified_evidence.py \
  --modes positive,normal,busy,overload,off,incomplete --fanout 16 \
  --output ~/homemind-u05b4c1f/fanout-16-rerun.jsonl
python -m unittest discover -s tests -p 'test_universal*.py' -v
python tools/benchmark_qualified_soak.py --duration 14400 --window 45 \
  --output ~/homemind-u05b4c1f/soak-4h.jsonl
```

For an 8-hour run use `--duration 28800`. The collector restart fixture
closes and resnapshots the synthetic stream. Keep transport-level WebSocket
and full scheduler acceptance separate. Use the existing composed #367
fixtures for end-to-end scheduler comparisons. Follow exact-head
[checks for #368](https://github.com/mpuchara/HomeMind-HA/pull/368/checks)
for Python 3.11, Python 3.13, configured benchmarks and image smoke.

## Acceptance boundary

Status is **BLOCKER** where normal or busy evidence remains incomplete.
Distinguish the installed predecision limit from starvation below that limit.
A capacity change requires a separate safety and resource contract.
The short persistent soak provides component recovery observations.
The [runtime-fix workflow](https://github.com/mpuchara/HomeMind-HA/actions/runs/41873139096)
passed Python 3.11, Python 3.13, configured benchmarks and image smoke for
the exact runtime-fix commit. See the PR checks for the later runner/report HEAD.
Four-hour and eight-hour host runs are **NOT RUN**. Runtime stability and
real Home Assistant / Raspberry Pi acceptance remain open.

The [measurement checkpoint](benchmarks/UNIVERSAL_U05B4C1F_SUMMARY.json)
records completed, rejected and failed runs. The benchmark's active inference
load uses the control pool directly. The Candidate activity counters and
matched OFF/normal/incomplete intent signatures define the observed scope of
the performance and functional results.
## What to investigate next

At 1000/200, separate preparation throughput below the durable-config bound
from the above-bound case. The matched normal run produced four qualified
samples out of 384 intact decisions. The busy run preserved all 384
decisions but qualified zero. Candidate had 92 queue drops even with zero
historical collector drops. Increase measurement duration and retain
admission timings and observed config counts before proposing a change.
Avoid treating fewer active Candidates as equivalent to the same workload.

For system stability, run the four-hour runner and a separate production
composition soak with fake WebSocket transport and scheduler participation.
The short synthetic run cannot cover a multihour backlog trend, UI latency or
transport recovery. Existing retention/cancellation tests are the individual
fault evidence. The soak exercises actual SQLite contention, but does not
inject a mid-session connection loss or delayed metadata commit.
## Executed checks

The WSL 5/1 short soak finished three windows in 15.5 s. Each window observed
SQLite busy and recovered the native writer. All three disconnect/restart
cycles rejected a cross-gap export. The short threshold checks returned no
failures and overall stability is `INCONCLUSIVE`. The raw measurements
include 0 HA service calls.

The focused WSL suite ran 289 tests. One retention rollback test failed
in fixture setup before the rollback, with `missing_historical_decision_receipt`.
The same test passed in isolation on this branch and on the exact #367
source. The aggregate failure and both isolated results are retained.
The reason for the one-off fixture miss remains unresolved.

## Additional SQLite fault validation

The closure probe against `673291f2ba0ec9980981d3810feb07f792fff44d` found
that retries repeatedly reused a closed worker connection and that shutdown
timed out. Commit `0b29ed013a32d45587b27237d268a9745b94b600` routes the closed
session exception to the existing session-open retry after marking the
write failure and dropping the uncertain envelopes. The regression verifies
one reopened session, a subsequent committed observation, a preserved prior
watermark, and rejection across the lost interval.

A separate test holds the real metadata transaction open before sealing
coverage. The reader sees the old committed watermark and ingress can queue
a later event during the hold. On release the writer commits and coverage
can advance. The collector suite passed all 41 tests in WSL, including both
new fault regressions. The fanout numbers were recorded before the
closed-session fix and did not exercise that exception path.
