# Adaptive AI 0.14.18 — bounded training slices

## Why this release exists

A Raspberry Pi run of 0.14.17 exposed a remaining starvation path during explicit
historical training. The UI could stay responsive through most replay, then become
unavailable around **REPLAY COMPLETE** while CPU approached one full core.

The underlying issue was not the amount of history itself. 0.14.17 throttled work at the
archive iterator boundary, but expensive temporal reconstruction and post-replay
finalization could run between those boundaries. The average duty-cycle target therefore
did not bound the longest continuous piece of Python work.

## What changed

0.14.18 adds a shared cooperative wall-clock training budget for
`adaptive-ai-index-*` workers.

Default explicit-training budget:

- target duty cycle: **25%**;
- maximum continuous work slice: **75 ms**;
- maximum compensating sleep: **2 s**;
- only one heavy training job at a time, unchanged.

Checkpoints now exist inside:

- archive replay;
- `SQLiteTemporalTracker` entity/history reconstruction;
- short home-context replay;
- edge scans;
- the replay → finalization transition;
- policy serialization and model save;
- benchmark finalization;
- qualification;
- final worker garbage collection.

The 75 ms value is a **cooperative target**, not a kernel/cgroup hard limit. A single
blocking SQLite operation can still run longer before the next checkpoint. Diagnostics
therefore report both the configured slice and the longest observed slice.

## UI availability

The 0.14.17 read-side UI lifeline remains active. In 0.14.18 the heavy-job slot is also
held through final `gc.collect()`, so the UI cannot switch back to expensive rich reads
while the worker is still cleaning up.

The activity panel now shows:

- CPU duty-cycle budget;
- configured maximum work slice;
- longest observed slice;
- slice overrun count.

Post-replay status also exposes explicit stages instead of remaining on one ambiguous
“Replay complete” message.

## Database and learning semantics

No destructive migration is performed.

This release does **not** change:

- reward semantics;
- policy update order;
- historical experiences;
- raw Recorder archive;
- feedback and Teach labels;
- Candidate/Live lineage;
- promotion thresholds;
- Executor dispatch or physical-control rules.

`set_partial_benchmark()` and queue-completion status now use config-only agent reads
instead of COUNT/AVG history aggregates because those totals are not needed in these paths.

## New option

`training_max_continuous_work_ms`

- default: `75`;
- allowed range: `25..500`;
- applies only to explicit historical training workers;
- realtime inference/control is not throttled by this budget.

## Raspberry Pi verification

The release specifically targets the observed failure mode:

1. training reaches the end of chronological replay;
2. UI displays `REPLAY COMPLETE`;
3. CPU rises to roughly one full core;
4. reopening the Ingress panel fails or stalls.

CI proves deterministic semantics and the cooperative-budget contract. Final CPU and
Ingress responsiveness still need to be verified on the actual Pi, because scheduler,
SQLite storage latency and Home Assistant load are hardware/runtime dependent.
