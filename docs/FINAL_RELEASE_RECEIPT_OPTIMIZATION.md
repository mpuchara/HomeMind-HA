# Receipt writer experiment — 2026-11-06

Gate D remains **BLOCKER** until the complete registered protocol passes.
No deployment, version, cadence, batching, admission or qualification change.

## Frozen identity

- Exact #368 baseline: `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.
- Previous production: `9c245577727c93ede3c59100f11844bf336acb1a`.
- Resume reporting HEAD: `9739874ee69a2bcca0737e88e197311f2055f147`.
- Experimental production: `4dc2d2d630aa8ee144c0e7d8492173c462647a34`.
- Experimental add-on tree: `6cc6c81c21764d9c1b530fbc7cff4bf8ef2295c1`.
- Version: **0.14.232**. Local branch and fetched origin matched; worktree
  was clean, and reporting changes after `9c245577` left `adaptive_ai` unchanged.

## Isolated implementation

The writer already materializes a historical sensor universe for affected-sensor
verification. `validate()` nevertheless repeatedly creates a set, sorts it,
serializes it and computes its SHA-256 for every receipt in the same decision
interval. The experimental `_PreparedSensorUniverse` owns a frozenset and the
checksum of its canonical sorted representation. Every declaration still has
its checksum, JSON contract, complete flag, membership, universe count/checksum,
clocks, topology information and decision identity independently verified.

This is reuse of a pure computation on an owned immutable value. It introduces
no native revision/schema/declaration verification cache. `verify_for_write()`,
`validate_binding()`, native revision SELECT, schema PRAGMA, native `latest()`
and the topology timestamp SELECT still execute independently. The original
native topology lookup/cache is unchanged. Derived inputs do not extend that
cache's lifetime or supply a new authoritative source.

Derived values are local to `_write()`, cleared before every non-decision
metadata item and after receipt flushing. Only consecutive decision verification
can reuse them; its SQL consists of the original native reads. Inserts and their
custom/revision triggers execute in `flush_receipts()`, which clears derived
inputs before any continuation. Snapshot seeding flushes and clears before its
partial commit. DDL, registry/topology/retention metadata work has no new reuse
interval. Exceptions/rollback/return discard the local dictionary. External
writers cannot modify this BEGIN IMMEDIATE interval. Concurrent collector epoch
changes still face the original per-decision epoch check and gap closure.

The general `validate(sensor_universe=list)` path retains its original ordering
and short-circuit rejection behavior. Both persisted JSON serializations,
`HistoricalDecision.capture()`, `asdict()`, outer encoding, hashes, event keys,
retention protection, transaction boundaries and storage admission are unchanged.
The largest serialization operations were not deleted or replaced with a new
receipt format. A general `VerifiedEvidenceTransactionContext` was not added:
native invariance across arbitrary own writes, triggers, schema changes and
partial commits is not established by BEGIN IMMEDIATE alone.

## Correctness and preserved failed setup attempts

Frozen oracles are `tests/reference/collector_write_9739874.py` and
`tests/reference/affected_validate_9739874.py`, extracted from the resume source.
The differential test compares all selected collection and journal tables,
including exact decision JSON bytes/checksums, mask keys and native rows.
Fifteen subcases cover complete and incomplete receipts, invalid binding,
missing mask/declaration, generation/model/epoch/topology changes, duplicates,
intervening metadata, rollback, native revision triggers, DDL and ABA mutations.
A positive assertion requires an actually persisted complete V2 receipt; equality
of two fail-closed outputs alone is insufficient. Pure validation comparisons
check rejection text and immutable ownership.

Early fixture attempts are retained in raw logs. Random new collector sessions
first prevented row equality. More importantly, SQLite backup assigns a fresh
destination schema cookie, so a newly added positive assertion correctly caught
vacuous rejection. The disposable exact-schema copy now restores the source
cookie before the tested DDL changes; Store migration is not rerun on that copy.
Neither workaround changes production verification. A standalone journal run
initially lacked its test import/data bootstrap; its corrected disposable run
passes. These failed invocations are not counted as successful validation.

- Candidate: **295 Universal PASS**, 233.376 s.
- Corrected exact-schema differential: **2 PASS**, 3.065 s, including 15 subcases.
- Native journal differential: **4 PASS**, 2.105 s (external writer exclusion,
  insertion trigger, rollback, exact protected rows/keys).
- Existing Universal coverage also exercises busy/failed writes, partial metadata
  commit and snapshot seeding, restart/concurrent gap invalidation, Correct/Undo,
  changed historical topology/declarations and retention. These are overlapping
  suites, not additive unique counts. The inherited intermittent retention miss
  is not declared resolved by passing this run.

## Measurement protocol

WSL ext4 study: `/home/mpuchara/homemind-receipt-study-20261106`.
Python: `~/homemind-perf-venv/bin/python`. All subprocesses run serially.
An immutable source archive freezes the experimental runtime before profiling,
screening and acceptance. Exact source/tool manifests and the common harness
fingerprint precede the measurements. The previous production arm has the same
add-on tree as reporting `9739874`; the baseline is exact #368.

Profiles are separate from unprofiled runs. The pure validation CPU screen
excludes native work and preparation cost and is explicitly not Gate D. The
unprofiled previous-production screen alternates order twice for 50/10 and twice
for Normal. Registered acceptance preserves all 13 original pairs, 24 ticks,
400 ms, fanout 16, original counts/order, native persistence/background work and
all original margins. Mixed results require the six same-revision controls.
No test/build/other benchmark runs concurrently with these measurements.



## Rejected experiment and restored production

**REJECTED**, with runtime restored by `c1dbfd9fd468a9ea24c114cdaf044fbe5b0d8244`.
The restored add-on tree is `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`, exactly
identical to `9c245577`. The experimental `4dc2d2d6` source remains immutable in
Git and the ext4 study; later reporting/tool changes do not alter that source.
The optimization was reverted once the unprofiled screening failed to demonstrate
repeatable process-CPU savings. Its full registered matrix continued on the
already frozen ext4 source, independently of the Windows worktree restoration.
No timing measurement was run on a moving source.

Pure validation microbenchmarks (six alternating pairs, 2,000 validations per
arm) show reduced CPU in every pair: 43.2–56.7% at 50 sensors, 94.5–96.0% at 1,000.
They exclude native work and preparation of the derived input; they are not
writer or process acceptance. The previous-production screen is the controlling
causal comparison for retaining this isolated optimization:

| Pair | CPU | p95 | p99 | RSS | Consumed | Qualified | Screen |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| small50-1 | -2.92% | -3.55% | -2.30% | +2.02% | 175 -> 175 | 175 -> 175 | PASS |
| small50-2 | +18.15% | +19.48% | +5.40% | -4.14% | 175 -> 175 | 175 -> 175 | FAIL |
| normal-1 | +0.54% | -0.15% | +1.73% | -2.54% | 13 -> 13 | 13 -> 13 | PASS |
| normal-2 | +3.38% | +0.74% | -1.19% | -2.35% | 13 -> 13 | 13 -> 13 | PASS |

All screen pairs preserve original inputs/signatures, native evidence accounting
and zero unauthorized HA calls/errors/drops. Small50 pair 2 has load batches
50 -> 53 and preparation CPU 32.892 -> 50.365 ms, with the same 20 turns and
175 consumed/qualified receipts. Pair 1 has 52 -> 52 load batches and preparation
CPU 45.063 -> 41.515 ms. Both have an empty Busy-work dictionary. Normal screen
load batches are 164 -> 168 and 162 -> 167; preparation CPU is 380.560 -> 389.338 ms
and 352.942 -> 394.529 ms. These real activity differences are recorded, not
normalized away. They do not establish a reproducible environment defect.

## Complete registered Gate D result for the rejected candidate

**11/13 timing screens PASS; Gate D BLOCKER.** Exact #368 is the baseline.
The complete original 26-arm protocol finished with unchanged margins and
workloads; no failed pair was averaged away or discarded.

| Pair | CPU | p95 | p99 | RSS | Consumed | Qualified | Screen |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| normal-1 | +9.64% | +3.28% | +7.88% | +0.55% | 6 -> 11 | 6 -> 11 | PASS |
| normal-2 | +11.11% | +4.57% | +5.74% | -0.46% | 5 -> 13 | 5 -> 13 | PASS |
| normal-3 | +2.21% | -3.79% | -4.46% | -0.32% | 5 -> 13 | 5 -> 13 | PASS |
| normal-4 | +3.60% | -3.35% | -4.27% | -0.67% | 5 -> 13 | 5 -> 13 | PASS |
| normal-5 | +1.32% | -0.77% | -0.61% | -0.43% | 4 -> 13 | 4 -> 13 | PASS |
| normal-6 | +7.33% | -1.93% | -3.81% | +0.00% | 5 -> 13 | 5 -> 13 | PASS |
| busy128 | +1.60% | +4.83% | -1.70% | -1.19% | 7 -> 14 | 6 -> 12 | PASS |
| busy200 | +3.30% | +0.30% | +4.70% | -1.63% | 4 -> 9 | 3 -> 7 | PASS |
| off | +0.50% | -0.82% | -4.42% | -3.79% | 0 -> 0 | 0 -> 0 | PASS |
| incomplete | -5.75% | -6.19% | -8.15% | -2.94% | 0 -> 0 | 0 -> 0 | PASS |
| unsupported | -2.87% | +1.67% | +2.97% | -3.68% | 0 -> 0 | 0 -> 0 | PASS |
| small50 | +16.06% | +9.00% | -5.92% | -2.78% | 100 -> 158 | 100 -> 158 | FAIL |
| small200 | +16.12% | -4.56% | +0.32% | -2.39% | 46 -> 108 | 46 -> 108 | FAIL |

Primary consumed **41 -> 99**, qualified **39 -> 95**: both exceed 2x.
Including the supported small workloads, consumed **187 -> 365** and qualified
**185 -> 361**: both are **below 2x**. Every individual active supported workload
improves, but that does not waive either aggregate or CPU limits. The two failed
CPU margins are 50/10 **+16.057%** and 200/30 **+16.121%** (limit +15%).

All 26 arms have matching fingerprints/original decision signatures and exact
original decision counts, zero HA service calls, zero collector errors/drops and
zero event drops. Native decision/provenance counts and fail-closed controls pass
in every arm. Unsupported 400 remains unqualified with `agent bound`; a passing
unsupported timing screen does not qualify that cardinality.

Failed-workload investigation: 50/10 process CPU is 0.424802 -> 0.493014 s,
load batches 26 -> 50, consumed/qualified 100 -> 158; candidate preparation uses
20 turns / 45.370 ms CPU. 200/30 CPU is 0.697686 -> 0.810162 s, load batches
37 -> 65, consumed/qualified 46 -> 108; candidate preparation uses 30 turns /
120.576 ms CPU. Both workloads have no Busy training jobs. Batch/evidence counts
are real work and are not divided out of the acceptance CPU. Source and harness
integrity checks pass. No independent benchmark-environment defect is established.

## Registered same-revision controls

All six timing screens pass. Both roles execute the exact `4dc2d2d6` source
(the actual receipt commands, not the generic protocol's baseline label, identify
that source). Inputs/signatures and safety accounting match throughout.

| Pair | CPU | p95 | p99 | RSS | Consumed | Qualified | Screen |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| variance-1 | -1.70% | +0.06% | -0.79% | -0.52% | 13 -> 13 | 13 -> 13 | PASS |
| variance-2 | -3.08% | -6.66% | -11.49% | +0.05% | 13 -> 13 | 13 -> 13 | PASS |
| variance-3 | -2.80% | -3.42% | -7.49% | +0.20% | 13 -> 13 | 13 -> 13 | PASS |
| variance-4 | -4.05% | -0.40% | +4.62% | -0.27% | 13 -> 11 | 13 -> 11 | PASS |
| variance-5 | -9.07% | -11.49% | -3.04% | -0.25% | 13 -> 13 | 13 -> 13 | PASS |
| variance-6 | +0.17% | -0.62% | +0.85% | -0.25% | 13 -> 13 | 13 -> 13 | PASS |

Control 4 consumes/qualifies 13 -> 11 despite identical source and input, showing
admission/qualification count variation. These controls neither erase the two
registered CPU failures nor turn the below-2x broader aggregate into PASS.
The prior `9c245577` 9/13 matrix and its four failures remain historical evidence;
they are not attributed to `4dc2d2d6`, and the new results are not attributed to
the restored runtime without another experiment. No cross-series causal speedup
claim is justified by changing the number of passing timing screens.

## Final verification and continuation

Every one of **253 add-on files in each of four trees** (baseline, previous,
experimental and restored validation) matches its exact Git archive. Initial,
registered matrix and variance Python manifests remain unchanged. Harness and
fixture fingerprints match in every arm. `integrity.json` records the proof.
The restored source passes **2 differential tests in 1.566 s** (15 writer subcases)
and **4 native journal tests in 2.317 s** after all timing measurements finish.
Candidate Universal 295 PASS remains a candidate-local result, not release CI.

A subsequent test-only portability refinement closes the backup destination
explicitly before temporary-directory cleanup. Windows rerun: **2 PASS**, 3.613 s;
see `benchmarks/FINAL_RELEASE_RECEIPT_WINDOWS_TESTS.txt`. This does not modify any
measured runtime/harness or raw archive. Native Git source archives also remain
in the ext4 study's `source-archives/` directory for reproducible verification.

The largest measured remaining native writer cost is storage-budget sampling,
page accounting and guarded transaction lifecycle, detailed in the CPU report.
Continue inside those original checks: distinguish redundant Python/statement
preparation from required fresh filesystem/page/WAL checks. Preserve every
admission/commit/rollback boundary and run a lightweight CPU attribution before
another isolated optimization. Do not reapply the rejected universe experiment,
change cadence, discard work, relax bounds or redesign receipt bytes.

Reproduction: `profile_native_writer.py` (optional `--profile-storage-budget` is
extra diagnostic overhead), `run_receipt_screen.py`, the unchanged registered
`run_gate_d_snapshot_study.py`, `analyze_receipt_study.py`, and
`verify_receipt_study.py`. `screen_receipt_universe.py` must run on frozen
`4dc2d2d6`; it intentionally refuses the restored runtime. Drivers, commands,
logs, complete JSONL, manifests, failed fixture attempts and all raw profiles are
in `benchmarks/FINAL_RELEASE_RECEIPT_RAW.tar.gz` with adjacent SHA-256;
`FINAL_RELEASE_RECEIPT_SUMMARY.json` contains the parsed matrix and checks.

Candidate Recovery, production composition, actual-installation migration,
inherited intermittent retention/rollback, four-hour soak, release integration
and Pi/live HA remain open. The collector remains default-off. No version bump,
merge, release/tag/ZIP or deployment occurred.

## Checkpoint CI

[Checkpoint CI 42305210953](https://github.com/mpuchara/HomeMind-HA/actions/runs/42305210953)
is **SUCCESS for exact `56fd84f8d8bb23fdcb88e127c4f60a07594cb26b`**: 3,176 tests
on Python 3.11 and 3,176 on 3.13, required benchmarks and image build/smoke;
package/release is skipped. Earlier reporting `630f4453` CI `42304381708` also
passes. [Exact CI receipt](benchmarks/FINAL_RELEASE_RECEIPT_CI.json) distinguishes
both sources. Neither run certifies rejected experimental `4dc2d2d6` or a final
release. Later receipt/reporting commits leave add-on, tests and tools unchanged.
