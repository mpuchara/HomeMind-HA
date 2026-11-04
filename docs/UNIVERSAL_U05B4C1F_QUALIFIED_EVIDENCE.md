# U05b.4c.1f — qualified evidence under sustained load

Draft [#368](https://github.com/mpuchara/HomeMind-HA/pull/368) stacks on
[#367](https://github.com/mpuchara/HomeMind-HA/pull/367) at
`c4adb44512f8c1954a8cefdd7970710b249591d2`. The reference release 0.14.232
is `f7384ba5476f26155ecb138034cc3350fa5bfda2`.
The preceding runtime study still reports `SCALABILITY_TARGET_NOT_MET`.

## Protocol

The offline runner uses OriginalEvidenceScenario for disposable Store setup,
explicit original topology, declarations and real Engine/DeviceAgent policies.
Positive uses the paced original episode path with a real collector thread.
Normal, busy, off and incomplete use the same timestamped stream shape and
round-robin target sequence at each cardinality. Their event times advance
with the monotonic host clock. The driver submits real Engine agent processing
to the control pool with bounded per-target outstanding work. It does not
run the full Engine scheduler or connect to Home Assistant. Every motion
pulse can admit a configurable fanout of root targets. The synthetic stream
uses the production 300 ms interactive priority window.

The collector's native write thread runs throughout ingress. There are no
per-intent durability barriers in the sustained profiles. The stream sends
coverage watermarks and continues producing input. Writer occupancy overlap,
foreground admissions and deferred admissions are separate counters. Final
provenance/feedback work, native drain and export are outside the ingress
interval. SQL calls are instrumented native cursor executes and do not include
every internal SQLite operation. Collector write quantiles are a bounded
envelope latency tail that includes queue residence. The probe has overhead.

Collector OFF detaches runtime ingress/decision hooks after the common
synthetic seed. Incomplete uses the same topology and writes with preparation
disabled. This matched negative control differs from #367's sparse composed
fixture. Busy adds real Candidate/Shadow evaluation, fitting of a disposable
copy of an existing policy, SQLite model writes, retention and Correct/Undo.
The fixture pumps the native Candidate queue from its background worker.
Candidate timing and drops therefore belong to this profile's worker cadence.
Busy is a different workload and its timings carry that label.
Overload reduces queue capacity to 32 after seeding and uses an independent
SQLite writer lock and unpaced event bursts.

Native source export and U05a eligibility are different counters. A
`qualified_source` status records successful V3 source export and may have
zero eligible samples. V2 receipt, affected-sensor binding and valid provenance
are independently counted. Samples are built and verified by the native
source. Overload must reconcile all event envelopes, lose some of them, and
reject a full-window export.

## Preparation availability

The initial WSL series exposed a repeated phase mismatch. After a short
foreground write turn, a 250 ms poll can occur near 255 ms and 505 ms after
ingress. The 300–400 ms idle opportunity at the tested 400 ms cadence falls
between those checks. Commit `8f73b665345c19110302c0f1e57f1665aadf4527`
shortens the wait to the interactive deadline only while preparation is
pending. It rechecks native admission and preserves the active-future gate,
snapshot limits, mask semantics, evidence TTL, and rejection rules.

Two hundred roots plus 200 Candidate configs exceed the existing detached
arbiter bound of 256. Preparation errors are grouped by actual exception
text. Zero eligible samples with intact persistence is an admissible
benchmark observation and remains a blocker for acceptance at that scale.

## Offline lifecycle

The optional lifecycle check uses the default ValidationPolicy and existing
rebuild, publication, guarded load and ranking. Controlled positive can use
the original independent-trial synthetic control. Sustained data keep one
continuous episode id and actual feedback availability. The runner does not
relabel overlapping episodes as independent or backdate Correct to construct
a holdout. A sustained source can be usable for U05a yet have insufficient
evidence to train and validate a checkpoint.

See the [measurement checkpoint](benchmarks/UNIVERSAL_U05B4C1F_SUMMARY.json)
and [handoff](UNIVERSAL_U05B4C1F_HANDOFF.md) for measured results and execution
scope.

## Observed yield

The WSL fanout sweep uses one synthetic pulse every 400 ms, 24 paced pulses,
and up to 16 concurrent targets per pulse. The paced profiles generate 48
sensor event envelopes per case. The counts below use original intents as
the denominator. The controlled positive schedules its own sequence.

| Sensors/devices | Positive eligible/intents | Normal eligible/intents | Busy eligible/intents | Overload source |
| --- | ---: | ---: | ---: | --- |
| 5/1 | 8/8 | 17/24 | 16/24 | Rejected |
| 50/10 | 10/10 | 92/240 | 77/240 | Rejected |
| 200/30 | 30/30 | 52/384 | 45/384 | Rejected |
| 1000/200 | 200/200 | 4/384 | 0/384 | Rejected |

Each normal and busy case wrote every original decision and all 48 event
envelopes. The 1000/200 normal source exported four V2 receipts and U05a
rejected 380 labels for missing historical decision receipts. In busy,
15 preparation attempts raised `agent bound` with 400 durable configs.
Correct/Undo revoked 48 labels. The other 336 had no historical receipt.
The Candidate queue processed 276 jobs, dropped 92 and retained 16 at the
measurement boundary. That queue is distinct from the historical collector,
which dropped zero envelopes in the busy case.

All overload cases dropped 240 sensor event envelopes and refused native
export of the requested V3 window. At 1000/200 overload, 200 original intents
were observed and two decisions were durably written. The collector dropped
462 envelopes in total, including decisions and watermarks. The overload
profile restricts capacity after seed and deliberately holds a separate
SQLite write transaction. These are fail-closed results under injection.

The fixed 5/1 lifecycle smoke admitted 128/128 controlled samples, published a
validated offline artifact and loaded it through guarded ranking. The 12-tick
sustained run admitted 11 normal and 10 busy samples. Native rebuild returned
`need_train_and_future_holdout` for both sources.

## Observed latency

Values are p95/p99 in milliseconds from synthetic event receipt to the
original ActionIntent creation clock. BUSY carries extra work.

| Sensors/devices | OFF | ON incomplete | ON normal | ON busy |
| --- | ---: | ---: | ---: | ---: |
| 5/1 | 1.18/1.21 | 1.94/2.59 | 2.81/2.81 | 2.76/2.76 |
| 50/10 | 5.65/27.44 | 7.01/36.16 | 6.89/32.16 | 7.61/124.93 |
| 200/30 | 23.24/60.88 | 28.87/64.54 | 28.68/61.96 | 78.54/183.98 |
| 1000/200 | 56.67/62.78 | 65.23/82.90 | 62.26/73.58 | 164.03/186.39 |

This is one measured sweep. Native coverage/provenance, decision signatures,
p50, queue residence, CPU, RSS, SQLite and SQL measurements are retained in the
JSON checkpoint. The observed root policies emitted synthetic intents that
the executor rejected. Zero service calls and matched intent signatures cover
this workload's functional comparison. Existing dispatch and control tests
cover additional safety paths.
