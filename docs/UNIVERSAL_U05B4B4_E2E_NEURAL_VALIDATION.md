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

## Benchmark B and regression evidence

Controlled runtime A/A and a fresh full local suite are in progress. Their
final measurements and CI identities will be recorded in the handoff before
this stage is claimed complete. Earlier/adverse observations are retained
separately and are not substituted for controlled current-code results.

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
