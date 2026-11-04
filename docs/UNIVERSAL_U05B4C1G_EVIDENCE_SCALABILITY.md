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

## g.2 implementation and matched observations

Implementation `245307f470cd710e0a3d6906934d825573d5a3d8`. A completed
preparation turn accrues cooldown equal to active turn wall time ×
`(1 − duty) / duty`, using the configured Training Budget duty cycle.
Ingress waits and native writes can overlap that cooldown. There is no
preparation sleep and no accumulated burst credit. The worker rechecks
foreground admission before each single-target turn. Preparation uses a
zero SQLite busy timeout. Failed writer drains leave preparation for a
later iteration. The original writer's checkpoint retains its intra-turn
behavior and no longer charges elapsed time from a preceding preparation
turn. The existing closed-session recovery now imports the SQLite exception
type it uses.

| Case | Prepared | Consumed / prepared | V3 qualified / intents | p95 ms | p99 ms | Load CPU s | End RSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Normal 200+0 | 10 → 40 | 5/10 → 22/40 | 5/384 → 22/384 | 111.0 → 64.4 | 133.7 → 73.0 | 2.46 → 2.14 | 77.9 → 81.1 |
| Busy 128+128 | 9 → 36 | 8/9 → 28/36 | 7/384 → 24/384 | 139.0 → 179.6 | 160.5 → 258.9 | 5.08 → 6.54 | 89.9 → 93.0 |
| Busy 200+56 | 9 → 27 | 5/9 → 9/27 | 4/384 → 7/384 | 238.5 → 260.2 | 309.9 → 306.0 | 6.74 → 8.96 | 86.7 → 87.9 |
| Busy 200+200 | 0 → 0 | — | 0/384 → 0/384 | 236.3 → 265.4 | 278.6 → 297.5 | 9.43 → 10.00 | 101.9 → 99.2 |
| OFF 200+0 | 0 → 0 | — | 0/384 → 0/384 | 89.7 → 53.2 | 100.1 → 59.0 | 1.66 → 1.02 | 56.2 → 55.9 |
| Incomplete 200+0 | 0 → 0 | — | 0/384 → 0/384 | 55.2 → 54.9 | 64.7 → 68.8 | 1.22 → 1.23 | 76.2 → 76.2 |

The supported cases met the registered throughput screen. Consumed
evidence rose from 18 to 59 and V3 from 16/1152 to 53/1152. Consumption
efficiency fell in both Busy cases. The Busy 128+128 pair exceeded both
latency margins and the two Busy cases exceeded the CPU margin. In Busy
128+128 the concurrent worker produced 1,045 → 901 updates and Candidate
processed 332 → 302 jobs while its own drops rose 36 → 70. Offered load
was identical. Completed background work varied, so causal attribution
and latency non-inferiority remain open.

Two additional unprofiled pairs reversed the original run order.

| Case | Consumed / prepared | V3 qualified / intents | p95 ms | p99 ms | Load CPU s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Busy 128+128 repeat | 8/9 → 36/40 | 7/384 → 31/384 | 139.9 → 144.6 | 161.8 → 171.8 | 5.06 → 5.93 |
| Busy 200+56 repeat | 5/11 → 21/41 | 4/384 → 18/384 | 144.6 → 161.4 | 170.5 → 183.6 | 5.01 → 6.18 |

The 128+128 repeat stayed within both latency margins. The 200+56
repeat exceeded the p95 margin by 1.6 percentage points. All four
supported Busy pairs exceeded the CPU margin. The repeat results
strengthen the throughput observation and leave the tail result mixed.

The current preparation worker spent 0.667, 0.968 and 1.172 s wall time
in turns during the three supported loads and 0.668, 0.794 and 0.873 s
of thread CPU, respectively. Preparation occupied about 7%, 10% and 12%
of load wall time. Its cooldown deferred 0, 1 and 1 admission attempts.
These are not measurements of aggregate CPU capacity. Raw rows retain
pending and rejected counts, physical V2 receipts, collector counts,
native writer latency and SQLite operations. The inherited harness
does not expose event-to-intent max or split snapshot rejection into
eviction, expiry and invalidation. Those metrics are unmeasured.

## g.2 durable configuration bound

The bounded count shares the native SQLite snapshot and runs before
affected-sensor lookups or arbiter table copies. The separate profiled
runs include finalization, so counts can exceed the timed load counters.

| Durable configs | Median native snapshot wall ms | Median turn wall ms | Median turn thread CPU ms | Traced native SQL calls / turn |
| --- | ---: | ---: | ---: | ---: |
| 256 | 11.152 → 12.204 | 301.994 → 28.176 | 18.957 → 21.027 | 12 → 13 |
| 258 | 13.595 → 0.395 | 777.591 → 4.824 | 17.013 → 4.694 | 12 → 3 |
| 400 | 30.032 → 0.514 | 325.124 → 5.177 | 24.961 → 4.957 | 12 → 3 |

The extra query at 256 retained valid preparation and V2/V3 production
in the synthetic case. The 258 and 400 cases still returned `agent bound`
and produced zero V2 and V3. Lower per-attempt cost did not guarantee
lower aggregate rejection work. The 400 profile made 15 → 216 rejected
attempts, and the 258 profile made 13 → 238. These are unsupported controls.
At 256, the original mask's median wall time was 2.406 → 2.766 ms and
affected-evidence preparation was 0.150 → 0.157 ms. These inclusive spans
can overlap. The bounded copy probe combines related state and payload
copies and does not separately attribute them.

## g.2 snapshot materialization follow-up

Implementation `3aecd8182223fa69af4578c6edbc7bbd7a1ae284` reads and
normalizes agent configurations directly in the same native transaction.
SnapshotStore already serves those configs from its detached list. The
isolated SQLite copy still supplies the arbiter's mapping and resource
reads and writes. Flat SQLite scalars are charged to the same structural
byte bound without building a second recursive object tree. The original
agent bound, revision checks and evidence timing still apply.

Two serial pairs per supported Busy case used the exact #368 baseline
`951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`, the same harness and
alternating order on separate ext4 source snapshots. The harness now
also records maximum event-to-intent latency.

| Busy case | V3 / 384 | Consumed / prepared | p95 ms | p99 ms | Max ms | Load CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 128+128 pair 1 | 6 → 25 | 7/9 → 28/30 | 224.8 → 245.2 | 301.8 → 285.7 | 343.8 → 328.6 | +12.5% |
| 128+128 pair 2 | 7 → 29 | 9/11 → 34/39 | 214.2 → 218.8 | 252.9 → 265.7 | 264.2 → 289.2 | +16.6% |
| 200+56 pair 1 | 4 → 16 | 5/11 → 19/34 | 231.2 → 229.4 | 291.9 → 263.6 | 298.8 → 266.7 | +3.6% |
| 200+56 pair 2 | 4 → 19 | 5/9 → 22/43 | 234.3 → 214.1 | 261.4 → 247.3 | 270.6 → 264.0 | +11.4% |

All four supported Busy pairs cleared the registered p95 and p99
screen. Three cleared the CPU screen and one exceeded the +15% margin.
The measured qualification increase and the narrower CPU gap leave
Gate D at **BLOCKER** under the existing criteria. The harness offers the
same ingress in each pair, while the number of background policy updates
and Candidate completions varies.

Normal 200+0 increased V3 from 5 to 22 per 384 intents, with p95
56.5 → 67.8 ms, p99 64.6 → 77.3 ms and CPU 1.318 → 2.051 s.
All three margins were exceeded. In the same implementation, the
incomplete control used 1.351 s CPU with the collector still writing
and preparation disabled. Normal's preparation thread used 0.555 s CPU
over 41 turns. This single contrast locates most of the observed CPU
difference in or near preparation. It cannot assign the remaining
difference to a specific worker. OFF and incomplete retained zero
qualification.

The incomplete control's p99 increased 66.6 → 77.2 ms (+15.9%),
slightly over the +15% screen. Neither incomplete side ran preparation.
This adds evidence of run-to-run tail variation and does not erase the
Normal or Busy margin breaches.

In a separate diagnostic at Busy 128+128, comparing the previous
implementation `245307f4` with `3aecd818`, median preparation thread
CPU per turn fell 24.538 → 19.173 ms and median native snapshot thread
CPU fell 13.451 → 9.052 ms. Median preparation wall time fell
32.102 → 23.628 ms. The probe observed 47 → 53 turns, including
finalization. The stage timings are inclusive. This single profiled pair
describes the cost per turn and is separate from latency acceptance.

The separate 400-config control still rejected with `agent bound` and
zero V3. Its native snapshot median was 31.773 → 0.757 ms versus #368.
The unsupported case attempted more preparations, 14 → 165 in the
profile, so the lower per-attempt cost does not establish lower total CPU.
