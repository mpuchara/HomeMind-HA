# U05b.4b.3 handoff

## Baseline and scope

Branch `codex/universal-u05b4b3-granular-retention` starts at verified Draft
PR #362 HEAD `4755830a0244c6ae33e115e27ce321600806c341`, not main.
Baseline [CI 41655642962](https://github.com/mpuchara/HomeMind-HA/actions/runs/41655642962)
was successful before work. The initial worktree was clean. Reference remains
0.14.232 / `f7384ba5476f26155ecb138034cc3350fa5bfda2`.
The new Draft PR must target `codex/universal-u05b4b2-affected-sensors`, preserving
the #355–362 dependency chain. No merges, package/version change, installation,
automatic neural training, Candidate/Executor/Correct UI or Control activation.

Implementation commit: `728ba13091f7a5d3ff0a90230cda9f6bd77c9553`.
Offline membership hardening and final stress harness:
`e0ac6f3525005b007f4f7af862b2041c37126453`.
[Draft PR #363](https://github.com/mpuchara/HomeMind-HA/pull/363) targets the
exact PR #362 branch above. [Implementation CI 41674189645](https://github.com/mpuchara/HomeMind-HA/actions/runs/41674189645)
completed **success**: Python 3.11 **3074 tests / 302.867 s / OK**;
Python 3.13 **3074 tests / 351.363 s / OK**. Image and every required benchmark
passed; package skipped. The measurement follow-up has its own automatic CI;
its exact HEAD/run must be checked separately (linked in Draft PR #363).
Hardening [CI 41679052057](https://github.com/mpuchara/HomeMind-HA/actions/runs/41679052057)
completed **success**: Python 3.11 **3076 tests / 290.230 s / OK**;
Python 3.13 **3076 tests / 326.808 s / OK**; image and every required benchmark
passed; package skipped. This is the final implementation validation. The
documentation/measurement-only follow-up has its own automatic CI and exact
HEAD/run linked in Draft PR #363; no implementation change is hidden in it.

## Implemented API

Read `UNIVERSAL_U05B4B3_GRANULAR_RETENTION.md` for the schema, trust boundary,
resource/precision budgets and migration semantics. New collected segments are
V3; V1/V2 retain their contracts. Native retention triggers track actual
deletion/mutation and backdated insert atomically, across restarts, disabled
collection and external SQLite connections. Prune buffers are capped at 512.
No new WS/Engine hot-path queries or additional sensor history are introduced.

`ReplayCoverage` separates full historical topology/universe from required
sensor history. `source_from_collection` creates a scoped witness only from
newly collected V3 evidence. U05a verifies tracker, revisions, source/time scope,
native original seed keys/clocks, immutable topology/universe and damage.
Lifecycle publication/guarded load check the witness and supervision revision
in the existing writer transaction. Correct Undo retains its original owner.

Changed modules:

* `universal_retention.py`: bounded durable metadata, trigger contract, witness
  construction, native seed/topology/damage verification.
* `observation_contract.py`: startup migration/disabled-collector protection,
  512-candidate prune chunks and native live-count use.
* `universal_history_collector.py`: original seed-key capture, new V3 segments,
  native sealing, scoped export and V1/V2/V3 dispatch.
* `universal_training_data.py`: ReplayCoverage extensions, receipt-window replay,
  full historical universe membership and V3 qualification.
* `universal_affected_sensors.py`: explicit universe revision/count consistency;
  declaration approval/trust semantics remain unchanged.
* `universal_ha_adapter.py`: optional bounded receipt-time lower cutoff.
* `universal_checkpoint_lifecycle.py`: V3 history/supervision acceptance guard.
* Collector regression tests, two new retention/lifecycle test modules, native
  synthetic training fixture, component/runtime/summary benchmark tools and CI
  smoke. No Engine, DeviceAgent, Executor, Correct UI or version file changed.

Initial migration initializes the live row count once and advances the legacy
global epoch. Missing/altered trigger repair rotates tracker identity; no old
source is repaired into authority. Tables store no sensor values. One envelope
per entity (10,000 maximum) is deliberately conservative on disjoint changes;
overflow advances a global rejection floor. Large prune batches share one
transaction and can still hold the writer for a long operation.

## Validation status

Final focused suite: **33 tests / 93.829 s / OK**, WSL Python 3.12.3, including
external REPLACE (recursive triggers off/on), resource-budget exhaustion and
required loss during training before publication. Existing prune reference
benchmark: **retained-key parity passed**, 20,128 rows, protected/unprotected
prune and append+prune, two passes. The full local suite and required Python
3.11/3.13 implementation CI passed as recorded above. The earlier local full
suite passed **3071 tests / 648.680 s / OK** before the last three added cases;
the initial implementation is covered by CI's 3074 cases on both versions.
Two further regression cases reproduced acceptance of an unknown affected ID
and a shrunken exported registry when V3 checked only universe hash/count.
The hardening restores full historical universe membership validation in U05a;
both cases and the other 31 tests now pass. The fix changes offline U05a
qualification; Engine/WS/Executor/collector/journal execution logic is unchanged.

Native integration covers original HA-event-style clocks, real predecision and
affected declarations, native collector/source/U05a, unchanged universe hash
after unrelated seed deletion, original seed loss/reinsertion, mutation, age,
per-entity/global protected hard caps, disabled/restart/second process, ROLLBACK,
process exit before/after COMMIT, missing/corrupt metadata and legacy semantics.
The lifecycle fixture collects a fresh chronological synthetic stream of 128
explicit V1 historical decision receipts into real V3 coverage; it does not
upgrade earlier coverage or claim physical HA acceptance.

## Executed host benchmarks

Full validated summary: [UNIVERSAL_U05B4B3_SUMMARY.json](benchmarks/UNIVERSAL_U05B4B3_SUMMARY.json).
Four component files `u05b4b3-components-{pr362,current}-{A,B}.json` and
`u05b4b3-runtime-aa.json` preserve SQL counts, CPU, RSS, original evidence
preparation, model prediction, event-to-intent p50/p95/p99, collector write
latency/drops, database/object/index sizes, dataset preparation and eligibility.
Python 3.12.3 / NumPy 2.3.3 / WSL Linux x86_64 HOST only. Temporary databases
are disposable fixtures, not real HA/Recorder or Pi measurements.

| Sensors/devices | Complete evidence / initial samples | PR #362 after unrelated loss | V3 after unrelated loss | V3 metadata object pages |
|---|---:|---:|---:|---:|
| 5/1 | 1 | 0 | 1 | 12,288 B |
| 50/10 | 10 | 0 | 10 | 12,288 B |
| 200/30 | 30 | 0 | 30 | 32,768 B |
| 1000/200 | 200 | 0 | 200 | 94,208 B |

Both sources reject every sample after required loss. V3 has zero false
invalidations in these unrelated-retention cases; PR #362 loses all samples.
Metadata rows after protected-cap stress are 5/50/200/1000, under the 10,000 cap;
object page bytes include the metadata primary-key index, not observation values
or all schema-text overhead. Full database sizes before stress were respectively
565,248 / 802,816 / 1,470,464 / 11,141,120 B for V3 versus
536,576 / 770,048 / 1,421,312 / 11,018,240 B for PR #362 (both A/B).
Preparation/export traces add 16 callbacks in each scale (73/403/1135/7386
versus 57/387/1119/7370); these are trace counts, not C API instruction counts.

Real 2048-row protected loads cross multiple 512-row batches. The no-trace
second prune asserts **2048 actual deletions**, retaining 16, in every A/B case.
The cap is reset before that append so automatic prune cannot turn the measured
prune into a no-op. Wall-time ranges below retain both A/B observations, including
the slower baseline 200/30 run and V3 50/10 dataset outlier:

| Sensors/devices | Append PR #362 / V3 ms (no trace) | Protected prune PR #362 / V3 ms (no trace) | Initial dataset PR #362 / V3 ms (with trace) |
|---|---|---|---|
| 5/1 | 91.75–96.54 / 116.86–118.60 | 21.78–23.70 / 40.95–46.97 | 1.75–2.90 / 2.67–2.75 |
| 50/10 | 88.52–91.45 / 110.57–114.82 | 18.53–21.02 / 42.21–42.38 | 5.17–5.18 / 9.24–238.56 |
| 200/30 | 94.47–206.97 / 120.21–145.39 | 20.75–91.22 / 42.10–65.67 | 13.94–14.24 / 15.97–16.79 |
| 1000/200 | 93.64–95.89 / 106.49–125.34 | 19.22–22.60 / 39.59–42.61 | 736.57–978.19 / 627.83–650.42 |

Triggers have a measurable write/prune cost. Dataset timings include witness
verification, not an isolated witness microbenchmark. Instrumented bulk INSERTs
re-render SQL per trigger callback and are far slower than the no-trace load;
both measurements and rendered-character counts remain in the raw files.
One-envelope precision can also conservatively reject a between-damage interval
(explicit adverse unit case); zero false invalidations is not a universal claim.

The final composed sweep has 40 measured events / 8 warmups, baseline-on A/B,
current-on A/B and current-off at each scale: **equal inputs, equal decisions,
zero HA calls in all 20 runs**. Event-to-intent p95 ranges (A/B) in ms:

| Sensors/devices | PR #362 on | V3 on |
|---|---:|---:|
| 5/1 | 1.76–2.07 | 1.74–2.05 |
| 50/10 | 29.92–30.57 | 29.49–35.29 |
| 200/30 | 94.42–103.04 | 90.48–93.23 |
| 1000/200 | 1169.14–1223.97 | 1158.33–1230.14 |

These are mixed results, not a no-regression certificate. Busy fixtures lack
operator declarations: zero eligible training samples, with **1065** optional
drops at 200/30 and **9335** at 1000/200 in each enabled run. Those gaps remain
fail-closed. Complete native evidence is measured by the separate component
fixture above. The runtime sweep used the 728ba130 runtime; later e0ac6f35
changes only offline membership validation and the component harness. That
qualification branch is not called by the runtime fixture; Engine/WS/Executor/
collector/journal execution logic is unchanged. The last 1000/200 current-on-B
run overlapped focused offline regression checks; the first baseline component
A run overlapped their tail. Keep this confound and A/A spread in interpreting
latency, particularly the slower baseline 200/30 measurement. Original
development, tracing-adverse and automatic-prune
calibration reports are retained as separately labelled JSON, not substituted
for final-code observations. Host noise and the small-event tail prohibit Pi,
physical performance or universal latency conclusions.

`tools/benchmark_universal_retention.py --source-root ROOT --output FILE` runs
the same native component fixture against each revision. Regenerate the checked
summary with `python tools/benchmark_universal_retention_summary.py`.
`benchmark_universal_affected_impact.py` accepts `--baseline-label`; use exact
PR #362 and preserve missing-declaration/dropped-history results at high load.

## Start U05b.4b.4

First verify this branch's final HEAD, Draft base and full CI; do not start from
main or assume an earlier CI applies to later commits. U05b.4b.4 owns the complete
validated-artifact end-to-end path. Reuse NativeDatasetSource, U05a,
CheckpointLifecycle and these V3 native guards; retain original masks,
affected declarations, independent holdout and Undo provenance. Do not introduce
current-HA reconstruction, relaxed lux/power eligibility, automatic training,
Candidate integration or Control activation. Pi/real HA acceptance remains
separate from host results.

The remaining integration distinction is intentional: original V2 predecision /
affected-sensor receipts already reach a qualifying native V3 U05a sample in the
tests; the 128-point training/validation/publish/load fixture uses the explicit
synthetic V1 decision-receipt API with freshly collected V3 coverage. U05b.4b.4
must join the original V2 receipt path to enough independent eligible historical
episodes and the validated-artifact lifecycle, without manufacturing clock,
mask, causal completeness or holdout independence. This stage does not claim
that complete original-V2 validated-artifact end-to-end acceptance.

Concrete next-stage sequence:

1. Fetch #363, verify exact latest HEAD and successful CI, then branch from that
   HEAD for a separate Draft targeting #363; preserve the entire dependency stack.
2. Audit the original V2 decision bindings together with V3 retention: agent,
   generation, logical device/property, original full legal mask, ActionIntent,
   registry/configuration clocks, approved assertion scope, collector epoch and
   original seed keys. Keep unsupported/missing evidence explicit.
3. Build enough independently eligible episodes through original event receipt,
   predecision/prediction/one ActionIntent, native collection, Correct/provenance,
   NativeDatasetSource and U05a. Keep real receipt ordering; do not edit a short
   fixture's timestamps to manufacture history or holdout independence.
4. Exercise the existing explicitly invoked offline rebuild, chronological
   label-availability split, independent validation gate, durable holdout claims,
   atomic publication and guarded load/rank using that same original V2 source.
   No automatic trainer, Candidate or Control connection.
5. Cover required/unrelated retention during training/validation/after publish,
   Undo, registry/configuration/generation changes, WS gaps/reconciliation,
   disabled/restart recovery and stale imported source/artifact rejection. A
   failed attempt must not restore exposed holdout independence or revoked trust.
6. Compare complete-evidence and missing-evidence paths using preserved A/A,
   CPU/RSS/SQL/latency/drop/sample metrics. Keep Pi and physical HA acceptance
   separate; resolve or document measured write costs rather than hiding gaps.
7. Run full Python 3.11/3.13, image and all required benchmark checks on the new
   exact HEAD; update its Draft and handoff. No merge, version or package release.
