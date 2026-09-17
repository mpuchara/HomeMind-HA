# F22 — bounded history, diagnostics and training cost

Date: 2026-09-17
Stage: 17

## Scope

This stage reduces computation that previously grew with all retained Candidate/history data. It does **not** weaken evidence requirements and does not delete source-of-truth observations needed for audit, undo, model replay or rollback.

The shipped execution path remains:

`run.sh -> trial_queue_main.py -> RuntimeCompositionRoot -> preference/fast/queue stack -> Stage 11/13/14/15/16 -> Stage 17 -> workers`

`ActionIntent -> Executor` remains the only AI physical dispatch boundary. Stage 17 has no HA service path.

## Hotspots found

### Candidate fast metrics

Before Stage 17, `_fast_metrics()` loaded every pair for the generation edge and called `_observed_lead()` twice per pair. For N pairs that created approximately `2N + constant` SQL reads. Repeated UI/status polling repeated the work.

Stage 17 persists versioned sufficient statistics in `candidate_fast_metric_state`. Pair evidence is consumed in bounded batches and each batch fetches parent+child observed decisions in one time-range query. A warm status call checks only for rows after the durable cursor. Option changes that alter metric semantics invalidate the state through an option fingerprint and cause a bounded streaming rebuild from raw evidence.

Opportunity-order decay is preserved exactly: old success/failure sufficient statistics are multiplied by `0.5 ** (1 / half_life)` for each newly consumed opportunity. Manual-correction penalties remain separate, are individually retained in bounded state, decay only for later opportunities and disappear from the metric after Undo. Raw Teach/manual rows remain untouched.

### Candidate comparison summary

Before Stage 17, `_rebuild_summary()` read the complete `candidate_generation_pairs` edge and rebuilt the comparison after every new pair.

Stage 17 adds `candidate_generation_summary_cursors`. The summary and pair cursor are committed atomically with Candidate/generation comparison state. Restart therefore cannot apply the same pair twice. First use on an old installation streams the historical edge in batches; later updates consume only pair rows after the cursor. Raw pairs remain immutable lineage evidence.

False-early counters are not pair-derived. They continue to be updated by their existing event path and are folded into the next cursor update without being reset.

### Teach-RL supervised feature scoring

Before Stage 17, `RLTeaching.supervised_scores()` executed two history queries per eligible sensor: an as-of seed plus a range query, then materialized the trajectory and bisected it for every Teach label.

Stage 17 uses a batched SQLite as-of query. A batch contains at most 24 candidate sensors and at most the existing 256 active Teach labels. For every `(sensor, label)` SQLite selects the indexed last `entity_history` row at or before the label timestamp. The returned working set is therefore bounded to `24 * 256 = 6144` rows per batch, independent of archive length. The calculation after state selection is unchanged: same `context_scalar`, label weights, feature evidence gate, correlation, coverage, recency and evidence factor.

The existing `entity_history(entity_id, ts)` index is retained and Stage 17 adds an explicit `(entity_id, ts, id)` index for deterministic as-of tie ordering.

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
- index `idx_candidate_pairs_edge_outcome_f22`
- index `idx_candidate_decisions_generation_ts_f22`
- index `idx_entity_history_entity_ts_id_f22`
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

### Measured CI smoke on exact PR head

Exact head: `df8d396fb5a1ddc98c3a0a8b1fc7ceaecdddc7b5`

Workflow: `Validate HomeMind`, run `35232855153`, Python 3.11 benchmark job. The test matrix also passed on Python 3.13; the benchmark is intentionally executed only once on Python 3.11.

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

- legacy fast metrics: `1004` SELECT statements, `14.85 ms`;
- optimized cold fast metrics: `8` SELECT statements, `10.05 ms`;
- optimized warm 20 status polls: `100` SELECT statements total, `2.55 ms` total, `0.153 ms` p95 per poll;
- legacy Teach supervised scoring: `96` SELECT statements, `10.33 ms`;
- optimized Teach scoring: `2` SELECT statements, `10.81 ms`.

The small Teach smoke deliberately demonstrates the query-count reduction, not a claimed wall-clock speedup: on this tiny in-memory/CI-sized case the batched query overhead is approximately equal to the old path. The expected scaling advantage is that the optimized query count and Python materialization no longer grow as two full history reads per sensor.

Concurrent synthetic load on this CI host:

- inference calls: `1800`;
- inference p50: `0.0616 ms`;
- inference p95: `0.0756 ms`;
- inference max: `0.154 ms`;
- Candidate/status p95 while Teach scoring runs: `4.91 ms`;
- Candidate/status max: `5.14 ms`;
- Stage-17 write commit p95/max observed in the smoke: about `0.100 ms`;
- peak process RSS: `25.41 MB`;
- peak RSS increase from benchmark start: `2.75 MB`.

These numbers validate the scaling shape and non-blocking behavior on the CI machine only. They are not Home Assistant deployment numbers and are not Pi numbers.

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
- summary and metric cursors: replaceable derived state; rebuildable from raw evidence;
- active regression anchor references: latest 64 per agent; older references stay persisted as inactive audit rows;
- in-memory diagnostics: latest 256 samples;
- Teach labels: existing 256 active-label limit remains unchanged;
- Candidate/Teach computations: bounded batches, never one unbounded Python materialization of the full archive.

## Limitations / follow-up

Stage 17 is a performance layer installed explicitly by the runtime composition root, but two legacy modules still expose internal global helper functions (`_rebuild_summary`, `_fast_metrics`). To avoid a large behavior-changing rewrite in this PR, Stage 17 replaces those helper implementations during composition. This is visible to the existing F23 overlay characterization and should be removed when those Candidate modules are migrated to explicit service contracts in the next F23 extraction. The replacements carry no per-runtime state; durable state is keyed in SQLite and diagnostics are attached to the concrete manager/service instance.

The benchmark is synthetic. It validates scaling shape and same-data equivalence; it does not substitute for a multi-day Home Assistant deployment or a real Raspberry Pi measurement.
