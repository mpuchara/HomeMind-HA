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

## g.2 preparation poll cadence

Implementation `ab7116f585a2016c3a4a2cf6d513a7a62c3b40fb` retains the
existing 250 ms preparation poll interval as a minimum between preparation
starts. Writer notifications can advance the writer independently. The
existing active-work cooldown can extend the interval and foreground
admission still applies before each turn. A controlled-clock regression
exercises multiple wakeups within the interval.

Two serial pairs per supported case used the exact #368 baseline
`951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`, identical harnesses, separate
ext4 snapshots and reversed order in the second series.

| Case | Prepared | V3 / 384 | p95 | p99 | Load CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| Normal pair 1 | 9 → 23 | 5 → 13 | −24.4% | −32.0% | −14.3% |
| Normal pair 2 | 12 → 23 | 5 → 13 | **+15.2%** | +2.6% | **+19.0%** |
| Busy 128+128 pair 1 | 10 → 23 | 6 → 19 | −33.3% | −34.0% | −14.6% |
| Busy 128+128 pair 2 | 7 → 22 | 4 → 19 | +3.6% | −13.5% | −3.3% |
| Busy 200+56 pair 1 | 10 → 22 | 4 → 10 | +2.4% | +8.4% | +11.8% |
| Busy 200+56 pair 2 | 11 → 23 | 4 → 11 | −23.9% | −27.5% | −7.1% |

Across the six supported pairs, consumed evidence increased 32 → 94 and
V3 increased 28 → 85 per 2304 intents. All four Busy pairs passed all
four performance margins. The second Normal pair breached p95 and CPU.
The current Normal preparation thread used 0.397–0.409 s CPU per
run. The baseline's Normal load CPU differed substantially between the
two observations (2.863 and 2.047 s). The observations establish that
the cadence bound reduces turn count under this offered load relative to
the preceding implementation's 41-turn Normal observation. They leave
Normal performance acceptance open.


## U05b.4c.1g.2d — Normal Performance Recovery

Gate D remains **BLOCKER**. 2 of 6 Normal pairs met all four margins.
Both new Busy controls and both smaller scales crossed at least one margin.
All matched workloads retained decision counts and signatures with zero HA service
calls. Collector drops were zero and the ON arms exported coverage version 3. Supported cases consumed 43 → 113 prepared
proofs and qualified 40 → 108 V3 samples in aggregate.
Each of Normal, Busy A and Busy B improved. The candidate series used fresh
paired baselines and consumed 35 → 86
proofs and qualified 32 → 81 V3 samples. The 400-config fixture continued to
fail closed with zero prepared/qualified evidence.

The comparison used separate ext4 snapshots of #368 `951dcd70` and #369
`ab7116f5` on WSL with the same Python 3.12.3 environment and identical paired
harness. The two runtime trees and benchmark dependencies matched 255 tracked
Git blobs each after line-ending normalization. Each arm used a new process
and database, 24 pulses at 400 ms, and fanout 16 (bounded by root count).
The protocol fixed the order and +10% p95, +15% p99, +15% CPU and +10% RSS
margins before measurements. The six Normal pairs alternated baseline-first
and current-first. CPU is process CPU during load and RSS is the post-run
resident measurement used by the existing harness. Input hashes fingerprint
the logical workload and fixture, with run-local clocks and generated IDs
excluded. JSON retains full digests, SQLite counts, historical coverage, and
Candidate counters. Timings with probes are separate.

| Pair | Δ p95 | Δ p99 | Δ CPU | Δ RSS | Margins exceeded |
| --- | ---: | ---: | ---: | ---: | --- |
| Busy A | +11.5% | +7.2% | +3.8% | +1.5% | P95 |
| Busy B | +19.2% | +36.9% | +13.1% | +0.6% | P95, P99 |
| candidate-busy128 | +11.4% | +24.5% | +11.6% | +0.6% | P95, P99 |
| candidate-busy56 | +16.2% | +25.3% | +16.1% | +0.5% | P95, P99, CPU |
| candidate-normal-1 | -16.4% | -47.3% | -1.4% | +0.9% | None |
| candidate-normal-2 | +11.4% | +10.9% | +25.9% | +0.5% | P95, CPU |
| candidate-normal-3 | +1.8% | +9.3% | +21.8% | +0.7% | CPU |
| candidate-normal-4 | -0.3% | -26.2% | +17.2% | +0.9% | CPU |
| candidate-small:0 | +1.3% | +10.2% | +13.4% | +1.7% | None |
| candidate-small:1 | +16.9% | +8.3% | +18.7% | +1.2% | P95, CPU |
| candidate-unsupported | +2.7% | -3.9% | +1.9% | -1.4% | None |
| normal-1 | +9.9% | -0.8% | +24.9% | +1.2% | CPU |
| normal-2 | -4.7% | -9.2% | +5.5% | +1.3% | None |
| normal-3 | -15.0% | -30.5% | +1.8% | +0.7% | None |
| normal-4 | +5.7% | +9.7% | +30.6% | +1.3% | CPU |
| normal-5 | -1.6% | +17.5% | +11.8% | +0.5% | P99 |
| normal-6 | +8.8% | +5.5% | +19.1% | +0.6% | CPU |
| 50/10 | +21.9% | +37.5% | +43.6% | +9.4% | P95, P99, CPU |
| 200/30 | -30.6% | -17.3% | +15.9% | +8.0% | CPU |
| Unsupported | -15.2% | -13.4% | -9.3% | -0.7% | None |

| Ablation | p50 ms | p95 ms | p99 ms | max ms | CPU s | RSS MiB | SQL calls | Consumed | V3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1-off | 12.49 | 101.39 | 121.14 | 142.06 | 2.13 | 55.88 | 4101 | 0 | 0 |
| 1-incomplete | 15.18 | 105.95 | 120.95 | 135.02 | 2.08 | 75.46 | 5307 | 0 | 0 |
| 1-normal | 23.40 | 123.53 | 144.77 | 145.64 | 3.49 | 79.00 | 5642 | 13 | 13 |
| 2-normal | 11.73 | 66.42 | 78.69 | 85.86 | 1.81 | 78.58 | 5626 | 13 | 13 |
| 2-incomplete | 10.16 | 69.48 | 92.61 | 108.46 | 1.63 | 75.59 | 5171 | 0 | 0 |
| 2-off | 10.00 | 78.82 | 97.71 | 103.38 | 1.49 | 55.94 | 4108 | 0 | 0 |

Preparation ON increased load SQLite calls by 335 and 455. The two process
CPU differences against `incomplete` were +1.408 s and +0.180 s. Preparation
accounting measured 0.772 s and 0.491 s of collector-thread CPU respectively.
The second preparation CPU total exceeded its entire paired process CPU delta.
The OFF and incomplete controls also varied across orders. These comparisons
cannot attribute the full process difference or latency tail to preparation.

### Preparation phases and inference overlap

The corrected profiles retained 384 original decisions each and qualified 13
samples in each ON run. Two earlier ON traces were invalid because the SQLite
probe lacked the `Row` attribute. Those traces are retained in the archive.
They are excluded from the phase and overlap comparison.

The following cells give **calls / inclusive wall ms / inclusive thread CPU ms**
over the recorded profile, including finalization. The preparation-turn interval
and overlap analysis below are restricted to load. Native transaction,
normalization, table copy, and arbiter spans can nest. Their times cannot be
added as independent contributions.

| Phase | Current 1 | Current 2 | Candidate |
| --- | ---: | ---: | ---: |
| foreground_idle_check | 233 / 27.1 / 26.7 | 244 / 28.3 / 28.1 | 210 / 19.8 / 19.6 |
| registry_snapshot | 32 / 32.0 / 32.0 | 32 / 34.4 / 34.4 | 30 / 24.3 / 24.3 |
| ram_snapshot_copy | 32 / 180.4 / 178.4 | 32 / 145.6 / 144.2 | 30 / 158.6 / 158.6 |
| native_snapshot_transaction | 32 / 373.9 / 321.6 | 32 / 398.8 / 347.7 | 30 / 272.5 / 245.7 |
| native_sql_execute | 608 / 35.2 / 31.5 | 608 / 61.6 / 58.1 | 600 / 28.3 / 26.0 |
| sqlite_row_accounting | 832 / 22.1 / 21.8 | 832 / 21.4 / 21.0 | 780 / 16.1 / 16.0 |
| agent_normalization | 6400 / 72.0 / 72.0 | 6400 / 75.1 / 75.1 | 6000 / 58.3 / 58.3 |
| detached_sql_connect | 32 / 5.7 / 2.7 | 32 / 2.5 / 2.5 | 30 / 2.1 / 1.9 |
| detached_sql_execute | 960 / 121.8 / 100.1 | 960 / 125.1 / 101.2 | 840 / 69.7 / 61.2 |
| detached_sql_insert_batch | 416 / 33.7 / 27.3 | 416 / 29.0 / 22.8 | 0 / 0.0 / 0.0 |
| original_arbiter_mask | 32 / 102.1 / 91.2 | 32 / 96.7 / 85.0 | 30 / 61.3 / 57.7 |
| affected_declaration_read | 32 / 2.1 / 1.8 | 32 / 2.5 / 2.3 | 30 / 1.5 / 1.4 |
| affected_topology_read | 32 / 26.9 / 26.5 | 32 / 27.7 / 26.8 | 30 / 50.5 / 49.9 |
| affected_evidence | 32 / 6.3 / 6.3 | 32 / 7.2 / 7.1 | 30 / 5.1 / 5.0 |
| bounded_evidence_copy | 32 / 5.6 / 5.6 | 32 / 5.5 / 5.5 | 30 / 4.3 / 4.3 |
| prepare_pending_turn | 32 / 783.7 / 707.8 | 32 / 772.5 / 698.3 | 30 / 584.2 / 550.9 |

The current profiles processed 1,200 state entries and 1,200 registry entries
per turn, plus 200 normalized durable configs. The fixture has 1,000 sensors
and 200 target states. The full RAM copy accounted for about 2.96 MB of
structural budget per turn. This is conservative accounting for traversal and
copying, distinct from peak resident memory. The native snapshot visited 400
rows per turn and accounted for about 0.97 MB. Each current turn inserted
all 200 generation rows into the detached store and copied about 0.12 MB of
bounded receipt structure. The original arbiter accounted for about 2.7–2.9 ms
CPU per turn. The engine-lock span around the registry and RAM snapshot took
approximately 5.7–6.8 ms wall per turn in the two current profiles.

Both corrected current profiles saw 23 load turns. All began approximately
300–301 ms after the preceding ingress. The longest turn lasted 41.4 ms.
Across 46 turns, none crossed the next motion or lux ingress and none
intersected an ingress-to-intent window. The overlap group is empty, so it
has no p95/p99 or agent CPU comparison. The corrected `incomplete` profile
had no preparation turns. The Normal fixture did not start the Busy
Candidate/Correct worker. Thread accounting observed the control workers,
history worker, provenance writer and main thread.

Collector writes intersected 368/384 ingress-to-intent windows in each ON
profile and 384/384 in the instrumented OFF-preparation control. These
overlap groups are confounded by position within each 16-target pulse.
The timelines show concurrency with writes and the measured CPU shows real
preparation cost. They provide no evidence of preparation stealing inference
time at the 400 ms cadence or of a foreground-admission priority violation.
The six uninstrumented #368 Normal baselines varied from 62.5 to 109.6 ms
p95 and 1.469 to 2.404 s process CPU. Host and scheduler variation cannot be
separated further from this run series.

### Isolated lineage experiment

Candidate `cf0099e7` removes detached materialization of generation rows.
It obtains the selected generation from the existing native read transaction.
The schema, row count, structural byte bounds and revision triggers are still
checked. The original arbiter still executes against its detached database.
The targeted regression first failed against the old path, then passed with
the candidate alongside 15 other predecision tests.

The candidate profile eliminated all 6,400 detached row inserts seen in each
current profile. All three profiles ran 23 preparation turns during load.
Their measured preparation-thread CPU was 437.6 ms, 426.7 ms and 400.6 ms,
respectively. The 26–37 ms difference is an observed profile difference.
The isolated operation is confirmed redundant, and the process-level paired
runs determine whether removing it clears the Gate D margins.

The candidate failed the CPU margin in three of four fresh Normal pairs.
One also exceeded the p95 margin. The candidate was reverted in `948b58c9`.
The paired results and raw probe output are retained, including controls
completed after the revert in the separate candidate snapshot.


Focused WSL suites on the measured runtime reported 295 OK, 35 OK, 237 OK, 184 FAILED (errors=1). The 184-test
Correct discovery initially had one import error in the standalone invocation.
Adding `tests:adaptive_ai/src` to `PYTHONPATH` alone exposed the missing
temporary Store setup. Importing the test support module before discovery
fixed both issues. The final Correct suites for current and candidate
passed 186 tests each. The candidate's 16
predecision tests passed, including the detached lineage regression. The diagnostic
harness is `000f8ca06af4f43a04176240bce19c1d0928cf58`. The prior workflow `41932360794` verified
`d101d831`. Workflow `41970285551` reported `success` for `948b58c9`, which contains the measured runtime and the final diagnostic harness.

The raw archive is [UNIVERSAL_U05B4C1G2D_RAW.json.gz](benchmarks/UNIVERSAL_U05B4C1G2D_RAW.json.gz).
The original evidence and timeline files are also under
`~/homemind-u05b4c1g/g2d-3M0JRY`.
