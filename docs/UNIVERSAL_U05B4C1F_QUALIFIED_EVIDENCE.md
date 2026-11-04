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
It is a deliberately different workload and must be compared with that label.
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
