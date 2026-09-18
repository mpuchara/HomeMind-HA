# F22 — bounded history, diagnostics and training cost

Date: 2026-09-17
Stage: 17

## Scope

This stage reduces computation that previously grew with all retained Candidate/history data. It does **not** weaken evidence requirements and does not delete source-of-truth observations needed for audit, undo, model replay or rollback.

The shipped execution path remains:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py -> RuntimeCompositionRoot -> Stage 11/13/14/15/16/17 -> workers`

`ActionIntent -> Executor` remains the only AI physical dispatch boundary. Stage 17 has no HA service path.

## Hotspots found

### Candidate fast metrics

Before Stage 17, `_fast_metrics()` loaded every pair for the generation edge and called `_observed_lead()` twice per pair. For N pairs that created approximately `2N + constant` SQL reads. Repeated UI/status polling repeated the work.

Stage 17 persists versioned sufficient statistics in `candidate_fast_metric_state`. Pair evidence is consumed in bounded batches and each batch fetches parent+child observed decisions in one time-range query. A warm status call checks only for rows after the durable cursor. Option changes that alter metric semantics invalidate the state through an option fingerprint and cause a bounded streaming rebuild from raw evidence.

Opportunity-order decay is preserved exactly on the normal online stream: old success/failure sufficient statistics are multiplied by `0.5 ** (1 / half_life)` for each newly consumed opportunity. Manual-correction penalties remain separate, are individually retained in bounded state, decay only for later opportunities and disappear from the metric after Undo. Raw Teach/manual rows remain untouched.

#### Late/out-of-order outcome protection

The legacy metric orders opportunities by `outcome_ts`. A final review found that an incremental cursor based only on insertion `rowid` could change half-life weighting if a delayed pair were inserted later with an older `outcome_ts`.

Stage 17 therefore adds `candidate_fast_order_guard` and an explicit repair path:

- an edge is checked once for historical `rowid` vs `outcome_ts` inversions;
- later calls inspect only pair rows newer than the guard cursor;
- normal chronological edges stay on the bounded incremental fast-metric path;
- if a late outcome is detected, only that edge switches to the exact legacy `ORDER BY outcome_ts` calculation;
- the exact repair result is persisted and reused on repeated status polls;
- new pair evidence, active correction facts, metric options or Teach-anchor inputs invalidate the repair cache and cause one new exact recomputation;
- raw pair evidence is never reordered, rewritten or deleted.

This preserves the old numerical semantics even for exceptional delayed data without turning every status request back into a full-history scan.

### Candidate comparison summary

Before Stage 17, `_rebuild_summary()` read the complete `candidate_generation_pairs` edge and rebuilt the comparison after every new pair.

Stage 17 adds `candidate_generation_summary_cursors`. The summary and pair cursor are committed atomically with Candidate/generation comparison state. Restart therefore cannot apply the same pair twice. First use on an old installation streams the historical edge in batches; later updates consume only pair rows after the cursor. Raw pairs remain immutable lineage evidence.

Summary components are commutative counts/sums, so insertion order does not change their result. False-early counters are not pair-derived. They continue to be updated by their existing event path and are folded into the next cursor update without being reset.

### Teach-RL supervised feature scoring

Before Stage 17, `RLTeaching.supervised_scores()` executed two history queries per eligible sensor: an as-of seed plus a range query, then materialized the trajectory and bisected it for every Teach label.

Stage 17 uses a batched SQLite as-of query. A batch contains at most 24 candidate sensors and at most the existing 256 active Teach labels. For every `(sensor, label)` SQLite selects the indexed last `entity_history` row at or before the label timestamp. The returned working set is therefore bounded to `24 * 256 = 6144` rows per batch, independent of archive length. The calculation after state selection is unchanged: same `context_scalar`, label weights, feature evidence gate, correlation, coverage, recency and evidence factor.

The existing `entity_history(entity_id, ts)` index is retained. No redundant `(entity_id, ts, id)` index is built: `id` is the INTEGER PRIMARY KEY/rowid tie key already carried by SQLite's secondary index, and rebuilding a duplicate index on a large Pi archive would itself be an expensive upgrade operation.

### Regression anchors

Regression anchors are references, not training evidence (`training_weight=0`). Stage 17 keeps at most 64 references active in the regression test pool per agent. Older anchor rows are marked inactive with `retired_ts`; they are **not deleted**. `regression_anchor_audit()` can inspect retained historical references while the normal regression path has bounded memory and result size.

### Heavy work and backpressure

Historical replay already ran in a worker and `HistoryManager.status()` already used a cache instead of counting the archive synchronously. Stage 17 preserves that design and adds a maximum pending TrainingQueue depth (default 16, dedup still wins). A new request beyond capacity receives an explicit retryable `backpressure` status rather than allowing the in-memory queue to grow indefinitely. Existing requests for the same agent still follow the normal dedup/upgrade path.

Performance diagnostics are bounded to the most recent 256 samples and expose:

- p95/max Stage-17 write-commit latency;
- maximum rows materialized by a Stage-17 batch;
- queue depth/capacity and oldest wait;
- backpressure count;
- bootstrap vs incremental summary/fast-metric counts;
- Teach batch count.

No diagnostic status call scans evidence history.

## Additive persistence

Stage 17 adds only accelerators/metadata:

- `candidate_generation_summary_cursors`
- `candidate_fast_metric_state`
- `candidate_fast_order_guard`
- index `idx_candidate_pairs_edge_outcome_f22`
- index `idx_candidate_decisions_generation_ts_f22`
- `adaptation_regression_anchors.active`
- `adaptation_regression_anchors.retired_ts`
- index `idx_adaptation_anchor_active_f22`

No existing model vector, schema, TrialRecord, Candidate pair, Teach label, episode or rollback row is reinterpreted.

## Correctness tests

`tests/test_performance_f22.py` compares legacy and optimized code on identical synthetic rows. It verifies:

1. fast preference/timing metrics are numerically equal;
2. the legacy `2N` observed-lead query shape disappears;
3. repeated warm status uses a cursor and does not run a full ordered pair scan;
4. incremental comparison summary equals the legacy full rebuild;
5. restart/new manager does not double-apply a pair;
6. batched Teach as-of scores equal the legacy trajectory+bisect scores;
7. Teach materialization stays within the declared batch bound;
8. active regression anchors remain bounded while audit rows remain retained;
9. queue backpressure is bounded and does not break dedup of an already queued agent.

`tests/test_performance_f22_order_guard.py` adds the order-sensitive exceptional case:

10. a pair inserted later with an older `outcome_ts` triggers exact legacy ordering for that edge, and subsequent unchanged status calls reuse the durable repair cache rather than repeating the full calculation.

Existing Candidate Shadow, promotion, fixed-future evidence, device arbitration and Executor tests remain authoritative for safety behavior.

## Benchmark

Run:

```bash
python tools/benchmark_history_costs.py
python tools/benchmark_history_costs.py --pairs 3000 --teach-sensors 96 --teach-labels 24
```

The script reports:

- exact environment (`platform`, Python, architecture, CPU count, detected Pi model);
- same-data metric differences;
- SQL SELECT count before/after;
- cold and warm elapsed time;
- peak RSS;
- p95 inference latency while a synthetic Teach scoring job runs in another thread and Candidate status is polled;
- p95 status latency under the same load.

The CI workflow runs a smaller smoke profile. CI/desktop numbers are **not Raspberry Pi measurements**. The script labels a run as Raspberry Pi only if `/proc/device-tree/model` identifies Pi hardware.

### Measured CI smoke on the code head

Benchmarked code head: `8a76986b12653a80b07457baa005e3b962385050`

Workflow: `Validate HomeMind`, run `35234589736`, Python 3.11 benchmark job. Python 3.11 and Python 3.13 both passed the full test suite; the benchmark is intentionally executed only once on Python 3.11.

The branch may have a later documentation-only commit; the measurements below correspond to the exact code head above.

Environment reported by the benchmark:

- GitHub-hosted Ubuntu 24.04 runner / Azure x86_64;
- Linux `6.17.0-1022-azure`;
- Python `3.11.16`;
- 4 logical CPUs;
- `raspberry_pi=false`;
- measurement scope: **non-Pi host; do not quote as Raspberry Pi performance**.

Synthetic smoke dataset:

- 500 Candidate pairs;
- 4 decision rows per pair;
- 48 Teach sensor candidates;
- 12 active Teach labels.

Same-data correctness:

- maximum absolute fast-metric difference: `5.3290705182007514e-14` (floating-point accumulation order only);
- Teach supervised score maximum difference: `0.0`;
- Teach `scores_equal=true`.

Query shape and elapsed time on this CI host:

- legacy fast metrics: `1004` SELECT statements, `10.63 ms`;
- optimized cold fast metrics: `8` SELECT statements, `7.94 ms`;
- optimized warm 20 status polls: `100` SELECT statements total, `1.96 ms` total, `0.116 ms` p95 per poll;
- legacy Teach supervised scoring: `96` SELECT statements, `7.89 ms`;
- optimized Teach scoring: `2` SELECT statements, `8.46 ms`.

The small Teach smoke deliberately demonstrates the query-count reduction, not a claimed wall-clock speedup: on this tiny CI-sized case the batched query overhead is approximately equal to the old path. The scaling advantage is that query count and Python materialization no longer grow as two full history reads per sensor.

Concurrent synthetic load on this CI host:

- inference calls: `2325`;
- inference p50: `0.0479 ms`;
- inference p95: `0.0575 ms`;
- inference max: `0.0886 ms`;
- Candidate/status p95 while Teach scoring runs: `3.62 ms`;
- Candidate/status max: `6.72 ms`;
- Stage-17 write commit p95/max observed in the smoke: `0.0828 ms`;
- peak process RSS: `25.64 MB`;
- peak RSS increase from benchmark start: `2.87 MB`.

These numbers validate the scaling shape and non-blocking behavior on the CI machine only. They are not Home Assistant deployment numbers and are not Pi numbers.

## Full validation

On code head `8a76986b12653a80b07457baa005e3b962385050`:

- Python 3.11: **778 tests**, success;
- Python 3.13: success on the same suite;
- `compileall`: success;
- configured JavaScript syntax checks: success;
- `simulate_anticipation.py`: success;
- `simulate_context_tournament.py`: success;
- `simulate_fast_light_timing.py`: success;
- Docker build `homemind:0.14.11`: success;
- packaged image smoke: success.

The added late-outcome test passed on both Python versions.

## Proposed Raspberry Pi budgets

These are initial acceptance **budgets, not measured Pi results**:

- DiagonalLinUCB inference p95 during one heavy job: <= 50 ms
- Candidate/status p95 during one heavy job: <= 150 ms
- Stage-17 write commit p95: <= 100 ms
- pending heavy training queue: <= 16
- Teach materialized rows per batch: <= 6144
- add-on peak RSS in the synthetic acceptance profile: <= 512 MB

Recommended Pi command:

```bash
python tools/benchmark_history_costs.py --pairs 3000 --teach-sensors 96 --teach-labels 24
```

Do not compare wall-clock values between different Python builds/storage media without keeping the environment block with the result.

## Retention policy

- immutable episode/pair/Teach/manual evidence: retained according to its existing audit/undo policy;
- summary, fast-metric and order-guard cursors/cache: replaceable derived state; authoritative source remains raw evidence;
- active regression anchor references: latest 64 per agent; older references stay persisted as inactive audit rows;
- in-memory diagnostics: latest 256 samples;
- Teach labels: existing 256 active-label limit remains unchanged;
- Candidate/Teach computations: bounded batches, never one unbounded Python materialization of the full archive on the normal path.

## Limitations / follow-up

Stage 17 is a performance layer installed explicitly by the runtime composition root, but legacy Candidate modules still expose internal global helper functions (`_rebuild_summary`, `_fast_metrics`). Stage 17 replaces/wraps those helpers during composition to preserve the public behavior without a large architectural rewrite in this PR. The existing F23 overlay characterization sees these replacements. They should be moved into explicit per-runtime service contracts during the next F23 extraction.

The exceptional late/out-of-order edge intentionally prioritizes exact legacy semantics over incremental speed. It performs an exact recomputation only when relevant evidence/configuration changes and persists the result for subsequent status polls.

The benchmark is synthetic. It validates scaling shape, same-data equivalence and UI/inference responsiveness under a controlled workload; it does not substitute for a multi-day Home Assistant deployment or a real Raspberry Pi measurement.


## Current-stack hardening after Stage 13 v2

A later review on the Stage-16-v2 stack found a new F22 regression introduced after the original Stage-17 work. Stage 13 v2 added fixed-future confidence/promotion metrics after PR #76, and its status decorator still loaded the complete Candidate pair edge on every request:

`_decorate_summary -> _pair_rows -> EvaluationEpochJournal.ensure/final_report`.

That meant the old `_fast_metrics` path was bounded, while the newer authoritative promotion metric could again grow with all retained Candidate history.

Stage-17 contract v2 closes that gap without changing evidence semantics.

### Selection evidence

Before a fixed evaluation epoch exists, Stage 13 needs only the exact **evidence-readiness** result from the selection report. A durable per-edge revision and `confidence_selection_scan_cache` make this change-driven:

- unchanged insufficient evidence returns the cached result without reading Candidate pairs;
- a new pair increments the edge revision through an SQLite trigger;
- only then SQLite evaluates the same episode deduplication, opportunity decay, dependency-cluster cap and separate ON/OFF effective-N rules with window functions/aggregation;
- Python receives one aggregate row instead of materializing the entire Candidate edge;
- once the epoch is frozen, selection history is never scanned again for that evaluation revision.

The SQL work of a changed, not-yet-frozen edge can still scale with rows on disk, but it no longer creates an unbounded Python working set and it never repeats on unchanged UI/status polls.

The first status after upgrading an old edge may perform one exact source scan at revision 0. No startup migration scans all edges.

### Fixed future holdout

The final report no longer loads screening/automation history. Its source query is constrained to:

- the exact parent/child generation edge;
- rows after `selection_cutoff_ts`;
- `calibration_eligible=1`;
- declared Stage-13 independent evidence kinds;
- non-null calibration outcome and paired correctness.

A durable `calibration_revision` invalidates the collecting report only when relevant independent evidence changes. Ordinary automation transitions do not invalidate this cache.

Once `final_end_ts` is frozen, ordinary screening/automation history no longer invalidates the report. A new independent calibration fact still advances the calibration revision because it may have been attached retroactively to an episode whose `outcome_ts` is inside the locked window. In that case HomeMind recomputes only the bounded fixed-future window and then caches it again. This preserves the pre-optimization semantics instead of assuming that label-arrival time equals outcome time.

The legacy prefix search for the first sufficient final window now starts at `final_target`; a shorter prefix cannot satisfy the declared minimum, so the skipped prefixes were provably unnecessary.

### Probability calibration

`ProbabilityCalibrationJournal.report()` previously reloaded every probability episode in a scope. It now has a durable scope revision and report cache. On a cache miss, SQLite computes the same decay weights, dependency-cluster cap, Brier score and reliability bins and returns at most the configured bin count (10 by default) to Python. The supported `record()` path increments the revision exactly once for a new stable episode id; duplicate records remain idempotent. Unchanged UI/report reads do not scan calibration history. Raw episodes remain retained for audit.

### Additive persistence in v2

Additional derived-only state:

- `confidence_pair_revisions`;
- `confidence_selection_scan_cache`;
- `confidence_final_report_cache`;
- `confidence_probability_revisions`;
- `confidence_probability_report_cache`;
- index `idx_confidence_pairs_final_window`;
- two SQLite triggers that increment pair-edge revisions on insert/update.

All of these are replaceable accelerators. Raw Candidate pairs, probability episodes, TrialRecords, manual feedback, Teach labels, EpisodeEvaluator rows and rollback state remain authoritative and are not deleted or reinterpreted.

The pair index/triggers are installed lazily only after the Candidate pair schema, including Stage-13 calibration columns, exists. Probability calibration can therefore initialize before Candidate composition without imposing module-order coupling.

### v2 acceptance invariants

New tests require that:

- streamed selection readiness matches the legacy dependency-adjusted result and materializes one aggregate row in Python;
- an unchanged insufficient selection result does not rescan Candidate pairs;
- optimized fixed-future output exactly matches the legacy report on the same rows;
- twenty warm final-status polls perform zero Candidate-pair full scans;
- growth of unrelated automation-transition history does not invalidate the final report;
- a locked final holdout ignores unrelated history growth, while a later independent label causes at most one bounded fixed-window recomputation before warm reads are cached again;
- the durable cache survives a new journal/runtime instance;
- streamed probability calibration is numerically equivalent to the legacy calculation, materializes at most the reliability-bin count, and unchanged reports do not rescan source episodes.

The original Stage-17 equivalence tests for fast metrics, batched Teach scoring, summary cursors, active-anchor retention and queue backpressure remain unchanged.

### Benchmark extension

`tools/benchmark_history_costs.py` now also measures the current Stage-13 fixed-future path. It reports:

- legacy rows materialized by full `_pair_rows`;
- optimized cold query count and maximum materialized final batch;
- twenty warm status polls and the number of Candidate-pair full scans;
- exact/small-floating-noise same-data metric equality;
- streamed selection effective-N/readiness;
- probability-calibration legacy-vs-SQL aggregation plus warm-cache scans.

The benchmark still reports its actual platform and only labels results as Raspberry Pi if `/proc/device-tree/model` identifies Pi hardware. Hosted CI/desktop timings are scaling evidence, not Raspberry Pi measurements.
