# U05b.4b.2 handoff

## Branch and scope

Branch `codex/universal-u05b4b2-affected-sensors` starts at exact Draft PR #361
HEAD `1b298554490a629cbc00d024c3879659bbfeb253`. Its full CI
[41613749739](https://github.com/mpuchara/HomeMind-HA/actions/runs/41613749739)
was verified successful before changes. The initial worktree was clean.
Reference remains 0.14.232 / `f7384ba5476f26155ecb138034cc3350fa5bfda2`.
Keep Draft dependencies #355–361. No main merge, version/package, installation,
production enablement or automatic neural training/promotion is part of this work.

Implementation commit: `b2d611b71e2cf62a9a1ada773aba1f5b6f43cdcd`.
[Draft PR #362](https://github.com/mpuchara/HomeMind-HA/pull/362) targets
`codex/universal-u05b4b-causal-evidence`. The follow-up records measurements,
documentation and benchmark database-size reporting, not runtime changes.
Implemented: immutable scoped operator declarations, hypothesis/
partial/unknown/empty distinction, integration in the original predecision
snapshot/revision contract, exact ActionIntent binding, native collector receipts
and HistoricalDecision v2/U05a verification. Read
`UNIVERSAL_U05B4B2_AFFECTED_SENSORS.md` for the complete API and trust scope.

## Validation

Exact-code focused Linux suite: **15 tests / 9.752 s / OK**, including malformed
approval/configuration/topology clocks, topology-time binding and downgrade
rejection. Earlier complete Universal subset: **219 tests / 83.536 s / OK**;
the exact final implementation is covered by the full CI below. Existing
predecision **14** and collector **26** regressions passed during implementation.
Native integration covers original received events, original masks/ActionIntent,
committed operator declarations, native FeatureJournal/Correct/provenance,
collector -> NativeDatasetSource -> U05a TrainingDataset and real Undo. Positive
tests do not override receipt timestamps. Negative corruption tests deliberately
alter exported fields/clocks and must reject them.

[Full implementation CI 41651155497](https://github.com/mpuchara/HomeMind-HA/actions/runs/41651155497):
**completed / success**. Python **3.11: 3043 tests / 262.790 s / OK**;
Python **3.13: 3043 tests / 314.897 s / OK**. Image build/smoke and all required
benchmarks passed, including the new original-evidence/native-dataset smoke.
Package skipped. The follow-up has its own automatic CI and must not be confused
with the exact verified implementation revision above.

## Host component results

`docs/benchmarks/UNIVERSAL_U05B4B2_COMPONENT_AA.json` and its four individual
receipts compare exact #361 to this implementation using the SAME harness,
interleaved baseline A/current A/baseline B/current B. WSL Ubuntu/Python
3.12.3/NumPy 2.3.3. All clocks in this native-evidence fixture are real; there
is no timestamp override or synthetic backdating. Operator assertions describe
explicit independent fixture dependencies, not physical truth in a real home.

| Sensors/devices | Eligible #361 A/B | Eligible current A/B | CPU #361 A/B s | CPU current A/B s | Native SQL #361/current |
|---|---:|---:|---:|---:|---:|
| 5/1 | 0/0 | 1/1 | .138/.065 | .071/.066 | 50/57 |
| 50/10 | 0/0 | 10/10 | .089/.089 | .098/.107 | 334/387 |
| 200/30 | 0/0 | 30/30 | .308/.319 | .357/.403 | 964/1119 |
| 1000/200 | 0/0 | 200/200 | 4.290/4.311 | 4.999/5.001 | 6344/7370 |

Complete assertions equal the eligible counts in this fixture. All four runs
have zero collector drops/errors. Preparation quantiles, RAM-only binding
quantiles, CPU/RSS, native SQL count, write latency and SQLite growth are retained
in full. The larger current workload also validates and retains actual samples;
#361 only rejects missing receipts, so total CPU is not an equal-qualification
throughput comparison. At 1000/200: RSS #361 60.29/59.08 MiB, current
85.50/83.79 MiB; SQLite growth 3,665,920 versus 9,261,056 bytes. Write p95
#361 1099/1446 ms versus current 1081/1126 ms. The slower current 200/30 write
case (**3262 ms**) and 50/10 case (**1273 ms**) remain in the receipts.

At 1000/200, current A preparation wall p95 is 304 ms including deliberate
TRAINING_BUDGET waits; RAM bind p95 is .135 ms and asserts zero SQL. One index
lookup reads the latest declaration instead of scanning its history. Database
size reporting is supplemented separately; producer/receipt/dataset limits and
the 8 MiB cache remain in force. Fixture context is sparse (one independent
motion input per decision), while the declared monitored scope covers every
sensor; this does not measure a 200,000-token training dataset.

## Composed runtime comparison

The same extended runtime harness is used for both sources, with exact sensor
counts and 1/10/30/200 real policies/devices. The opt-in Probe measures real
model prediction, SQL operations and lock spans equally on both sides; its
overhead is included. Event-to-intent quantiles are actual latency clocks.
Only the existing comparison fixture's logical event clocks are fixed to make
input/decision hashes comparable; this run creates no complete causal proofs.
The separate component fixture above supplies the positive native evidence.

`UNIVERSAL_U05B4B2_RUNTIME_AA.json`: completed, parity checks passed in **all 20
runs**, 40 measured events after 8 warmup events per run. For each cardinality:
exact identical input/decision hashes across baseline A, disabled, enabled A,
baseline B and enabled B; **zero HA calls**. Model/intent counts are respectively
40/400/1200/8000 in every matching run, with no extra prediction/intent boundary.

| Sensors/devices | #361 p95 A/B ms | Current on p95 A/B ms | Current off p95 ms |
|---|---:|---:|---:|
| 5/1 | 3.14/3.56 | 3.82/3.38 | 2.50 |
| 50/10 | 49.24/31.41 | 35.57/60.53 | 36.99 |
| 200/30 | 123.76/97.44 | 135.77/117.58 | 129.67 |
| 1000/200 | 1382.06/1611.57 | 1720.52/1599.92 | 2023.17 |

At 1000/200: baseline CPU 60.265/67.573 s versus current on 72.336/67.936 s
and disabled 78.811 s. RSS baseline 349.41/343.36 MiB, current on 348.70/350.07
MiB, disabled 344.88 MiB. Pure model prediction wall p95 baseline
1.444/1.602 ms, current on 1.460/1.436 ms; this is distinct from queued
event-to-intent. Observed SQL statement counts baseline 34581/36523,
current on 37768/38150; full per-operation probes retain counts and truncation
information. Model probe storage is bounded to 4096 samples/operation, while
intent latency retains all measured 8000 samples in this case.

The slower 50/10 enabled B, 200/30 enabled A and 1000/200 enabled A are retained.
**Do not claim no runtime regression or uniform speedup**: timing/CPU spread is
material, even the disabled case is slow in the largest series, and equal
instrumentation is not bare-runtime or Pi4 acceptance. Only semantic parity and
zero additional HA actions are established by these runs and the real Engine
regression. Forty events do not establish a production p99 certificate. All
p50/p95/p99, prediction, CPU/RSS, SQL/cache/probe and writer metrics are in JSON.

Busy collection loses receipts under sustained larger loads: baseline/current
200/30 drops **1104/1065**, 1000/200 drops **9141/9335** (same in both A/B
repeats). These are mixed collector receipts, not a measurement of HA socket
frame loss. Global coverage gaps prevent qualification rather than reconstructing
data. The busy fixture has no operator declarations and no prepared complete
evidence, so its eligible count is **zero**. Positive qualification is measured
separately by the native component fixture. This is not an active-preparation
contention/soak gate or a guarantee of dispatched-Control evidence yield.

## Database sizes and retained initial evidence

The additional reporting-only size pass uses the same exact-cardinality native
fixture and reproduces current 1/10/30/200 eligibility versus predecessor zero:

| Sensors/devices | #361 final bytes | Current final bytes |
|---|---:|---:|
| 5/1 | 512000 | 536576 |
| 50/10 | 593920 | 770048 |
| 200/30 | 937984 | 1433600 |
| 1000/200 | 5120000 | 11018240 |

`UNIVERSAL_U05B4B2_DATABASE_SIZES.json` and the two individual reports retain
initial/final SQLite-family sizes and growth. At 1000/200 the initial sizes are
1,454,080/1,757,184 bytes; final size includes durable declarations and receipts.
The separate pass adds reporting fields only; no runtime/contract code changed.
`UNIVERSAL_U05B4B2_COMPONENT_INITIAL_CONTENDED.json` preserves the initial
component run overlapping the 200-device runtime smoke; it is not represented
as isolated A/A performance. Full native/composed raw logs remain locally under
`.devtools/u05b4b2-bench-logs/`; checked-in JSON retains every A/A case, adverse
quantiles, errors/losses, qualification count and instrumentation bounds.

Reproduce on Linux with the same pinned Python/NumPy environment:

```sh
python tools/benchmark_universal_affected_sensors.py --output component.json
python tools/benchmark_universal_affected_sensors.py --source-root BASE_361 --output base.json
python tools/benchmark_universal_affected_impact.py --baseline-source BASE_361 \
  --output runtime.json --log-dir runtime-logs --events 40 --warmup 8
```

`BASE_361` must be exact commit `1b298554490a629cbc00d024c3879659bbfeb253`.
No real HA service, neural trainer, Candidate change or model promotion occurs.

## Limits and continuation to .3

* No automatic causal inference from names, rooms, device membership or routing.
  Unknown/partial declarations remain ineligible; explicit empty requires an
  operator-scoped assertion and reason. Operator trust is not a physical proof.
* New operator declarations cannot repair old missing evidence. Stored historical
  assertions are not rewritten by later changes. Retracting an incorrect past
  physical assertion would need an explicit historical revocation policy.
* Generation/config/device/topology changes require a matching new declaration.
  Exact topology receipt ids include time, so a registry refresh/restart can
  also require reapproval even if the physical structure appears unchanged.
  Durable revision checks deliberately reject even unrelated covered writes,
  or changes after a decision but before persistence. Sustained activity may
  sacrifice evidence coverage; control is never blocked to improve yield.
  In particular, a successful Control dispatch can mutate leases/resource rows
  before persistence and conservatively invalidate that prepared snapshot. The
  fixture yield is not evidence of dispatched-Control training-data yield.
* Generic number/select review limitations from .1 remain; power and illuminance
  training exclusions remain. No validated neural artifact or Pi4 acceptance is
  claimed here. Full validated-artifact integration belongs to .4.
* Begin .3 from this Draft HEAD after verifying its live CI. Keep FeatureJournal
  as the sole sensor history and ReplayCoverage/native witness checks intact.
  Implement sensor/window/topology-specific versioned retention witnesses with
  atomic invalidation in native deletion transactions, including restarts and
  disabled collection. Unverifiable older witnesses must fail closed.
  V2 currently matches the declaration's topology-universe hash against the
  full ReplayCoverage entity set. A granular coverage version must separately
  attest the historical topology universe and the retained required-sensor
  history; never claim complete unrelated history just to satisfy that check.
* Retention must not consult current HA to refill missing history or reinterpret
  scoped operator assertions. Preserve both evidence versions, exact original
  decision binding, supervision provenance, deduplication and Undo.
