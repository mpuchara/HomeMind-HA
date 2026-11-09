# Gate D feasibility

## Verdict

**CURRENT REQUIREMENTS NOT DEMONSTRABLY ACHIEVABLE (MEASURED). Gate D remains BLOCKER.** The retained architecture has already exceeded all four registered aggregate evidence targets in one complete matrix. That matrix failed the joint performance requirements in four pairs. The available measurements do not establish a hardware limit, a CPU lower bound above the gate, or a need to replace the architecture.

**PROVEN.** The audit started with clean local, remote and Draft PR #369 head `dba0ceb13bba8a0ef121e2e1e99688611da0a6f5`. The add-on tree is `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`, identical to retained runtime `9c245577727c93ede3c59100f11844bf336acb1a`, version 0.14.232. Gate D comparator is exact #368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.

## Registered Gate D assessment

| Requirement | Retained matrix (MEASURED) |
| --- | --- |
| All 13 pairs within CPU +15%, p95 +10%, p99 +15%, RSS +10% | **9/13**. Normal 2/3/6 fail CPU (+19.7%, +21.3%, +17.6%). Normal 2/3 also fail p95 (+15.0%, +17.8%). 50/10 fails CPU (+45.6%) and p95 (+38.1%). All p99/RSS margins pass in this matrix. |
| Primary consumed and qualified at least 2× | 43 → 112 (2.60×), 41 → 107 (2.61×) |
| Broader consumed and qualified at least 2× | 170 → 401 (2.36×), 168 → 396 (2.36×) |
| Every supported active case improves | Observed. The aggregate 2× requirement is distinct from improvement in each case. |
| Identical original decisions, correct native persistence, zero unexplained event/collector loss or unauthorized HA calls | Observed in all 26 synthetic arms. Unsupported 400 remained unqualified. |

The reverted receipt-universe experiment passed 11/13 timing screens but missed both broader throughput targets. The reverted scheduling experiment recovered 12 → 13 twice in a controlled replay and failed the two 50/10 timing screens against the retained runtime. Those screens were not a new #368 acceptance matrix. No matrix was repeated for this audit. [Retained matrix and raw evidence](FINAL_RELEASE_NATIVE_WRITER.md) · [Receipt experiment](FINAL_RELEASE_RECEIPT_OPTIMIZATION.md) · [Scheduling experiment](FINAL_RELEASE_EVIDENCE_SCHEDULING_OPTIMIZATION.md)

## Scheduling and qualification

**PROVEN LIMIT.** There is one worker for both writing and preparation, at most one preparation per turn, and preparation start spacing is at least max(250 ms, previous charged wall time / duty fraction). The default duty fraction is 0.25. The 300 ms priority window after ingress, outstanding inference futures, writer admission and drain, SQLite retry, debt and wakeup timing all restrict usable intervals. In the ideal periodic stream, each 400 ms cycle exposes at most roughly 100 ms after priority expires and fits at most one start in that window. Thus 4 starts/s is the cadence ceiling in continuous idle, and about 2.5/s is the nominal stream ceiling. Exact ingress jitter and the final drain make the latter a model, rather than a universal bound.

A turn fits the nominal stream only when a wakeup, successful drain and all readiness guards admit it. A 10 ms turn at 25% duty consumes about 40 ms of minimum start spacing before the independent 250 ms floor. The observed limiting resource is access to admission windows. An idle window can be lost while the shared worker drains writes. In the recorded Normal miss a drain took 200.34 ms wall and 5.52 ms worker CPU. After the drain and a 5 ms wait the worker entered the 250 ms condition wait. Ingress woke it roughly 71 ms later, after the remaining usable window had elapsed. The exact reason for the drain's wall-minus-CPU time is UNKNOWN. [Trace](FINAL_RELEASE_EVIDENCE_SCHEDULING_ROOT_CAUSE.md)

**MEASURED PRACTICAL LIMIT.** For the retained arms of the storage trace, measure the active interval through the last inference (~9.21 s). The longer trace includes additional preparation while finalizing, which does not serve original decisions in the stream.

| Metric | Normal 1000/200 | 50/10 |
| --- | ---: | ---: |
| Original decisions | 384 | 240 |
| Request calls | 371 (~40.3/s) | 65 (~7.1/s) |
| Distinct queue admissions reconstructed from the complete trace | 200 (~21.7/s during the stream) | 20 (~2.17/s, including refresh) |
| Requests coalesced while already pending (trace reconstruction) | 171 | 45 |
| Active load publications | 23 (~2.50/s) | 19 (~2.06/s) |
| Publications including finalization | 31 in the retained traces | 20 in the retained trace |
| Max queue / pending at load end | 188 / 177 | 10 / 1 |
| Median wait to attempt / typical wall per turn | 6.00–6.23 s / 8.43–10.24 ms across Normal trace arms | ~300 ms / first retained turn ~3 ms |
| Observed expired lookups | 0 | 10 |
| Consumed / qualified | 13 / 13 | 175 / 175 |

Admissions reconstructed from request and preparation-completion events can misclassify a request arriving within a turn. In these traces they agree with the load-end pending count plus publications and there were no preparation failures. Coalesced calls are demand retries, distinct from native writer deferrals (0–2 in the retained Normal active traces, 2 in retained 50/10) and failed preparation retries (zero observed here). Expired lookups count failures at use, not unique publications that expired unused. The latter count is **UNKNOWN**. Normal's 23 publications versus 13 consumptions also cannot identify which cache entry was ever used. [Trace report and archive](FINAL_RELEASE_EVIDENCE_THROUGHPUT_ANALYSIS.md)

**INFERRED.** Normal has an initial wave of 200 distinct keys and ends the active stream with 177 pending. The bounded, coalesced queue cannot grow indefinitely, but timely service of all keys is infeasible under that finite burst and observed service rate. A stationary queue claim would need a longer workload with a measured distinct-key renewal rate. 50/10 nearly drains the initial plus refresh demand by the load boundary, with observed expiration and run-to-run variation of one successful preparation. It shows no sustained buildup over these 24 ticks. Long-run stability for either case is **UNKNOWN**.

**MEASURED.** 2× aggregate evidence is achievable at the existing cadence in the tested workload. Evidence can be reused across decisions before expiry, so publications and qualified counts have different units. Guaranteed qualification at every relevant original decision or repeatable aggregate qualification alongside all performance margins is **UNKNOWN**. Controlled same-source Normal pressure varied qualification from 13 to 12 and heavier pressure produced only 1–2 qualified. These observations rule out treating cadence as guaranteed service.

## Product performance assessment

**MEASURED, synthetic only.** In registered Normal the retained p95 is 53.50–59.62 ms and p99 62.08–70.63 ms. Registered 50/10 reports p95 9.36 ms and p99 32.24 ms, versus 6.78 and 31.89 ms for #368. The 50/10 RSS delta is +1.4%. Synthetic decision parity and native reconciliation held, and qualification increased. A six-second preparation wait in Normal limits how early new historical evidence becomes usable even if foreground inference remains responsive. Qualification adds usable receipts for later historical training. The improvement in live control or learning quality from this increment is UNKNOWN.

**UNKNOWN, production.** No evidence here quantifies end-to-end HA transport latency, CPU/thermal or RAM headroom on Raspberry Pi, long-run backlog, behavior with actual Recorder and disk pressure, or value of the extra evidence after real label/retention filtering. Busy synthetic and fail-closed tests exercise bounded conditions. Product acceptance requires composed scheduler/HTTP/WebSocket tests, contention/restart and migration with representative data, a four-hour soak and live Pi/HA validation. [Readiness](FINAL_RELEASE_READINESS_REPORT.md)

[CPU and latency audit](FINAL_RELEASE_PERFORMANCE_LIMITS.md) · [Decision and next action](FINAL_RELEASE_GATE_D_DECISION.md)
