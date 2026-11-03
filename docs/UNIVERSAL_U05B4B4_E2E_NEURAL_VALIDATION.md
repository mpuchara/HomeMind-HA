# U05b.4b.4: Original V2 evidence to validated offline neural artifact

## Scope and source

This continues the interrupted work on
`codex/universal-u05b4b4-e2e-neural-validation`, based on exact PR #363 HEAD
`e797439989dba56ca2ccf3d37cf1b3521c501b1c`. Implementation commit:
`e6d13526f0bf30ab82d35483d24502c9bd5443eb`. Production reference remains
0.14.232 / `f7384ba5476f26155ecb138034cc3350fa5bfda2`.

The disposable synthetic producer runs the existing Engine, DeviceAgent,
ActionIntent, FeatureJournal, PredecisionEvidence, approved affected-sensor
declarations, HistoricalTrainingCollector, native provenance and Correct
journal. It never constructs a HistoricalDecision V1 or rewrites persisted
timestamps. Original V2 receipts and native ReplayCoverage V3 pass through
NativeDatasetSource/U05a into the existing shared scorer trainer, quality gate
and checkpoint lifecycle. HA service dispatch is forbidden in the fixture.

The existing U03 encoder is frozen. Scorer training uses parent seed 1,
encoder seed 41 and TrainingConfig(epochs=24), fixed before gate evaluation.
The default ValidationPolicy is unchanged. Training and future holdout are
chronologically separated, with distinct synthetic episode identities. Only
the training partition fits weights and normalization. The real lifecycle
durably reserves the holdout before evaluating its labels and atomically
publishes a sealed artifact after rechecking native evidence.

These synthetic trials assume independence by construction. Their repeatable
holdouts are integration/regression fixtures, including earlier smoke runs;
reruns are NOT new independent statistical evidence, and are not household
acceptance. A disposable database does not make an exposed scenario new data.
Seeds/configuration must not be tuned using these published holdout results.

## Changes and trust boundary

* `universal_history_collector.py`: bounded native receipt/provenance and
  historical registry recheck for admitted V3 samples. Exported JSON with
  recomputed public checksums cannot substitute an original mask or registry.
  Checks run in the caller's consistent snapshot and again inside the
  lifecycle acceptance writer transaction. Batches are 64 sample clocks,
  with a 16 MiB native body budget and cooperative training-budget checkpoints.
* `universal_checkpoint_lifecycle.py`: invoke the native checks during dataset
  preparation and guarded acceptance; reject an attempt interrupted by
  recovery while validation was running. Holdout reservations remain consumed.
* `tools/universal_e2e_fixture.py`: disposable original-event producer with
  monotonic producer clocks, explicit simulated declarations and real Correct
  and Undo contracts. No production import or automatic training hook.
* `tests/test_universal_e2e_validation.py`: 24 integration/boundary tests.
* `tools/benchmark_universal_e2e.py`: all four sensor/device scales, actual
  weight updates, SQL/CPU/RSS/database and lifecycle stage measurements.
* `tools/benchmark_universal_affected_impact.py`: preserve the existing five-way
  A/A harness and expose Candidate/training/Correct switches plus event-bound
  ActionIntent counts/parity. Event-to-intent telemetry is emitted after the
  original Engine creates an intent; blocked decisions remain blocked.
* CI: add the original V2/V3 lifecycle smoke for Python 3.11, retaining the
  existing full Python 3.11/3.13 and image checks.

No Engine, DeviceAgent, Executor, Candidate Manager, power/lux filtering,
resource/lease/manual override, release version or default collector setting
changes. Models stay offline with control_eligible=false and
promotion_eligible=false. No automatic trainer or promotion is installed.

## Integration coverage

Tests verify real weight changes, unchanged parent and frozen encoder,
dataset/supervision/contract identities, future-label isolation, original
mask/declaration/intent/provenance binding, real passed and failed quality
reports, insufficient evidence, durable holdout consumption, atomic publish,
guarded restart/load/inference, corrupt checkpoint/seal rejection, Undo before
training/during training/after validation/after publication, and required
history loss at those boundaries. Unrelated retention preserves qualified
history. A later configuration/declaration change does not rewrite valid
history; a change between predecision preparation and prediction invalidates
that prepared evidence. Mapping, mask, generation, lease, manual hold and
topology changes also fail closed. Disconnect/restart cannot extend coverage.

## Benchmark A: controlled host lifecycle

[Full measurements](benchmarks/u05b4b4-e2e-controlled.json), WSL Linux x86_64,
Python 3.12.3 / NumPy 2.3.3, sequential on an isolated source snapshot. No local
regression or other benchmark was run concurrently. The host remained an
ordinary desktop, not a dedicated performance appliance. All devices enter
each experiment; 1000/200 has only one trial per device, so some devices are
absent from train or future holdout. They are not silently removed from gate
coverage requirements. Background sensors are registered and seeded; this is
a sparse event producer, not 1000 high-rate sensor streams.
The lifecycle cases run sequentially in one process, so later RSS includes
allocator/module caches. The sparse offline fixture disables only the
interactive foreground grace interval in its temporary OPTIONS patch; it
does not change production defaults or composed-runtime benchmark options.

| Sensors/devices | Original decisions | Eligible/rejected | Actual updates | Train/holdout | Quality Gate / publication |
|---|---:|---:|---:|---:|---|
| 5/1 | 128 | 128/0 | 264 | 85/43 | passed / published |
| 50/10 | 128 | 128/0 | 264 | 85/43 | insufficient_evidence / none |
| 200/30 | 128 | 128/0 | 264 | 85/43 | insufficient_evidence / none |
| 1000/200 | 200 | 200/0 | 408 | 133/67 | insufficient_evidence / none |

All rejected lists are empty; no rejection reason is suppressed. Training
completed at every scale. Gate rejection is
`missing_independent_device_property_coverage` for the three larger scales,
requiring respectively 48/57/72 independent units per device/property slice.
The 2048 source cap cannot supply 72 future units for each of 200 devices.
Technical integration, optimization and statistical qualification are separate.

| Sensors/devices | Dataset ms | Training ms | Gate ms | Publish attempt total ms | DB family bytes |
|---|---:|---:|---:|---:|---:|
| 5/1 | 287.97 | 671.35 | 382.79 | 775.48 | 7,745,424 |
| 50/10 | 287.51 | 626.59 | 250.94 | 666.98 | 7,724,848 |
| 200/30 | 659.03 | 1454.48 | 21.32 | 590.85 | 8,527,688 |
| 1000/200 | 1092.96 | 1529.91 | 27.63 | 1273.27 | 15,724,528 |

Gate timing is nested in the publication attempt, not additive. The successful
5/1 artifact guarded load took 361.23 ms and guarded offline inference 950.55 ms;
the latter includes native evidence revalidation, not just neural prediction.
There is no artifact/load/inference for an insufficient cohort. The published
artifact belongs only to the disposable test Store and is deleted with it.
The report retains artifact identity/size and its guarded inference projection;
no reusable production artifact or installed model is delivered.

Raw JSON contains CPU and before/after RSS per stage, source/original receipt
sizes, SQLite family sizes and instrumented SQL execution counts, collector
diagnostics, identities, policies and full gate reports. Probe setup and direct
in-memory arbiter-copy SQL are excluded from SQL totals, explicitly marked.
Counts measure instrumented execute/executemany calls, not SQLite VM opcodes.
No Pi4, physical HA, Recorder or production training-yield claim is made.

## Regression and CI evidence

Fresh full WSL regression on exact implementation e6d13526:
**3100 tests / 427.301 s / OK / exit 0**. The 720 source/test/CI files are
checked against their Git blob identities after CRLF normalization; the root
test report input has its own SHA-256. Full source and process records are in
`benchmarks/u05b4b4-validation-record.json`. The earlier fresh run executed
3100 cases / 418.766 s / exit 1 because this continuation's copied snapshot
omitted `BENCHMARK_PRODUCT_F24.md`: 3099 passed, one FileNotFoundError. Its log
and failed status remain recorded; restoring the input required no code fix.

[CI 41710898166](https://github.com/mpuchara/HomeMind-HA/actions/runs/41710898166)
on bda4aa36 passed Python 3.11 (3100 / 324.892 s), Python 3.13
(3100 / 339.169 s), image and every required benchmark. The benchmark-only
follow-up 1a3a79ab has its own successful
[CI 41714102847](https://github.com/mpuchara/HomeMind-HA/actions/runs/41714102847):
Python 3.11 **3100 / 313.223 s**, Python 3.13 **3100 / 339.737 s**, image and
every required benchmark passed. Reporting snapshot HEAD
`e8acaf99301be22605cc28e71813923d0f97035f` also has successful
[CI 41714791110](https://github.com/mpuchara/HomeMind-HA/actions/runs/41714791110).
Package was skipped on these Draft branch runs. Job/step records are retained
alongside the benchmarks. Final reporting-only commits have their own
automatic CI; no untested executable changes are hidden in them.

## Benchmark B: composed runtime

The initial sweep used implementation e6d13526 and the exact clean #363 source,
with 40 events / 8 warmup, active Candidates, isolated concurrent policy
training and real Correct/Undo calls. The first three scales completed all
five variants. At 1000/200, baseline A and current-off completed; current A
hit the original 30-second observation deadline with 169/200 agents seen.
A separate original-deadline control also failed baseline A at 176/200.
Neither failure overlapped full regression. Their cause remains unresolved;
the evidence does not assign them to a particular implementation change.

Both failed attempts and completed partial measurements are retained in
[adverse summary](benchmarks/u05b4b4-runtime-adverse-summary.json), its lossless
raw probe archive `u05b4b4-runtime-adverse.json.gz`, and
`u05b4b4-runtime-incomplete-aa.json`. The original uncompressed report and
stdout/stderr remain local. No failed run is relabelled passed.

Benchmark-only commit `1a3a79abec6b8ec18bb1452ea6830d3ff8be87d3` exposes an
explicit bounded observation deadline (30..120 seconds, default still 30),
reports warmup quantiles, and identifies the step/phase on deadline failure.
This changes only the test harness's wait, not runtime gates or scheduling.
A separate 5/1 five-way smoke (12 events / 4 warmup) passes with 120 seconds.
After the successful full local suite, the full five-way 1000/200 observation
uses the same new harness and **120-second** deadline for baseline and current.
This diagnostic does not certify a 30-second response bound. The smaller
scale results retain their original, completed 30-second observations.

[Combined four-scale summary](benchmarks/UNIVERSAL_U05B4B4_SUMMARY.json) retains every completed variant, source/CI records and raw-log hashes. The separate largest report is `u05b4b4-runtime-1000-expanded-aa.json`.

All four groups have identical input and decision hashes, matching event-bound ActionIntent counts (40/400/1200/8000 per variant), and zero HA calls. Queue/drop counts and both adverse attempts remain explicit. The full 1000/200 group completed with exit 0 and parity=true; this is an expanded-observation result, not a default-deadline pass.

| Sensors/devices | Variant | Event p50/p95/p99 ms | Prediction p95 ms | CPU ms | RSS MiB | SQL calls | Intents | Drops | Write p95 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 5/1 | baseline-on-A | 2.22/8.19/14.54 | 0.178 | 1679 | 81.8 | 2208 | 40 | 0 | 2000.58 |
| 5/1 | current-off | 2.09/6.22/6.50 | 0.182 | 1364 | 70.9 | 2170 | 40 | - | - |
| 5/1 | current-on-A | 3.11/6.10/6.73 | 0.170 | 1634 | 73.0 | 2221 | 40 | 0 | 1935.16 |
| 5/1 | baseline-on-B | 3.02/6.52/7.08 | 0.198 | 1577 | 72.7 | 2244 | 40 | 0 | 1872.18 |
| 5/1 | current-on-B | 2.80/6.45/6.85 | 0.378 | 1442 | 72.9 | 2169 | 40 | 0 | 1330.26 |
| 50/10 | baseline-on-A | 19.52/42.31/50.83 | 0.277 | 3146 | 101.0 | 3878 | 400 | 0 | 3513.52 |
| 50/10 | current-off | 22.12/49.32/60.01 | 0.778 | 3263 | 98.9 | 4114 | 400 | - | - |
| 50/10 | current-on-A | 22.08/48.17/61.69 | 0.356 | 3222 | 103.0 | 3959 | 400 | 0 | 3094.00 |
| 50/10 | baseline-on-B | 19.58/42.54/51.87 | 0.301 | 3265 | 102.2 | 3822 | 400 | 0 | 3120.90 |
| 50/10 | current-on-B | 20.57/43.02/54.38 | 0.280 | 3444 | 102.5 | 3950 | 400 | 0 | 3261.96 |
| 200/30 | baseline-on-A | 70.21/148.00/179.20 | 0.455 | 8908 | 155.1 | 8958 | 1200 | 1065 | 2462.45 |
| 200/30 | current-off | 74.71/160.63/194.00 | 0.448 | 9242 | 155.4 | 9248 | 1200 | - | - |
| 200/30 | current-on-A | 100.51/247.35/342.54 | 0.927 | 14390 | 157.3 | 8717 | 1200 | 1065 | 2579.98 |
| 200/30 | baseline-on-B | 72.37/148.65/183.09 | 0.419 | 9379 | 161.0 | 9161 | 1200 | 1065 | 2055.86 |
| 200/30 | current-on-B | 68.42/160.46/185.28 | 0.375 | 9119 | 156.5 | 8924 | 1200 | 1065 | 2498.42 |
| 1000/200 | baseline-on-A | 1312.98/2607.83/3465.64 | 0.795 | 122730 | 509.5 | 63401 | 8000 | 9335 | 3951.57 |
| 1000/200 | current-off | 1271.38/2235.82/2464.07 | 0.767 | 113177 | 502.7 | 62528 | 8000 | - | - |
| 1000/200 | current-on-A | 1162.84/2097.53/2347.94 | 0.855 | 108885 | 509.0 | 63207 | 8000 | 9335 | 3787.92 |
| 1000/200 | baseline-on-B | 1174.61/2056.13/2247.83 | 0.797 | 107946 | 509.4 | 61632 | 8000 | 9335 | 3589.62 |
| 1000/200 | current-on-B | 1223.39/2179.43/2700.65 | 0.956 | 112117 | 507.9 | 62898 | 8000 | 9335 | 3788.99 |

The 200/30 current A p95 (247.35 ms) and CPU (14,390 ms) exceed both baseline runs and current B (160.46 ms / 9,119 ms). This adverse spread is retained, not averaged away. At 1000/200, measured event p95 spans 2056.13–2607.83 ms across enabled variants; optional collector drops are 1065 at 200/30 and 9335 at 1000/200 in every enabled run. Queue peaks, bytes, write p95/max, foreground deferrals and error counters are in JSON.

**Largest-scale warmup is much slower:**

| Variant | Warmup p95 ms | Warmup p99 ms | Warmup maximum ms |
|---|---:|---:|---:|
| baseline-on-A | 23690.44 | 29692.87 | 30524.74 |
| current-off | 21744.32 | 31510.13 | 32336.62 |
| current-on-A | 22511.18 | 31833.50 | 33133.28 |
| baseline-on-B | 20858.60 | 32215.93 | 33429.98 |
| current-on-B | 21707.48 | 32432.05 | 33564.60 |

Warmup maxima are 30.52–33.56 seconds. These observed pipeline tails explain why a 30-second observation bound cannot be certified; a precise cause of the earlier two deadline failures remains unresolved. The 120-second wait exposes these delays and does not improve them. Warmup and measured event metrics have different boundaries and are not interchangeable.

Runtime variants use fresh processes. Model probe quantiles use a bounded
4096-sample tail; total count and discarded probe samples remain explicit in
JSON. Event-to-intent metrics retain up to 100,000 samples separately. These
are 40 synthetic event steps with many correlated device responses, not a
production p99 certificate. CPU is the measured composed runtime interval;
fixture demonstration fitting is outside it. Nested CPU/probe spans are not
additive. Instrumented SQL counts are execution calls, not VM opcodes.
Busy runtime fixtures lack operator affected-sensor declarations and have
zero training eligibility; their collector gaps remain fail-closed. The
complete native training path is measured separately in Benchmark A.

No local regression or separate benchmark ran concurrently with any timed
sweep. The isolated background policy trainer is intentional workload inside
each variant. This remains an ordinary WSL desktop observation, with timing
spread, queue saturation and dropped optional collector writes. No claim of
absence of regression, Pi4 acceptance, real HA dispatch/ACK timing or physical
learning efficacy is made.

## Reproduction

Use an offline disposable workspace with NumPy 2.3.3, one benchmark at a time.
Preserve previous output paths and choose new names for every rerun. Existing
baseline/current sources must be exact detached snapshots; the current harness
is intentionally shared across both revisions.

```bash
python tools/benchmark_universal_e2e.py --output /new-results/e2e.json
python tools/benchmark_universal_affected_impact.py \
  --baseline-source /snapshots/exact-pr363 \
  --baseline-label 'PR #363 / e797439989dba56ca2ccf3d37cf1b3521c501b1c' \
  --candidates --training --correct \
  --output /new-results/runtime-aa.json --log-dir /new-results/runtime-logs
python -m unittest discover -s tests -v
```

Never point the synthetic producer at a live HA/Recorder database. It requires
empty disposable native tables. Do not run the full regression concurrently
with controlled performance measurements. The runtime benchmark's background
training is deliberate workload inside every variant, not external contention.
