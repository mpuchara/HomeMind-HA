# U05b.4c.1a handoff — audit/profiling, recovery still blocked

Branch: `codex/universal-u05b4c1-ha-native-performance`.
Exact base: Draft #364 / `b3a67c0a2d264697ac69f5ee16bdd673b843f6d3`.
Implementation: `ff44e24b2344a7140369ac864b6bcc2f7d63cc8a`.
Production reference remains 0.14.232 /
`f7384ba5476f26155ecb138034cc3350fa5bfda2`.
The local tree was clean; base SHA and successful CI 41718632216 were checked
before branching. The #355–364 Draft chain is preserved. Reporting commits
follow implementation; current reporting HEAD is `git rev-parse HEAD` and the
Draft PR head. No merge/version/package/deployment is authorized by this work.

## Delivered scope

This completes the first coherent audit/profiling fragment, **1a**, with working
tools and tests. **Overall U05b.4c.1 remains BLOCKER**: 1b runtime fixes, 1c actual
collector loss reduction and 1d post-fix acceptance matrix are not complete.
Do not recommend enabling the collector on Pi from these results.

* `adaptive_ai/src/ha.py`: the only production change is explicit
  `significant_changes_only=0` for requested full-history imports. The red test
  first reproduced omission; HA source defaults omission to significant-only.
  This fixes intended REST behavior without changing default/minimal requests,
  historical records, coverage or active models. It may increase future full
  import payloads; existing bounds/splitting remain. No backfill is performed.
* `tools/ha_native_experiments.py`: isolated compact-state RAM cache and
  Recorder-response inspection. No runtime imports/wiring, no historical
  witness/label authority. Tests cover clocks, partial context, attribute
  removals, entity removal, reconnect/base invalidation and malformed frames.
* `tools/benchmark_ha_native.py`: A/B/C transport replay and real local
  FeatureJournal/V3 journal cost, four scales, repeated frozen corpora.
  Server/network/Recorder IO and duplicate household row counts remain unknown.
* `tools/profile_universal_runtime.py`: retained cold/steady probes, exact
  aggregates, routing/metadata/Store/Tiny-lock/schema and target-admission
  observations, bounded wall-stack sampling, immutable JSON/gzip evidence.
  The schema-once research ablation is a disposable fixed-schema experiment,
  not a production cache; it lacks an external-DDL/replacement contract.
* `tests/test_ha_native_contracts.py`: 10 new contract/diagnostic tests.
* `.github/workflows/validate.yml`: two offline/host smoke steps, keeping the
  existing Python 3.11/3.13, image and required benchmark checks.

Executor, manual override, shared leases/resource arbitration, Candidate
Manager semantics, Correct/Undo, predecision/affected evidence, V3 retention,
Quality Gate and CheckpointLifecycle are unchanged. No automatic training,
promotion, neural Control or HA configuration change was introduced.

## Evidence and architectural decisions

Read [HA audit](UNIVERSAL_U05B4C1_HA_NATIVE_AUDIT.md),
[performance report](UNIVERSAL_U05B4C1_PERFORMANCE.md), and
[machine-readable summary](benchmarks/UNIVERSAL_U05B4C1_SUMMARY.json).

Official Core 2026.10.4 source is pinned to
`357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b`; 2025.1.4 source and the official JS
delta client are also inspected. URLs/hashes are retained. Repository minimum
HA Core version and actual household Core version/configuration are unknown;
the optional version question remains pending. No supported-version/live-API
acceptance is fabricated from source inspection.

HomeMind already delegates device integration, physical services, registries
and historical retrieval to HA. Filtered compact updates preserve selected
runtime state in the offline fixture and reduce wire bytes, but omit the
original event envelope and do not certify completeness. Dual-stream bytes
increase by A+B; no end-to-end advantage has been established. Neither option
is adopted. Recorder remains auxiliary/offline; it cannot supply original
HomeMind receipt times, masks, topology/declarations, Correct/Undo or V3 proof.
Statistics cannot reconstruct individual transitions. Current transport and
FeatureJournal remain in place.

## Measured findings

All five 1000/200 ablations and both final A/A profiles retain identical input
and decision hashes, 800 measured root pipeline calls each and zero HA calls.
These diagnostic runs use 120 seconds, not default-deadline acceptance.

* Full-workload diagnostic warmup: 37.16 s; Store-lock wait exposure 41.07 s
  across concurrent callers, with only 0.129 s thread CPU in those waits.
* Cold schema checks: 600 Store-lock holds, 7.39 s wall / 1.10 s thread CPU.
  Protected-window holds: 13.52 s / 1.55 s. Inclusive spans overlap.
* Isolated observations: no trainer warmup 17.75 s, no Candidates 7.63 s,
  collector-off 45.95 s, schema-once 11.93 s. Disabling required functionality
  is diagnostic only. Schema-once removes operations, but its latency gain
  is not proven by a single run.
* Final A/A: warmup 29.45 / 12.44 s, event p95 2454.57 / 2210.85 ms. Same code
  still varies materially. The continuous trainer performs 3943/124 versus
  2657/84 updates/model writes; foreground completion changes background work.
  Identical event hashes do not establish equal concurrent workload.
* Steady bounded-tail receipt-to-admission p95 is 2197.44 / 2065.30 ms,
  while actual submit-to-entry p95 is 3.21 / 3.52 ms. Delay is principally
  observed before worker admission, consistent with busy composed workers
  and shared writes; this is not a precise subtraction of latency components.
* Collector: 2075 mixed envelopes lost in every enabled 8/4-step ablation.
  A byte overflow clears pending envelopes and advances the epoch. Written
  batch counters are not exact committed sensor-event counts. Per-kind
  accounting and native qualified-sample yield need 1c work. No loss reduction
  is claimed by shortening the workload from #364's 40 events.
* Isolated FeatureJournal: 4000 actual native rows at 1000 sensors, 817 execute
  API calls, 405.5 ms wall, 540672 journal/V3 object-page bytes. This excludes
  foreground contention and does not measure Recorder or duplicate live data.

Old 200/30 current A has higher feature CPU/wall and Store-lock wait than B,
despite similar background-write counts. Its exact cause remains unresolved;
no implementation/network explanation is invented from old aggregates.

## Validation/source identity

New contracts: **10 tests / OK / exit 0**. The original failing history-query
test and subsequent successful logs are preserved.
Full local WSL Python 3.12: **3110 tests / 478.362 s / OK / exit 0**.
`u05b4c1/local-source-comparison.json` compares all 724 code/test/CI files to
implementation ff44e24b: only the final profiler's added admission/Tiny-lock
diagnostics differ from that full-suite snapshot. Runtime/client, protocol
and test sources match. The earlier profiler source is retained as gzip for
reproduction; the final profiler is separately exercised in A/A.

Both final A/A runs have actual normalized runtime-Python manifest digest
`6c980c7076903f0c593b7ad26f106279040f17ca7b0c5cab01cbba8689221516`, verified
against ff44e24b Git blobs, and the exact committed profiler SHA-256. The
`runtime_revision` CLI field is a baseline reference, not a claim that the
REST query fix is absent. Raw archives retain the actual file manifest.
Source hashes, exit codes, bounded raw archives, full-suite gzip log, official
source manifest and invalid-prototype logs are under `docs/benchmarks/u05b4c1`.

The early parallel cProfile experiment is rejected instrumentation: inconsistent
CPU attribution and one 50/10 diagnostic timeout. These results remain separate
and cannot support attribution or a production-failure claim.

Required CI for this implementation/reporting branch is recorded in the PR
and summary; check its exact head/run before continuing. A previous-stage CI
is not evidence for new files. Do not treat pending CI as successful.

## Resume locations and next work

Worktree: `C:\Users\mpuch\OneDrive\Dokumenty\HomeMind\HomeMind-HA-universal-232`.
Original working logs/backups: `.devtools/u05b4c1/`.
WSL snapshots: `/home/mpuchara/homemind-u05b4c1-20261103/`
(`current`: baseline runtime and evolving diagnostic tools; `validated`: full
suite snapshot; `admission`: final diagnostic A/A source; `results`: immutable
logs/JSON/gzip/exit codes). Check live processes before restarting anything.

1. Finish/check exact-head CI and keep this audit/profiling PR Draft on #364.
2. Begin 1b from this tested stage, in a stacked Draft branch. First candidate:
   avoid repeated no-op Shadow schema DDL/global-lock acquisition using a
   fresh-schema check or proven invalidation design. Test existing schema,
   missing columns/table, concurrent initialization, restart and external DDL;
   preserve stored models/masks. Do not just reuse the research skip flag.
3. Reproduce the 200/30 outlier and normalize/record background work in repeated
   comparisons. Keep original continuous-work stress results; any fixed-work
   diagnostic must be labelled separately, not silently replace the harness.
4. In 1c, measure accepted/evicted/committed items by kind, actual journal rows,
   idle deferral and window/mask serialization costs. Reduce real losses while
   keeping bounded RAM, intermediate events, fail-closed and writer isolation.
5. In 1d, run exact #364/current A/B/off at all four scales plus fault/load
   cases. The original 30-second 1000/200 test must pass with actual fixes.
   Do not claim no regression from one sample or a longer wait. Live HA/Recorder
   and Pi acceptance remain separate. Keep the collector disabled by default.
