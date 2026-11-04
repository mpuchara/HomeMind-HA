# U05b.4c.1f — synthetic runtime stability

The persistent soak runner retains one Engine and Store across repeated
windows, with a short idle phase, a paced sensor stream, Candidate/Shadow,
policy fitting, Correct/Undo, FeatureJournal prune and a burst under an
independent SQLite write transaction. Every window records queue and worker
state, process CPU/RSS, native write accounting, U05a yield, file sizes and
latency percentiles. It then records a synthetic disconnect, closes the
collector and rebuilds the original collection boundary from a fresh
synthetic reconciliation. It verifies that the previous interval cannot be
exported across the interruption.

This tests the collector boundary directly. WebSocket transport, REST and
the production scheduler are separate acceptance work. The runner does not
generate HA service calls or interact with real HA. Existing fake WebSocket,
stale source, retention, Undo and collector retry/reopen tests exercise
additional individual boundaries.

## Gates

The runner defines its thresholds in advance. Window finalization and
collector recovery each have a 15 s threshold. The collector must account for
all event envelopes, respect its capacity, and end each window drained.
Unexplained worker errors fail the run. Maximum RSS growth from the first
window is 128 MiB, WAL size is 32 MiB and native database-family growth is
bounded by 128 KiB per additional synthetic event. Completed futures remain
indexed by target. Active futures at the drain boundary and missing new
intents are failures.

For a run of at least four hours with at least 10 windows in each compared
hour, the final hour must stay within 1.5× initial CPU core-equivalents and
2× initial median window p95. The middle interval remains visible. Short runs
report analogous time ranges and overall `INCONCLUSIVE` for stability. Even
a passing host trend leaves production runtime acceptance open.

## Reproduce

```bash
python tools/benchmark_qualified_soak.py --cycles 3 --window 3 --sensors 5 --devices 1 --output soak-smoke.jsonl
python tools/benchmark_qualified_soak.py --duration 14400 --window 45 --output soak-4h.jsonl
python tools/benchmark_qualified_soak.py --duration 28800 --window 45 --output soak-8h.jsonl
```

Use fresh output paths. The runner flushes complete windows to JSONL so an
interrupted process retains its completed measurements. The 4-hour and
8-hour variants are **NOT RUN**. See the handoff for short-run status.
