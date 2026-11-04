# U05b.4c.1g.1 — evidence preparation under load

Runtime source is #368 at `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.
[CI 41876948496](https://github.com/mpuchara/HomeMind-HA/actions/runs/41876948496)
completed successfully for that base. Benchmark-only instrumentation is in
`ff63be93311350e333a912376fd32de5a95570df`.

The WSL runs use synthetic Store and Engine fixtures, 1000 registered sensors,
two incoming events per pulse, a 400 ms target cadence and up to 16 real
agent evaluations per pulse. Normal can seed Candidate configs independently
of the Busy worker. Busy adds Candidate/Shadow draining, training, retention
and Correct/Undo. The driver submits directly to the Engine control pool.
The baseline repeat ran from the Windows worktree and the profiled matrix
used an ext4 source copy. Host timing across those runs is descriptive.

| Mode | Roots | Candidates | Durable configs | Prepared at load end | V2 receipts | Eligible/intents |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Normal | 50 | 0 | 50 | 10 | 30 | 30/384 |
| Normal | 100 | 0 | 100 | 13 | 15 | 15/384 |
| Normal | 50 | 50 | 100 | 10 | 30 | 30/384 |
| Busy | 50 | 50 | 100 | 11 | 31 | 26/384 |
| Normal baseline | 200 | 0 | 200 | 10 | 5 | 5/384 |
| Normal | 128 | 128 | 256 | 11 | 10 | 10/384 |
| Busy | 128 | 128 | 256 | 11 | 9 | 8/384 |
| Normal | 200 | 56 | 256 | 10 | 5 | 5/384 |
| Busy | 200 | 56 | 256 | 10 | 4 | 3/384 |
| Normal | 130 | 128 | 258 | 0 | 0 | 0/384 |
| Busy | 130 | 128 | 258 | 0 | 0 | 0/384 |
| Busy baseline | 200 | 200 | 400 | 0 | 0 | 0/384 |

All completed qualification cases wrote 384 decisions per mode with zero
historical collector drops and zero HA service calls. Decision signatures
match within each root count across measured modes and config splits. In Normal, 379 decisions lack a
predecision mask and have no V2 receipt. Ten preparations completed during
load and five bindings became durable V2 receipts. Busy recorded eight
`agent bound` errors across load and finalization. It revoked 48 supervision
labels and the other 336 lacked V2 receipts. At 258 configs the same bound
also triggers in Normal, without a Busy worker. A case above 256 durable
configurations is **UNSUPPORTED** for detached evidence under this contract.
The 100-config split compares 100 active roots with 50 active roots plus
50 paused Candidates. Fewer active roots change the revisit cadence, so the
qualified yield difference does not isolate Candidate cost. Comparing the
50-root Normal runs holds roots fixed and increases durable configs from
50 to 100. Both produced 30 eligible decisions in this single observation.

The earlier 256- and 258-root attempts failed at the fixture's 200-root cap.
Those failures test a separate fixture boundary and are retained.

## What limits throughput below the bound

At 128 roots plus 128 Candidates, 118 distinct targets remained pending at
the load boundary in both modes. In Normal, six of 15 profiled pending turns
spent about 895–2,000 ms inside the Training Budget checkpoint. In Busy,
eight of 15 spent about 746–1,362 ms there. Median native snapshot transaction
time was 11.47 ms in Normal and 12.19 ms in Busy. Median original mask evaluation
was about 2 ms in Normal. The exact 200-root Normal profile reproduced 5/384 with 190 targets pending,
a 365.25 ms median checkpoint and a 9.20 ms median snapshot transaction.
With 200 roots and 56 Candidates, Busy had 191 targets pending, a 508.03 ms
median checkpoint and a 12.93 ms median snapshot transaction.
The slow checkpoint calls dominate their individual
turns and the single collector thread cannot offer another turn during them.
The raw profile includes finalization, while the counts in the table use
preparations completed during load.

Training Budget computes duty-cycle sleep from elapsed wall time since a prior
checkpoint. The same collector thread also waits for ingress, foreground
priority and the periodic idle poll between calls to prepare_one. Its budget
accounting therefore includes those idle intervals and other collector work
as active time. This is a specific cause of low preparation throughput even
with an idle slot and supported cardinality. The 300 ms foreground window and
one-target-per-turn policy additionally constrain the available turns. In the
400 ms nominal cadence the remaining opportunity is about 100 ms before
considering inference futures. A late preparation does not help an earlier
intent. A root in the 200-target, fanout-16 stream is revisited around the
five-second evidence TTL. The benchmark does not attribute individual misses
to that TTL or to related-state mismatches.

Each admitted attempt builds a new detached SQLite database and copies the
arbiter tables. For the 50, 100 and 200-root Normal series, the median full-turn
thread CPU was 10.49, 15.28 and 18.07 ms respectively. Full-turn elapsed medians
were 824, 448 and 928 ms. Those observations support measuring duty-cycle
accounting before changing snapshot reuse or batching. Raw SQL counts measure
instrumented native calls. In-memory SQLite VM work and per-snapshot peak
allocation are outside those counts. The 128+128 runs each recorded 180 direct native SQL calls from
prepare_one across 15 profiled turns (12 per turn). Calls from helpers are
tracked separately by the general probe. At the load boundary, 11 prepared
entries occupied 1,587,536 reported cache bytes in both runs. Process RSS
and cache byte accounting measure different scopes. The current probes do not break out unique unused
snapshots, per-target queue residence, or every invalidation reason. A request
counter can count the same target repeatedly and one prepared snapshot can be
bound to multiple decisions, as in the 50-root case.

## Candidate queue

The baseline Busy queue processed 127, evicted 225 and held 32 pending at the
measurement boundary. Its capacity is 32 roots. New frames for a pending root
coalesce in place, preserving the root's position. An overflow evicts the
oldest queued root and its observation frame. Those missing frames reduce
Candidate/Shadow evaluation coverage. The profiled Busy run with 200 roots
and 200 Candidates recorded 172 evictions and 24 pending. With 200 roots
and 56 Candidates it recorded five evictions and zero pending, but offered
only 112 Candidate jobs versus 384 in the full Candidate case. These runs
vary Candidate workload and detached evidence availability together.
They belong to a separate queue from
history and cannot be inferred from native SQLite decision counts. The replay
uses the fixture's background pump, so production worker recovery, downstream
training impact and causal contention with predecision preparation remain
open.

See the [machine-readable summary](benchmarks/UNIVERSAL_U05B4C1G_SUMMARY.json),
including detailed latencies, CPU/RSS, SQL calls, error reasons, durability
counts and retained adverse observations.
# U05b.4c.1g.2 comparison protocol

Baseline runtime is `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`. Run the same
g.2 harness on separate ext4 snapshots, serially, with 24 ticks, fanout 16
and period 0.4 s. Compare Normal at 1000/200, Busy at 1000/128 plus 128
Candidates and 1000/200 plus 56 or 200 Candidates, then OFF and incomplete
at 1000/200. Profile separate runs including 130 roots plus 128 Candidates.

Require intent signature and count parity, zero HA service calls, zero
collector loss in supported cases, and zero qualification in OFF,
incomplete and unsupported controls. For a throughput improvement require
at least 2× aggregate consumed evidence and qualified/intents across the
three supported active preparation cases, with an increase in each case.
Report consumed/prepared per case. Screen relative latency at +10% p95 and
+15% p99 per pair, relative load CPU at +15% and RSS at +10%. One pair per
case is descriptive. An observed breach stays visible and a production
non-inferiority claim requires independent repetitions.
