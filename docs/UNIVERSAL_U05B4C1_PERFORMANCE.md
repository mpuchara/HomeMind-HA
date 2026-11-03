# U05b.4c.1a â€” runtime and collector diagnosis

## Status and measurement boundaries

Baseline is exact #364 `b3a67c0a2d264697ac69f5ee16bdd673b843f6d3`.
**BLOCKER:** runtime recovery and collector acceptance remain unfinished.
This substage delivers an audit, executable diagnostic tools, isolated
experiments and a tested REST correctness fix. It does not claim completion
of 1bâ€“d or recommend enabling the collector on Raspberry Pi.

All measurements are synthetic WSL host observations. No HA server, network,
real Recorder backend, HA service dispatch/ACK or Pi was measured. Timed
benchmarks, protocol/journal experiments and full local regression run
sequentially. Concurrent policy training is deliberate workload in the
composed fixture. Source/profiler hashes and raw compressed probe archives
are retained; exact source versions/CI are recorded in the handoff.

The existing benchmark bypasses WebSocket receive/decode, uses shared synthetic
motion dependencies and Shadow agents, and trains its demonstration policies
before ingress timing. Its measured prediction is the shipped policy, not an
enabled universal neural Control model. The audit does not infer real HA
transport performance from it. Actual registry population and live UI-client
load need separate deployment tests.

## Instrumentation

`tools/profile_universal_runtime.py` retains cold probes before the existing
warmup reset, then steady probes separately. It reuses existing wall/thread-CPU,
SQLite, Engine/Store lock and executor-queue probes. Added bounded diagnostics
cover routing, metadata resolution, Tiny Shadow lock/schema work, live-card
publication and receipt-to-target admission/entry. The latter includes ingress
and scheduling; it is not pure ready-queue wait. Actual submit-to-entry delay
is a separate existing probe. Lock/SQL spans are inclusive and overlap; they
cannot be added to produce total latency. SQLite execution includes VM work,
busy/GIL/descheduling effects; it is not a separately isolated disk-I/O timer.

The optional 20 Hz stack sampler captures code locations without locals or
arguments, caps unique stacks at 2048, records overflow and its own thread CPU,
and reports wall occupancy rather than per-function CPU. Probes retain 256
samples/operation, explicit discarded-sample counts, and (in later runs) exact
count/sum/max aggregates. Raw archives retain all bounded observations.

An early parallel cProfile prototype produced inconsistent CPU attribution
(child function CPU exceeding the observed outer call interval) and its 50/10
run timed out at 7/10, step 0, under a 120-second diagnostic wait. These are
retained as **invalid instrumentation**, not production failures or accepted
CPU profiles. That implementation was discarded; the published tool uses
bounded stack sampling and the existing clocks instead.

## Four-scale diagnostic profile

These initial profiles use **2 warmup / 4 measured event steps**, all Candidate,
training, Correct and collector workload flags, diagnostic 120-second
observation and additional probe overhead. They are not directly comparable
to the prior 8-warmup/40-event acceptance matrix.

| Sensors/devices | Warmup max ms | Event-to-intent p95 ms |
|---|---:|---:|
| 5/1 | 17.84 | 2.61 |
| 50/10 | 551.92 | 47.10 |
| 200/30 | 2159.72 | 182.30 |
| 1000/200 | 13972.16 | 2629.14 |

At 200/30, cold probes locate 90 `tiny_mlp_shadow.ensure_tables` Store-lock
holds; their retained wall sum is 781.7 ms. Feature preparation totals only
42.9 ms across 107 calls. At 1000/200, the two-step cold profile sees 429 schema
ensure lock holds and substantial `open_windows_batch`, Shadow bundle writes
and model persistence. Raw samples/counters remain in the reports, including
discarded-tail counts; retained sums are not mislabeled complete totals.

Code inspection matches the observed paths: Tiny MLP record helpers repeatedly
run `CREATE TABLE IF NOT EXISTS` and column inspection under `Store.lock`;
Shadow initialization/persistence, Candidate evidence, provenance and protected
observation-window writes share that Store. Existing batching still leaves
long lock exposure. The authoritative decision can be ready before its
optional after-processing finishes occupying a worker; this delays admission
of other targets. Policy prediction alone therefore does not explain end-to-
intent latency.

## Isolated 1000/200 ablations

Same corpus and baseline runtime, **8 warmup / 4 measured events**. Each row
changes one fixture factor and uses identical diagnostic probes. These are
single controlled observations, not a no-regression certificate. Input and
decision hashes match across all five rows; no HA service call occurs.

| Variant | Warmup max ms | Event p95 ms | Mixed collector drops | Total fixture wall ms |
|---|---:|---:|---:|---:|
| All workloads | 37160.80 | 2279.89 | 2075 | 147642.3 |
| No concurrent policy trainer | 17754.40 | 1571.50 | 2075 | 125748.7 |
| No active Candidates | 7630.80 | 2092.80 | 2075 | 108298.7 |
| Collector off | 45946.70 | 3184.80 | n/a | 161526.8 |
| Research: schema checks once | 11927.70 | 2413.80 | 2075 | 140260.0 |

Total fixture time includes synthetic demonstration fitting and setup; it is
not production startup time. Removing training or Candidates is a diagnosis,
not a permissible production fix. Collector-off remaining slow demonstrates
that collector cost alone cannot explain the synthetic warmup. The schema
ablation skips redundant checks only after establishing the schema on the
disposable fixed-schema fixture; it is **not a production schema cache** and
has no external-DDL/replacement invalidation contract. The single run has a lower cold maximum but a worse steady p95; the A/A
spread below prevents treating that latency difference as proven improvement.

Exact cold aggregate Store-lock waiting in the full-workload run is 41.07 s
across concurrent callers, versus 0.129 s thread CPU in those waits. Schema
ensure lock holds are 7.39 s wall / 1.10 s thread CPU, and protected-window
lock holds 13.52 s / 1.55 s. Removing the trainer lowers corresponding observed
Store-lock wait to 13.13 s. These concurrent/inclusive exposures do not sum
to the 37.16-second warmup. They identify contention and repeated durable
bookkeeping as measured costs; CPU alone, network decoding and prediction
are not interchangeable explanations.

### Repeated A/A with final admission probes

Both repetitions use the same final profiler and identical runtime-source manifests, input and decision hashes. Each has 1600 warmup and 800 measured root decisions, zero HA calls, and the same 8/4 step configuration. The 120-second wait is diagnostic only.

| Repeat | Warmup maximum ms | Event p95 ms | Receipt-to-admission p95 ms (bounded steady tail) | Submit-to-entry p95 ms (bounded tail) | Trainer updates/model writes |
|---|---:|---:|---:|---:|---:|
| A | 29449.46 | 2454.57 | 2197.44 | 3.21 | 3943/124 |
| B | 12441.69 | 2210.85 | 2065.30 | 3.52 | 2657/84 |

Most observed delay is before a target obtains a worker slot, rather than after submission to the executor. This is consistent with scarce workers remaining occupied by composed after-processing and shared Store locks. Receipt-to-admission also includes ingress/routing; quantiles from different populations must not be subtracted as a precise decomposition.

**A/A is materially variable:** warmup maxima are 29.45 versus 12.44 seconds on identical code. The fixture continuously trains until foreground completion, so slower foreground allows more background training/writes; fixed event hashes do not imply fixed concurrent work. Candidate/background progress and the number of cold schema checks also differ (552 versus 420 in these repeats). This feedback is visible in measured counters. It prevents interpreting a single ablation latency reduction as a proven optimization effect. Repeated workload-normalized controls remain necessary in 1b/1d.

The schema-once experiment demonstrates removal of redundant schema operations (600 Store-lock holds become 2 in that fixed-schema run). Its wall-time gain is not statistically established: an unmodified A/A repeat can also be much faster. No production cache or learning/Candidate disablement is accepted from these numbers.

For the original 200/30 current A outlier in #364, retained data shows feature p95 2.594 ms wall / 1.143 ms thread CPU versus current B 0.620 / 0.607 ms, and Store-lock wait p95 436.17 versus 224.79 ms. Background model-write counts were 28 versus 32, so a simple higher-write-count explanation is unsupported. There was no unique hot-path implementation change in that row. A conclusive cause cannot be recovered from the old aggregates; this remains a BLOCKER for claiming no repeatable p95 regression. It must be reproduced with current phase probes and host/workload diagnostics, not assigned to HA transport or a code revision by assumption.

## Collector findings

`idle_ready` defers collection while foreground work remains active. In the
short two-warmup 1000/200 run, 725 deferrals and four writer batches accompany
1038 dropped envelopes. In the eight-warmup ablations, 2075 envelopes are
dropped with every enabled collector, despite large latency differences.
Original #364's 9335 drops used a longer 40-event workload; the smaller counts
here are **not a reliability improvement**.

The queue is bounded by both 1024 items and 8 MiB. A byte overflow calls `gap`,
evicts pending envelopes and advances the epoch; it does not drop only the
incoming observation. Envelopes include decisions, masks, barriers and sensor
events. Existing `written` counts batch entries, including entries a writer
can skip after epoch changes. Thus those counters are not exact committed
sensor-observation counts. Per-kind accepted/evicted/committed accounting and
actual qualified-sample yield remain work for 1c; they must not be relabelled
to manufacture an improvement. Busy fixtures lack approved declarations and
complete native topology, so they are not positive training-yield evidence.

The synthetic native journal experiment writes real FeatureJournal data/V3
triggers in isolation, in 128-row batches:

| Sensors | Rows | SQL execute API calls | Write wall ms | Journal/V3 object pages bytes |
|---|---:|---:|---:|---:|
| 5 | 20 | 11 | 4.7 | 12288 |
| 50 | 200 | 37 | 20.8 | 40960 |
| 200 | 800 | 167 | 76.3 | 118784 |
| 1000 | 4000 | 817 | 405.5 | 540672 |

This shows local journal work without foreground contention; it is not a live
SQLite throughput guarantee or an HA Recorder comparison. Internal trigger VM
steps are not SQL API calls. `dbstat` reports object/index page allocations;
raw database/WAL/SHM sizes are retained separately. Household duplicate row
counts and Recorder backend/query costs are unknown. No journal bypass,
retention weakening, queue enlargement, Recorder write or synchronous ingress
history query was implemented.

## HA transport experiments

`tools/benchmark_ha_native.py` performs two repetitions per scale on the same
400-change synthetic corpus, including initial snapshots. Full-event variant
A transfers 258277/270195/305817/508250 bytes. Filtered compact B transfers
8467/5813/8953/35743 bytes for a deliberately declared synthetic dependency
set. Selected RAM state, clocks and known context match; intermediate delta
messages are processed in order. B has no original-event/evidence authority.

The logical dual-stream C increases bytes to A+B (for 1000/200: 543993 bytes).
Decoder CPU is milliseconds for the complete corpus, but this is not an
end-to-end inference test, real network bandwidth/latency or HA server CPU.
Those metrics are explicitly null rather than zero. No transport variant is
adopted on the basis of lower byte counts. See the HA-native audit for the
unresolved permission, reconnect and historical-contract boundaries.

## Acceptance and remaining work

* **BLOCKER / 1b:** default 30-second warmup acceptance is not demonstrated;
  expensive Shadow/observation writes and shared-lock contention remain.
* **BLOCKER / 1c:** thousands of mixed collector envelopes still disappear in
  overloaded runs. No real loss reduction has been implemented or claimed.
* **BLOCKER / 1d:** final matched four-scale A/A acceptance matrix after actual
  runtime/collector fixes, including default 30-second runs, remains required.
* **BLOCKER / HA-native adoption:** actual supported/deployed Core version,
  server permission/reconnect behavior, Recorder configuration and real API
  load require validation. Offline replay cannot certify these.

Prioritize eliminating repeated schema work with a proven invalidation design,
bounding shared-lock work and preserving independent ownership/evidence
contracts. Measure each production candidate separately before combination.
Do not disable learning, Candidate/Shadow, intermediate observations or safety
gates to obtain a green benchmark. Executor, override, leases, Correct/Undo,
Quality Gate and CheckpointLifecycle remain unchanged.
