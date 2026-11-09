# Evidence scheduling checkpoint — 2026-11-09

**Gate D remains BLOCKER.** A same-source trace under controlled contention reproduces 13 → 12 qualified with all 384 decisions. A foreground drain finishes after priority expires and the collector then waits for a 250 ms poll. The next ingress closes the remaining opportunity. The traced miss is `light.fixture_12`. The historic 13 → 11 run has no equivalent scheduler trace.

Candidate `b141ac0f` adds one readiness recheck at that boundary. It passes controlled scheduling, Universal and exact receipt writer tests. The native diagnostic replay twice recovers 12 → 13. Both unprofiled 50/10 pairs exceed a latency margin, so revert `267ec76b` restores the `9c245577` add-on tree `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`. Full acceptance against exact #368 awaits a repeatable improvement in the unprofiled workload.

First unfinished action is to attribute the 50/10 tail regressions with matched same-source controls and targeted writer/inference wall and thread CPU spans. Preserve the identified wakeup case as a controlled replay, and check the measured cost before a narrower candidate. Study data are in `/home/mpuchara/homemind-evidence-scheduling-20261109`. Retained-tree reporting CI [42666974689](https://github.com/mpuchara/HomeMind-HA/actions/runs/42666974689) passed for `9d6bbfa9`. Draft PR #369 records the new reporting SHA and CI.

[Root cause](FINAL_RELEASE_EVIDENCE_SCHEDULING_ROOT_CAUSE.md) · [Implementation and A/B](FINAL_RELEASE_EVIDENCE_SCHEDULING_OPTIMIZATION.md) · [Raw checksummed data](benchmarks/evidence-scheduling-20261109/)

## Previous checkpoint

# Evidence availability under the Gate D workload

The evidence shortfall observed in the storage A/B screens occurs at the pre-predict lookup. The unprofiled Normal reverse-order pair falls from 13 to 11 consumed and qualified. The writer persists all 384 original decisions in each arm. Requests rise from 371 to 373 and the two extra persisted decisions carry `no_predecision_legal_mask`. Binding rejection and preparation error counters are both zero. Successful preparation actually rises from 31 to 32 turns, so the total number of publications does not explain which targets have evidence at their decision boundary.

## Diagnostic comparison

The probe wraps the existing harness in a separate process and leaves the acceptance harness byte-for-byte intact. It records monotonic request, lookup, preparation, submission and writer times. It records the original availability, validity, inference and intent clocks where the runtime supplies them. The prepared-cache observation after each turn distinguishes publication from merely completing a call. Native cursor wall time includes Python admission and SQLite execution. It cannot isolate lock wait on its own.

The probe caps the trace at 16,384 entries and records overflow. Lookup, bind, writer verification and completion events retain the synthetic agent or intent identifiers needed to join stages. Rejection flags sampled after a failed comparison describe observations at that point. The existing validator makes the decision.

| Runs | Retained / path experiment | Original decisions per arm | No evidence at lookup | Rejected after lookup | Consumed / V2 receipts / qualified |
| --- | --- | ---: | ---: | ---: | ---: |
| Normal A/B in alternating order | 2 / 2 | 384 | 371 | 0 | 13 / 13 / 13 |
| Normal same-revision pair | 2 / 0 | 384 | 371 | 0 | 13 / 13 / 13 |
| 50/10 diagnostic pair | 1 / 1 | 240 | 55 | 10 | 175 / 175 / 175 |

All eight runs retain the corresponding original decision signatures and input fingerprints. Every traced submission is accepted and matches the persisted decision inventory. Writer verification rejects zero bound receipts. Collector errors, collector drops, event drops, HA service calls and trace overflows are all zero. The offline dataset identifies the remaining rows as missing historical receipts.

In Normal, the peak observed preparation backlog is 188 agents. Median time from the first observed request to the preparation attempt is 6.00–6.23 seconds across these runs. Median turn wall time is 8.43–10.24 ms. Total preparation CPU is 304.6–361.4 ms per run and the timed native cursor envelope is 16.6–22.3 ms. Between 47 and 49 lookup misses have an observed preparation start for that agent after the lookup. This is a lower bound because it counts only starts that are later than the observed lookup.

In 50/10, the backlog peaks at 10 and the median observed request-to-attempt interval is about 300 ms. Every rejection after lookup has an observed expiry. The unprofiled 166-receipt arms have 19 successful preparation turns and 74 missing masks. The 175-receipt arms have 20 turns and 65 missing masks. That places the difference before the writer. The 166-receipt execution did not occur in the traced pair, so the exact target and refresh boundary for those nine decisions remain unobserved.

The traced Normal arms likewise reproduce the shortage of pre-predict evidence but all qualify 13. The unprofiled 13-to-11 pair locates the two losses at lookup. Which publication was late and whether it lost its idle slot to SQLite contention, foreground activity or host scheduling remain unmeasured in that arm. The measured queue wait greatly exceeds the median work inside a preparation turn. The data establish a temporal availability problem and support no change to the order, cadence, lifetime or validation of evidence.

The two storage arms show the same counts in both traced workloads. The component speedup in filesystem sampling therefore supplies no repeatable evidence-throughput gain. The archived trace summaries preserve prepare CPU, native cursor time, verification time, writer queue latency, match flags and qualification counts for each arm.

[Raw study and checksums](benchmarks/storage-budget-study-20261108/) · [Storage experiments and acceptance state](FINAL_RELEASE_STORAGE_BUDGET_OPTIMIZATION.md)

Registered same-revision control 3 changes from 12 to 13 qualified on retained code. Preparation turns are 31 → 33, requests and missing masks are 372 → 371 and writer SQLite deferrals are 3 → 2. Preparation errors and binding rejections are zero. The deferral difference does not identify the publication that missed its decision.
