# U05b.4c.1e steady-state experiment

## Protocol registered before comparison runs

Runtime sources are #364 `b3a67c0a2d264697ac69f5ee16bdd673b843f6d3`,
#365 `613e2e2638df6ac58b873bbcc2a93d78fd63a8c2` (diagnostic),
and #366 `7cf56533187a7db43a0d5d384b8e82444c0108d1`. One common harness
drives all three. All runs are WSL host-only, serial, fresh processes,
8 warmup and 40 steady steps, with an original 30 s observation deadline.
The collector, Candidate, normal Shadow inference, isolated background
training and Correct/Undo are active.

Matched mode releases a one-slot batch at every event, with 64 isolated
policy updates and two model writes at offsets 0 and 32. No next event is
released until both the observation and the training batch finish.
The run records relative monotonic release, trainer start/end and
observation times. This controls arrival phase and counts. Actual overlap,
the Candidate queue and contention remain outcomes. Natural mode uses
the continuous background fit and model writes without the batch barrier.
Correct runs at steps 0 and 25 and all root agents have a Candidate.

The primary 1000/200 series has four matched pairs and two separate
natural pairs. Order alternates baseline/current and current/baseline
within each series. Each smaller scale has a matched and a natural pair.
If the four large matched pairs are inconclusive, add pairs 5 and 6,
preserving earlier attempts. The separate diagnostic series includes #365
at 200/30 and 1000/200 and enables the existing probes. Probe results
are analyzed separately from primary runs.

For functional parity require equal input and decision hashes, equal
observed intent counts, zero HA calls and matching Correct/Undo results.
Check 30 s warmup separately. Collector durability requires physical
row parity and loss-gap checks from its own diagnostic benchmark.
Incomplete pairs are retained and cannot satisfy non-inferiority.

Experimental non-inferiority margins are +10% p95 and +15% p99.
Use paired run ratios and report each independent pair. To call
`NO_REGRESSION`, require at least four complete large matched pairs and
a one-sided exact paired sign test at 0.05 against each margin. With four
pairs this cannot pass, so extend to six if an inference is still needed.
Require natural-workload corroboration with both large pairs complete
and within the same margins. Any other evidence is `INCONCLUSIVE`
unless independent runs support a repeated regression. Report p50, max,
CPU and RSS separately. These margins are experimental. Multi-second
tails remain a separate `SCALABILITY_TARGET_NOT_MET` concern.

## Timeline diagnostic

The opt-in companion uses bounded per-thread RAM rings and monotonic
timestamps. It records ingress, scheduler passes, ready targets and the oldest
receipt represented in those targets, unfinished and done futures, submission
attempts, executor entry/exit, agent entry, selected durable config loads,
features, prediction and Executor submission. Worker interval intersections
measure active workers. Store acquisition, SQLite, Tiny Shadow, Candidate
tournament execution, training updates/model writes and collector writes are
separate inclusive exposures. They can overlap and must never be summed.

The submission timestamp measures admission and the handoff to the executor.
The event-to-submission interval includes ingress and scheduler time. It
cannot distinguish a ready task's exact creation or each failed admission
attempt within an Engine pass. Executor submission bounds intent availability
and does not expose the exact constructor timestamp. Ring drops and thread
limits are explicit, so an absent span in a truncated trace is unknown.

## First matched pair at 1000/200

| Source | Warmup max ms | p50 ms | p95 ms | p99 ms | Max ms | Process CPU s | Peak RSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| #364 | 16,621 | 841 | 1,795 | 2,914 | 3,514 | 104.0 | 477.2 |
| #366 | 9,536 | 760 | 1,456 | 1,595 | 1,905 | 90.8 | 478.7 |

Both runs performed 3,072 updates, 96 model writes and two Correct/Undo
operations, with matching input and decision digests and zero HA calls.
The trainer started before the observation boundary in all 40 steady steps
in both runs. Training finished after the last observed target in 38 of 40
baseline steps and 39 of 40 current steps. This checks the planned overlap
at the event boundary. It cannot resolve variation within individual target
intervals. The collector reported 9,335 baseline drops and zero current
drops. These primary counters do not independently count physical rows.
This first pair is a measurement of the harness under the target load.
The acceptance unit remains the independent run pair, so Gate D is
**INCONCLUSIVE**.

## Four matched pairs at 1000/200

| Pair | Warmup max baseline → current ms | p95 baseline → current ms | p99 baseline → current ms |
| --- | ---: | ---: | ---: |
| 1 | 16,621 → 9,536 | 1,795 → 1,456 | 2,914 → 1,595 |
| 2 | 19,920 → 13,007 | 1,405 → 1,501 | 1,579 → 1,725 |
| 3 | 6,273 → 15,226 | 1,389 → 1,504 | 1,519 → 1,730 |
| 4 | 16,672 → 14,848 | 1,455 → 1,492 | 1,626 → 1,628 |

Every side completed 3,072 updates and 96 writes, at the same event-relative
release points. Three of the four current p95s increased despite matching
counts. All four paired p95 and p99 ratios remained within the registered
margins. Four successes give a one-sided sign-test p-value of 0.0625 per
metric, so these results do not qualify for `NO_REGRESSION`. Six matched
pairs and natural corroboration remain required.

The first three pairs' p95 direction matched the direction of the median
trainer batch duration. Batch duration can itself be a result of contention.
The aggregate comparison does not distinguish that from a scheduler
bottleneck, shared SQLite work, or competition with Candidate and Shadow.
Use the worker timeline for target-level comparisons. The benchmark
processes ran serially, but checkpoint packaging and Git activity briefly
shared the host. No affected attempt is excluded from the report.

## Natural-workload cross-check and smaller scales

| Sensors / devices | Mode | p95 baseline → current ms | p99 baseline → current ms |
| --- | --- | ---: | ---: |
| 1000 / 200 | Natural 1 | 1,625 → 1,398 | 2,137 → 1,601 |
| 1000 / 200 | Natural 2 | 1,321 → 1,391 | 1,455 → 1,668 |
| 200 / 30 | Matched | 139 → 119 | 159 → 135 |
| 200 / 30 | Natural | 124 → 129 | 143 → 183 |
| 50 / 10 | Matched | 79 → 48 | 104 → 59 |
| 50 / 10 | Natural | 43 → 34 | 55 → 39 |
| 5 / 1 | Matched | 4.67 → 7.95 | 5.43 → 13.01 |
| 5 / 1 | Natural | 7.82 → 5.64 | 7.84 → 8.90 |

Natural pair 1 used 5,280 → 4,400 updates and 165 → 138 writes. Pair 2
used 4,385 → 3,297 updates and 138 → 104 writes. Their direction differs
even though current completed less background training in both.
The 200/30 natural p99 increase and the 5/1 matched increase are retained.
The smaller scales have one independent pair per mode. Their tails
remain descriptive, particularly the 40-sample 5/1 runs.

All 24 primary observations completed under the original deadline.
All completed comparisons preserve input and decision digests by scale
and report zero HA calls. Current's longest warmup was 26.410 s and
current reported zero historical collector drops. Physical counts from
the separate traced smoke are covered in CI. The primary matrix has
no post-run SQL count hook. Current max reached 2.545 s at 1000/200 in
matched pair 3, so `SCALABILITY_TARGET_NOT_MET` remains separate from
the relative performance assessment.

## Registered 1000/200 comparison completed

| Pair | Warmup max baseline → current ms | p95 baseline → current ms | p99 baseline → current ms |
| --- | ---: | ---: | ---: |
| 5 | 10,732 → 15,601 | 1,456 → 1,519 | 1,610 → 1,733 |
| 6 | 16,452 → 6,604 | 1,436 → 1,495 | 1,578 → 1,697 |

All six matched pairs were below both preregistered margins. Each one-sided
exact paired sign test gives p = 0.015625. Both natural pairs also met the
margins. The registered result for the 1000/200 synthetic host comparison is
**NO_REGRESSION**. Five of six current p95 values are higher than their
matched baseline, so there is no overall speedup claim. The 200/30 natural
tail remains a separate unresolved one-pair observation. Current max reached
2.824 s in pair 6. **SCALABILITY_TARGET_NOT_MET** remains open. These results
support no production scheduler change without stronger cause evidence.

## Worker timeline and SQLite diagnostics

| Scale | Source | p95 ms | p99 ms | SQLite decisions / observations | Reported collector drops |
| --- | --- | ---: | ---: | ---: | ---: |
| 200/30 | #364 | 150 | 172 | 410 / 48 | 1,065 |
| 200/30 | #365 | 166 | 192 | 410 / 48 | 1,065 |
| 200/30 | #366 | 147 | 233 | 1,440 / 48 | 0 |
| 1000/200 | #364 | 1,633 | 2,279 | 312 / 48 | 9,335 |
| 1000/200 | #365 | 1,569 | 1,834 | 312 / 48 | 9,335 |
| 1000/200 | #366 | 1,581 | 1,742 | 9,600 / 48 | 0 |

#365 is a single auxiliary instrumented observation per scale. The synthetic
ingress path bypasses REST, so this comparison cannot measure the REST
fix's cost or isolate the timing contribution of one change.

The slowest retained, fully joined current 1000/200 target took 1,959.19 ms
from Engine receipt to the first Executor submission. Its directly measured
partition follows. Shares are of that target interval only.

| Source of delay | Time ms | Share | Boundary |
| --- | ---: | ---: | --- |
| Ingress and scheduler before submission | 1,948.41 | 99.45% | Engine receipt to pool submit call |
| Executor worker wait | 0.20 | 0.01% | Pool submit call to worker entry |
| Worker entry to agent entry | 9.08 | 0.46% | Worker entry to composed agent |
| Agent entry to Executor submission | 1.50 | 0.08% | Composed agent entry to first Executor submission |

The same target's feature preparation and prediction occupied 0.52 and
0.24 ms inside the last interval. The joined trace shows no retained
same-thread Store-lock or SQLite span between agent entry and submission.
Absence after ring eviction is unknown. Before submission, at least one
control worker was active for 1,706 ms. Training updates overlapped for
870 ms, model writes for 535 ms, collector writes for 1,325 ms and SQLite
calls for 1,664 ms across threads. These are separately clipped union
exposures, not additive shares or time attributed to a blocker.

The corresponding slowest joined 200/30 current target spent 310 of 316 ms
before pool submission, with 307 ms of other worker activity and 303 ms
of collector write exposure in that interval. The 1000/200 current trace
joined 1,748 of 8,000 steady targets and evicted 171,673 spans across
threads. 200/30 joined all 1,200 targets, with operation-ring evictions.
These measurements place the observed slow paths upstream of execution
and bound the pool FIFO contribution. They do not separate the exact
creation and retry boundaries inside the scheduler or establish which
competing operation caused the delay. No production change is justified
by these individual instrumented comparisons.

All current traced decisions had complete physical counts. Each current
overload database had one coverage gap, zero historical mask rows and zero
coverage segments. This benchmark therefore establishes collector durability
only. Qualified V2/V3 under sustained load remains untested.

Executable checkpoint `916bf6f23816f8b750ab81f1333a17f5cd66908f` passed
[exact-head CI](https://github.com/mpuchara/HomeMind-HA/actions/runs/41810235557)
for Python 3.11 and 3.13, required benchmarks and image validation. The
local matched/timeline smoke passed in 7.565 s. The reporting commit
contains the 34 observations, their raw files and the derived trace joins.
