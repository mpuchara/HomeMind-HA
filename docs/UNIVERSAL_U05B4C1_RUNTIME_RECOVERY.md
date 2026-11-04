# U05b.4c.1 runtime recovery

The tested runtime is `8036a2bd64dba32490d784df2029c0818ad3edbf` in Draft #366,
stacked on #365 at `613e2e2638df6ac58b873bbcc2a93d78fd63a8c2`. The comparison is
#364 at `b3a67c0a2d264697ac69f5ee16bdd673b843f6d3`. Production reference
0.14.232 is `f7384ba5476f26155ecb138034cc3350fa5bfda2`.

| Gate | Result | Evidence |
| --- | --- | --- |
| A · Functional safety | PASS · controlled host | Completed comparisons match input and steady decision signatures at each scale, with zero HA calls. The fixture emits Shadow intents and exercises Correct/Undo. The full suite covers normal dispatch and control guards. |
| B · Warmup | PASS · controlled host | All final current runs completed all expected agent observations with the original 30 s per-observation deadline. Largest max was 16.133 s without probes and 25.697 s with probes. |
| C · Collector durability | PASS · controlled host | Zero historical collector drops/errors for current. Physical SQLite counts match 48 observations and 48 decisions per device in the probed matrix on 42f14bb1 and in both exact-final 1000/200 confirmations. Fault tests cover fail-closed gaps and retries. |
| D · Steady-state performance | **UNRESOLVED** | Two 1000/200 unprobed pairs improve and two regress, for both p95 and p99. Concurrent work differs substantially. |
| E · End-to-end neural evidence | PASS · synthetic host | Original V2 evidence, V3 coverage and U05a qualify synthetic samples at all four scales. The smallest cohort passes the default gate and publishes for guarded offline use. Larger cohorts return insufficient evidence. |
| F · Hardware acceptance | NOT TESTED | Raspberry Pi and real Home Assistant. |

**Overall remains open on Gate D.** The evidence does not support a steady-state
improvement claim or a new production scheduler change. The existing fixes cover
Shadow schema validation without routine DDL, batched scheduler capacity deferrals,
completed futures, bounded collector draining, and retry/reopen on writer startup.

## 1000 sensors and 200 devices without added probes

Each side uses 8 warmup steps and 40 steady steps with Candidate, concurrent
training and Correct/Undo. Each cell is baseline → current. All steady
percentiles use 8,000 samples per run. Warmup is max event-to-pipeline
observation across 1,600 warmup samples. CPU is steady process CPU, which can
exceed elapsed wall time. Peak RSS is process lifetime high water.

| Pair | Warmup max s | p50 ms | p95 ms | p99 ms | Max ms | CPU s | Peak RSS MiB | Updates / model writes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A | 31.941 → 14.421 | 745 → 674 | 1473 → 1329 | 1909 → 1574 | 2201 → 1743 | 79.0 → 71.8 | 475.2 → 472.2 | 5,758 / 180 → 3,489 / 110 |
| B | 32.389 → 16.133 | 690 → 751 | 1334 → 1616 | 1478 → 2104 | 1606 → 2347 | 71.9 → 82.8 | 474.1 → 469.8 | 5,657 / 177 → 3,969 / 125 |
| C | 31.510 → 10.346 | 663 → 618 | 1306 → 1246 | 1477 → 1422 | 1676 → 1649 | 71.3 → 67.1 | 469.7 → 464.6 | 5,281 / 166 → 3,470 / 109 |
| D | 21.964 → 14.614 | 632 → 662 | 1204 → 1268 | 1319 → 1493 | 1444 → 1657 | 67.1 → 69.1 | 464.0 → 471.7 | 4,897 / 154 → 3,720 / 117 |

All four baseline runs here had a 120 s observation deadline and are diagnostic.
Current kept 30 s. Under the original deadline, the separate probed baseline A
timed out at 172/200 agents in its first observation. Probed baseline B
completed with a 20.566 s warmup max. There is no steady distribution for the
timed-out attempt. Both sources use the same configured workload and decision
signature. Source manifests, the full hashes, CPU/RSS, SQLite and lock metrics,
per-phase samples, status and exits are in the
[recovery summary](benchmarks/UNIVERSAL_U05B4C1_RECOVERY_SUMMARY.json).

For every large unprobed pair, the collector reported 313 writes and 9,335
drops on baseline versus 9,648 writes and zero drops on current. Those runs did
not independently query physical counts. Both final probed current runs did
and found 9,600 decision rows and 48 observation rows, zero mask rows and zero
coverage segments. One recorded coverage gap was present. The overload fixture
has no qualifying topology and affected-sensor contract. Its separate
Candidate/Tournament queues still report evidence drops under load. The
historical collector's zero-drop result applies to its own queue.

## Where the p95 moved

The p95 deltas across A–D were approximately −144, +282, −60 and +64 ms.
The p99 deltas were −334, +626, −55 and +174 ms. In B, p95 event-to-target-start
moved from 1,327 to 1,611 ms while inference p95 moved from 3.34 to 4.11 ms.
This locates most of the increase upstream of model execution. It does not
separate scheduler admission, worker availability and shared-resource waits.

Current B used 82.8 process CPU seconds and completed 3,969 training updates
with 125 model writes, the highest of the four current runs. Baseline B still
did more training work than current B (5,657 updates, 177 writes). Training
counts span warmup plus steady and can rise because a run took longer.
Candidate depth peaked at 32 in every large unprobed run. In B, Candidate
processed 301 → 257 and its drops were 9,275 → 9,315. Those counters span the
run and do not measure instantaneous competing jobs.

The built-in context-row Store wait p95 for B fell from 781 to 254 ms and hold
p95 from 72 to 64 ms. This is one writer family. Current had 1,593 SQLite
deferrals, the highest current count, but its historical collector peak depth
(354) was below current A (409). WAL checkpoint busy results were zero in
all eight unprobed cases. The slow B result does not consistently align with
these contention indicators. Total SQLite API calls, global Store lock waits
and target executor queue times require the separate probes. The two final
probed cases had 56,763 and 56,269 SQL API calls and lock wait p95 and executor
queue samples are retained in their reports. Their steady p95 was 1.488 and
1.570 s. These probe runs cannot provide missing per-event timings for the
unprobed B run.

Cold Store startup component durations are in the per-case diagnostics.
Warmup distributions describe the first eight steps, and steady CPU and
latencies use the post-reset window. A phase-specific training or active-worker
timeline was unavailable. The older runtime-only and collector-only runs each
change one subsystem but had differing training counts. They help localize the
durability improvement and cannot isolate a steady-state p95 cause.

## Original evidence through guarded offline inference

The existing four-scale U05b.4b.4 benchmark ran on the final implementation.
It exercises original HA-style event ingestion and FeatureJournal persistence,
predecision and affected-sensor evidence, original ActionIntent, native
collector and V3 coverage, NativeDatasetSource and U05a, shared neural training,
default Quality Gate, CheckpointLifecycle and offline inference where eligible.

| Sensors / devices | SQLite observations | Original intents | Qualified | Training updates | Publication |
| --- | ---: | ---: | ---: | ---: | --- |
| 5 / 1 | 261 | 128 | 128 | 264 | Published and loaded for guarded offline inference |
| 50 / 10 | 306 | 128 | 128 | 264 | Insufficient evidence |
| 200 / 30 | 456 | 128 | 128 | 264 | Insufficient evidence |
| 1000 / 200 | 1,400 | 200 | 200 | 408 | Insufficient evidence |

The benchmark observed zero HA calls and zero historical collector drops or
errors. The passed artifact cannot control or promote. Separate existing
regression tests cover Undo before, during and after publication, revocation
of previously loaded handles, rollback with the retained V3 proof, required
loss blocking stale publication, and unrelated loss preserving a valid
artifact across restart. All trial independence assumptions are synthetic.
