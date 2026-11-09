# Evidence preparation scheduling root cause

The traced Normal pair under two CPU contenders reproduced **13 → 12 qualified** on the retained add-on tree. Both arms ran the same harness on one WSL CPU with two additional runnable processes. They produced the same input fingerprint and 384 original decisions with matching signatures. The divergence occurs before bind and native persistence. A subsequent pressure diagnostic varied qualification again under heavier contention with matching original decisions.

The 12 regular diagnostic arms all qualified 13. Their writer SQLite deferrals varied from zero to three, so deferral counts alone do not identify the lost preparation opportunity. The historic 13 → 11 run lacked the scheduler trace required for exact attribution.

## Observed missed opportunity

In controlled pair 3, `light.fixture_12` entered the preparation queue at 78.60 ms in the 13-receipt arm, at position 12, and at 101.75 ms in the 12-receipt arm, at position 13. Times are relative to the first motion ingress in each process. The first difference for this target is its request order. FIFO honors the observed arrival order. The later re-request in the lower-throughput arm preserves its pending position.

| Event for `light.fixture_12` | 13-receipt arm | 12-receipt arm |
| --- | ---: | ---: |
| First request | 78.60 ms | 101.75 ms |
| Selection | 4701.00 ms | 5501.84 ms |
| Publication | 4717.52 ms | 5523.58 ms |
| Second pre-predict lookup | 5204.73 ms | 5208.20 ms |
| Lookup state | Prepared | Pending |

The actor and matching decision identifiers are `07469d85` / `34d5cacc-2447-4b94-809b-c8e4e5c565f5` and `c1e1051c` / `2d7a6cd2-cf6b-440b-aa54-3a7604c8e1ab`. The first binds, commits and qualifies. The second persists with `no_predecision_legal_mask`. Its eventual preparation succeeds with 9.98 ms thread CPU.

The lower-throughput arm enters the foreground writer path at 4923.92 ms. It observes zero active futures and 176.45 ms of foreground priority remaining. The foreground drain succeeds at 5124.26 ms with the priority already expired by 23.89 ms, budget debt zero, writer retry delay zero and the write queue empty. The drain spans 200.34 ms wall time and 5.52 ms thread CPU. The remainder is elapsed time outside the measured thread CPU. The trace cannot separate host descheduling from storage or other waits inside that span.

After its existing 5 ms wait, the collector enters a **250 ms condition wait at 5129.46 ms**. The next ingress at 5200.59 ms wakes it into a new protected foreground window. The original lookup at 5208.20 ms finds the target still pending. The actual wait consumes about 71 ms before ingress. There are zero observed engine/context/preparation lock failures or writer SQLite deferrals in this pair.

This establishes the implementation defect in the observed trace. A foreground drain crosses the idle deadline and the next loop consults the periodic fallback before checking admission again. Request ordering determines which target bears the loss. At the observed wait entry, about 79 ms remain before the target lookup. The later preparation takes about 30 ms wall time. The exact counterfactual publication time without the wait is **UNKNOWN** because it would run under a different interleaving.

## Diagnostic scope

The opt-in probe instruments copies of scheduler methods and wraps the existing harness. It records the actual condition-wait result, idle check and drain result, observations of the priority and budget, queue and lock branches, selected and published agent IDs, original evidence timestamps, lookups, bind outcomes, writer validation and qualified sample keys. It keeps scheduling timestamps on the monotonic clock. Admission snapshots and sampled future counts are observations around the actual branches. The acceptance harness and fixture retain their hashes.

The per-agent timeline joins each original lookup to its bind and native write using the agent and decision sequence. Qualification joins on agent and original decision timestamp. Pending rank is reconstructed from enqueue and selection events captured inside the existing queue lock. The earliest series lacks stable target names across arms. The later controlled traces record the target at lookup. The traces retain the initial failed probe bootstrap and exact probe hashes for each series.

The heavier diagnostic pins the process and eight bounded CPU contenders to one core during the original stream. Its pairs yield 1 → 1 and 1 → 2 qualified with all 384 original decisions in each arm. These traces record 29–30 idle checks per arm with expired priority and active inference futures. This is a separate observed admission limit under severe contention. The foreground guard is doing its intended job in those checks.
