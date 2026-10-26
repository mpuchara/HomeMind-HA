# Performance optimization continuation

CURRENT VERSION: 0.14.230 (experimental test candidate)
CURRENT BRANCH: codex/performance-capture-20261027
CURRENT COMMIT SHA (executable/test/tool snapshot): 7332f42abbb9896cde481f4f4439e53d7cd8809a
LATEST PR: https://github.com/mpuchara/HomeMind-HA/pull/346 (draft; do not merge automatically)
BASE BRANCH: codex/shadow-agent-scaling-20261026
BASE COMMIT SHA: 8e47cd6652c1364f0b048f79d507929b1cca6168

The final documentation/BUILD_INFO receipt commit may follow the executable
snapshot above. Resolve the current exact HEAD with `git rev-parse HEAD`; packages
record that exact HEAD in SOURCE_COMMIT.txt. Verify the 658-file executable source
manifest before reusing validation. The previous .229 checkout at
`../HomeMind-HA-sensor-action-20261016` has the same complete tree as the base SHA.
The remote main branch was 0.14.184 at the start; do not switch to it for this work.

## COMPLETED STAGES

- A0 foundation: shipped-composition normal-policy benchmark, paired runner,
  bounded p50/p95/p99, opt-in process CPU, anonymous timing-only trusted endpoint.
- A2a routing subset: remove full-house dependency-set copying from each scheduler
  pass. This is a completed bounded optimization, not completion of all A2a.

## COMPLETED CHANGES

`Engine.process` takes private copies of owner sets for current event + retry
entities under the existing atomic lock. Full state and revision snapshots stay
detached and shared across workers. No durable writes, model logic, reward,
qualification, Correct, promotion/rollback, WAL/disk settings or FIFO rules changed.

Tournament submit False is specifically sync_required; capacity/capture failures
already report optional gaps and return True. Retain lifecycle synchronous work.
No capture-moving optimization was implemented in .230.

Packaging now uses git archive HEAD with commit identity, per-file SHA manifest,
CRC/content/version/LF checks and the requested repository/addon ZIP names.

## TEST RESULTS

Final local suite: 2817 tests OK / 433.865 s, exit 0; compileall, 22 JS syntax checks, UI VMs and shipped entrypoint pass. CI Python 3.11/3.13: 2817 tests each, required benchmarks and Docker build/start pass.

See `docs/benchmarks/PERFORMANCE_0_14_230_QA.json` for exact final local/CI receipts,
counts, source SHA and log hashes. Initial CI 40418705499 found five UI version
consistency failures because index.html still said .229. Commit 7332f42 fixes the
panel label and cache keys without changing inference code. The corrected CI is
https://github.com/mpuchara/HomeMind-HA/actions/runs/40419243101.

Seven new regressions cover selective lookup without index traversal, snapshot
isolation under concurrent routing/revision changes, independent retry routing,
timer clock isolation, bounded percentiles and anonymous trusted HTTP export.
The existing full suite covers Control, Correct, generations, discard, promotion,
rollback, manual hold, HA dispatch/ACK, restart and storage concurrency.

## BENCHMARK RESULTS

`docs/benchmarks/PERFORMANCE_0_14_230_SCALING.json` contains 7 scheduler and 14
runtime configurations, 5 paired series each, alternating execution order.
658-file source manifest: `PERFORMANCE_0_14_230_SOURCE_HASHES.json`.
Raw samples are retained in `PERFORMANCE_0_14_230_RAW.json.gz`.

Scheduler p95 at 20 agents: 0.977 -> 0.299 ms on the host, identical passed
snapshot SHA in every pair. Whole-runtime event-to-decision p95 is mixed:
5 shared agents 47.03 -> 36.83 ms, 4 shared 34.24 -> 40.91 ms, 4 independent
26.81 -> 35.22 ms, 20 shared 149.84 -> 154.32 ms, 20 independent 116.65 -> 113.45 ms.
All 70 runtime pairs preserve Root decision SHA and make zero HA service calls.
These are cold-inclusive synthetic burst workloads, with simple trained policies
that chose OFF/SHADOW in this matrix; not a policy-quality or physical ON/OFF test.
No claim of uniform whole-runtime p95/p99 improvement or Pi speedup is supported.

## KNOWN ISSUES

- Whole-Root no-regression performance gate remains open. Longer warmed paired
  runs and profiles must investigate the 4-agent result before broad performance
  acceptance. Do not dismiss it merely because the scheduler got faster.
- Detailed phase CPU, SQLite VM/planner/lock/transaction/checkpoint profiling,
  active Candidate, concurrent isolated training/Correct, slow writer and sustained
  2/4/10 Hz + 500/5000 bursts remain incomplete A0 work.
- Telemetry retains 512 samples; p99 there is explicitly insufficient. Pooled
  samples across agents are correlated; the runner's 1000-sample flag is only a
  heuristic and does not establish statistical or hardware acceptance.
- The observer uses normal sampling/backpressure; shutdown may drop optional RAM
  jobs. Root decision parity is not full Tournament evidence parity.
- No 24-hour soak, installed Pi 4, real HA socket/dispatch/state-ACK acceptance.
- Local Docker daemon was unavailable. Check QA for the actual CI Docker evidence.
- Three separately named audit documents from the prompt were not found locally;
  the pasted task was the audit reference used here.

## UNFINISHED WORK / NEXT STAGE

Close the remaining A0 measurement gaps and explain mixed Root latency, then A1:
profile capture history/entities/forecast/policy/group accounting; move only work
whose causal immutable contract is proved. Preserve exact model generation,
event_time/received_time, causal cutoffs, topology/schema revisions and queue RAM
charges. Do not move a live reference into a worker or rebuild a past frame using
newer states. Do not remove sync_required promotion/probation behavior.

After A1: A2a remaining run/process state copies; A2b cross-agent immutable sensor
descriptors only if profiling supports them; A3 measured SQLite/WAL contention;
A4 bounded optional ingress and durable command distinction; A5 existing training
caches/Correct; A6 phased maintenance and soak; A7 target acceptance/package QA.

## NEXT FILES TO MODIFY

- tools/benchmark_runtime_scaling.py, tools/compare_runtime_scaling.py
- adaptive_ai/src/tournament_observation_queue.py
- adaptive_ai/src/temporal_observation_snapshots.py
- adaptive_ai/src/detached_mapping_snapshots.py
- adaptive_ai/src/context_tournament.py
- adaptive_ai/src/telemetry.py, runtime_debug_log.py, engine.py

## NEXT TESTS TO RUN

1. Fetch current remote HEAD and inspect #346/#345 status. Do not redo the .229
   shared protection update optimization. Do not lose the stacked base changes.
2. Add warmed long-run profiles for the mixed 4/5/20-agent configurations, retaining
   cold samples separately. Compare tracing off/on. Add real active Candidate and
   isolated training/Correct fixtures without forcing confidence or model outputs.
3. For A1 prove full observation parity, target edge FIFO, explicit overload gaps,
   no future-state reads, model/config/generation invalidation, promotion/rollback,
   zero observer HA calls and bounded pending+active charges.
4. Run relevant existing .172/.217 scheduler, .229 sharing, deferred Tournament,
   Candidate/Correct/generation/promotion/rollback tests, then complete suite,
   compileall, JS, shipped entrypoint and Docker build/start on exact source.
5. Bump version, add a separate PR based on the technically validated predecessor,
   source manifest, report, continuation and committed-source install packages.

## PI 4 / LONG-RUN PROCEDURE (NOT EXECUTED)

Back up first and use the same addon identity/config/data. Install manually only
when the operator elects to test. Capture version/source, Pi model/RAM, CPU load,
storage media/free space, WAL sizes, active agent/model generations and sensor
topology. Establish the prior version under identical input workload, then .230.
Warm models before steady-state timing; retain cold startup separately. Exercise
0/1/3/4/5/10/20 agents, shared/independent sensors, then Candidate, training and
Correct. Test bursts/rates and writer contention on disposable copies/fixtures,
never corrupt or auto-repair the production database.

For a real 24h soak, record timestamped rolling latency percentiles/counts, CPU,
RSS/GC, WAL, queue depths/oldest age, failures/gaps and recovery. Keep detailed
tracing off in the baseline, measure its overhead separately, and collect short
bounded traces around spikes. Preserve logs across controlled restart; RAM-only
evidence is lost. Verify persisted corrections/models/generations and resume
progress. Separately exercise Control dispatch and matching observed state ACK
with manual override and Executor guards. Report actual elapsed duration; do not
substitute this procedure, CI or host fixtures for a completed Pi/24h test.

## INSTALLATION PACKAGE STATUS

Final receipt: `docs/benchmarks/PERFORMANCE_0_14_230_QA.json` and local `dist/`.
Build only after committing all intended content:

```sh
python tools/package_install_release.py
```

Expected `dist/HomeMind-HA-0.14.230-repository.zip` and
`dist/HomeMind-HA-0.14.230-addon.zip`, with .sha256 sidecars. SOURCE_COMMIT.txt
must equal the packaged HEAD. Verify every source-manifest file against that
commit. The release report contains backup, source replacement, image rebuild,
restart, verification and rollback instructions. No model Rebuild is required.
This is an experimental manually installable candidate, not a stable publication
or evidence that the whole-product performance acceptance gate is closed.
