# Receipt CPU investigation checkpoint — 2026-11-06

**Gate D remains BLOCKER.** The isolated derived-universe experiment
`4dc2d2d630aa8ee144c0e7d8492173c462647a34` was implemented, tested and **rejected**:
it has no repeatable process-CPU improvement versus previous production.
Restoration commit `c1dbfd9fd468a9ea24c114cdaf044fbe5b0d8244` returns the add-on tree
to `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`, exactly the `9c245577` production tree.
Version **0.14.232** is unchanged. Reporting-only HEAD is recorded in PR #369.

The frozen rejected candidate completed all **13 pairs / 26 arms** against exact
#368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`: **11/13 timing screens PASS**.
All six Normal pairs, both Busy cases and OFF/incomplete/unsupported controls
pass. CPU fails for **50/10 +16.057%** and **200/30 +16.121%** (limit +15%).
Primary consumed/qualified are **41 -> 99 / 39 -> 95** (>2x), while the broader
supported totals **187 -> 365 / 185 -> 361** are **below 2x**. Every active
supported case improves. No failure was averaged away. All six registered
same-revision timing controls pass; one varies consumed/qualified 13 -> 11.

Every input fingerprint/original decision signature/count and native persistence/
provenance check passes, with zero unauthorized HA calls, collector errors/drops
or event drops. Unsupported 400 remains unqualified (`agent bound`). The earlier
`9c245577` 9/13 matrix remains evidence for the restored runtime; the rejected
candidate's matrix does not replace it or prove a cross-series causal speedup.

Candidate local validation: **295 Universal PASS**; corrected positive exact-byte
writer differential **2 PASS / 15 subcases**; journal differential **4 PASS**.
Restored runtime: **2 differential + 4 journal PASS** after timing finishes.
All **253 add-on files in each of four source trees** match exact archives;
original and registered source/harness manifests remain unchanged.

See [CPU profile](FINAL_RELEASE_RECEIPT_CPU_PROFILE.md),
[optimization, full matrix and rejection](FINAL_RELEASE_RECEIPT_OPTIMIZATION.md),
[parsed evidence](benchmarks/FINAL_RELEASE_RECEIPT_SUMMARY.json) and
[raw evidence](benchmarks/FINAL_RELEASE_RECEIPT_RAW.tar.gz) with adjacent SHA-256.
Next measured target is native storage-budget sampling/page accounting and guarded
transaction lifecycle: 619 Normal load samples and 318 page observations in the
extra diagnostic. Preserve fresh native checks, commit/rollback and all cadence,
workload, qualification, receipt-byte and evidence-throughput contracts.

PR #369 stays Draft/open. Collector stays default-off. Candidate Recovery,
production composition, actual-installation migration, inherited intermittent
retention/rollback, four-hour soak, release integration and Pi/live HA remain open.
No merge, version bump, release/tag/ZIP or deployment.

## Earlier checkpoint (historical)

# Native collector writer checkpoint — 2026-11-06

**Gate D remains BLOCKER: 9/13 registered pairs pass.** Measured/final production
source is `9c245577727c93ede3c59100f11844bf336acb1a`, add-on tree
`cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`, version **0.14.232** unchanged.
Exact baseline remains #368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.

Implemented a fresh, transaction-authoritative empty protection-link fast path
in native FeatureJournal and a bounded native agent-cardinality preflight before
RAM copying. Independent per-decision revision/schema/binding checks, receipt
bytes, original inference, retention, cadence and native persistence remain.

All 26 arms preserve inputs, original signatures, native decisions and fail-closed
controls, with zero collector/event drops, collector errors or HA service calls.
Normal 2/3/6 and 50/10 fail margins. Primary consumed **43 -> 112**, qualified
**41 -> 107**; broader totals **170 -> 401** and **168 -> 396**, improving every
active supported case. Unsupported 400 passes only its timing screen and stays
unqualified (`agent bound`). Six same-revision pairs have two screen failures;
the separate preflight ablation does not prove a repeatable aggregate CPU gain.

Local final production: **295 Universal PASS**, 88 focused PASS, 19 observation
contract PASS; 10 new differential/preflight PASS (overlapping runs, not additive).
[CI 42277851913](https://github.com/mpuchara/HomeMind-HA/actions/runs/42277851913)
is SUCCESS for exact `9c245577`, including 3.11/3.13 and image smoke.

See [native writer report and complete matrix](FINAL_RELEASE_NATIVE_WRITER.md),
[parsed evidence](benchmarks/FINAL_RELEASE_NATIVE_WRITER_SUMMARY.json) and
[raw archive](benchmarks/FINAL_RELEASE_NATIVE_WRITER_RAW.tar.gz). Reporting HEAD
is recorded in PR #369 and the final session reply; reporting changes after
`9c245577` must have an empty `git diff 9c245577 HEAD -- adaptive_ai`.
The earlier `cf09e214` matrix below remains historical and is not attributed to
a later source SHA. PR #369 stays Draft; no merge/release/version change/deployment.
Collector stays default-off. Other release gates remain open.

## Earlier checkpoint (historical)

# Gate D — reusable snapshot checkpoint, 2026-11-06

**BLOCKER** on measured source `cf09e214b4a97f367e8a7ca9169b2f30273b2c63`,
against exact #368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.
[Root cause, architecture, complete matrix and rejected attempts](FINAL_RELEASE_GATE_D_ROOT_CAUSE.md)
is the current report; older evidence below remains historical.

Revision-validated detached SQLite reuse reduces lightweight preparation CPU
16.75 → 9.92 ms/turn, while preserving fresh full RAM and original arbitration.
All 26 arms of the registered 13-pair matrix completed. Five of six Normal
pairs pass, as do both Busy cases, OFF and incomplete. Normal 4 exceeds CPU
(+16.75%); unsupported 400 and 50/10 exceed CPU/p95/p99; 200/30 exceeds CPU
(+15.69%). RSS stays inside +10% in every pair. No failed pair is waived.
Original inputs/counts/signatures match throughout, with zero HA calls,
collector errors/drops or unexplained losses. Negative controls stay unqualified.

Primary aggregate consumed 46 → 106 and qualified 43 → 102 clear the original
2x throughput screen; every supported case improves. Including smaller controls,
consumed 190 → 395 and qualified 187 → 391 also exceed 2x. Throughput does not
waive latency, CPU, RSS or fail-closed criteria. Cadence, persistence, workload,
training/control and qualification thresholds remain unchanged.

Final lifecycle code is `eb6650e2`; the 26 acceptance arms and 12 same-revision
arms remain measured on `cf09e214`. One same-revision pair also breaches tails;
none of the failed baseline comparisons are waived. Supplemental light profiling
locates the main 50/10 CPU delta in native collector writes, not preparation. The
small full-copy prototype was not adopted. See the root-cause report for complete
remaining-cost and allocation evidence and the exact next implementation unit.

## Previous checkpoint (historical)

# Gate D — finalization checkpoint

**BLOCKER**. Margins remain +10% p95, +15% p99, +15% load process CPU,
+10% RSS. Original counts/signatures, no HA service calls, no unexplained
collector loss and the registered qualified-evidence throughput screen remain
mandatory. Neither thresholds nor acceptance workloads were reduced.

Baseline: exact #368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.
Runtime candidate: `f858414c`, measured with harness `5ce4a78e`.
Sources are independent WSL ext4 snapshots, Python 3.12.3 from
`~/homemind-perf-venv`. Each arm uses a fresh process/database, 24 pulses at
400ms and fanout 16 (bounded by root count). Workloads run serially; profiles
are separate. Protocols and source/harness SHA-256 manifests precede their runs.

## Measured redundant operation and ownership

The original detached preparation evaluates the same target descriptor three
times using one frozen mapping DB/full RAM snapshot. Candidate `e2669cc0`
reuses it within that service only. It introduced a reference cycle through a
bound original method. A GC-disabled regression fails on that implementation.
The first Normal pair exceeded RSS by 15.3%; the second exceeded p95/CPU.
This version was rejected. The experiment was stopped during the next pair;
two baseline/candidate pairs and six same-revision pairs are retained. The
remaining planned arms are **NOT RUN**, not passing or silently discarded.

Fix `f858414c` deletes the temporary descriptor override in `finally`, breaking
the cycle on success and error. Seven snapshot tests now pass, including deep
ownership, siblings, explicit remaps/exclusions and concurrent invalidation.
No live descriptor cache, smaller RAM snapshot, changed revision/clock contract
or partial SQL snapshot was introduced.

Separate corrected profiles observed 31 turns each (including finalization),
93→31 descriptor evaluations and 553.2→510.5ms preparation-thread CPU.
Both retained 384 original intents and 13 qualified V3 samples with zero HA calls.
Full state and registry materialization stayed at 37,200 entries each across
turns. This is a measured profile difference, not a process-level speedup claim.

## Complete corrected matrix before fixture-order repair

All 26 arms of the 13-pair matrix exited zero, with no collector errors/drops
or HA calls. Controls OFF, incomplete and unsupported produced zero qualification.
The 400-config control retained `agent bound`. RSS stayed inside its margin.
The following percentages are raw observations, including invalid input pairs.

| Pair | p95 | p99 | CPU | RSS | Exact signature | Qualified baseline→candidate |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| Normal 1 | +22.8% | +15.5% | +26.8% | +2.9% | PASS | 5→13 |
| Normal 2 (reverse order) | +23.1% | +15.5% | +24.6% | +0.8% | PASS | 5→13 |
| Normal 3 | +16.2% | +24.2% | +27.1% | +0.7% | PASS | 5→13 |
| Normal 4 (reverse order) | +7.4% | +5.4% | +22.7% | +0.3% | FAIL: input order | 6→13 |
| Normal 5 | +8.3% | +1.5% | +28.9% | +1.1% | FAIL: input order | 6→13 |
| Normal 6 (reverse order) | +1.9% | −0.3% | +17.3% | +0.9% | PASS | 5→13 |
| Busy 128+128 | −10.2% | −21.4% | +0.2% | +1.9% | PASS | 4→17 |
| Busy 200+56 | +2.0% | +27.1% | +4.7% | +1.0% | FAIL: input order | 3→10 |
| OFF | +2.1% | −13.0% | −0.1% | −0.2% | PASS | 0→0 |
| Incomplete | −7.1% | −17.0% | −6.2% | −0.7% | PASS | 0→0 |
| Unsupported 200+200 | −8.7% | −9.5% | −0.6% | −0.8% | PASS | 0→0 |
| 50/10 | +10.8% | −2.4% | +16.7% | +4.4% | PASS | 110→171 |
| 200/30 | +2.0% | −7.9% | +12.9% | +0.6% | PASS | 60→108 |

Even the four signature-matched Normal pairs exceeded CPU. Mixed/failed
comparisons cannot close Gate D. No result is promoted to Pi/HA acceptance.

## Same-revision variance and input-order defect

Six pre-candidate pairs used the same `ab7116f5` runtime on both sides, alternating
order. One pair exceeded the p99 margin (+29.3%) without a code change, and two
pairs had different decision signatures despite 384 intents each. They remain
unqualified comparisons. Aggregate timing alone cannot separate host variation
from fixture variation in those pairs.

The added original decision traces located the corrected matrix's signature
differences in **which target received each decision index**. The fixture took
`Store.list_agent_configs()` order, which is ordered by real ISO creation time,
independent of the controlled producer clock. Its input order was not explicit.
The actual wall-clock history for those deleted temporary databases was not
retained; do not claim a proven host clock correction from the observed permutation.

The fixture now builds its root list in declared `self.targets` order. This
changes only synthetic workload construction, not production Store ordering.
A regression with reversed Store enumeration fails on the old fixture and
passes on the corrected fixture. The harness now exports both full original
decision rows and an input fingerprint covering root order and workload settings.
Future comparisons must require matching input fingerprints **before** checking
decision signatures or applying the unchanged performance margins. Previous
mismatched rows are not retrospectively counted as passing.

## Input-order repair confirmation (`84563536`)

Seven new pairs completed with the same unmodified load sizes, ticks, cadence,
fanout and margins, using an identical corrected fixture/harness in both trees.
Every pair matched input fingerprints and original signatures; all 14 arms
produced 384 original intents, with zero HA calls and zero collector errors/drops.
Enabled collectors persisted every offered event and every original decision.

| Case | p95 | p99 | CPU | RSS | V3 baseline→candidate | Screen |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Normal forward | +20.6% | +24.8% | +39.1% | +0.8% | 5→13 | FAIL |
| Normal reverse | +8.3% | +6.5% | +27.0% | −0.0% | 5→13 | FAIL: CPU |
| Busy 128+128 | −0.1% | +5.5% | +6.4% | +0.6% | 7→16 | PASS (one pair) |
| Busy 200+56 reverse | +5.8% | +2.3% | +10.3% | +0.9% | 6→11 | PASS (one pair) |
| OFF | −11.2% | −31.2% | −11.3% | −0.4% | 0→0 | PASS (control) |
| Incomplete reverse | +3.1% | +0.1% | +1.9% | −0.6% | 0→0 | PASS (control) |
| Unsupported 400 configs | −5.0% | −1.9% | +8.8% | −1.5% | 0→0 | PASS (fail-closed control) |

This is confirmation of the input-order repair and a negative performance screen,
not a substitute for the complete registered acceptance repetition. Normal still
fails CPU in both orders; Gate D remains **BLOCKER**. Qualification gain is not
a waiver of CPU/latency or safety criteria. Unsupported remains `agent bound`.

All protocols, complete raw data, rejected/incomplete attempts and analysis drivers
are in [raw evidence](benchmarks/FINAL_RELEASE_CHECKPOINT_RAW.json.gz); its checksum
is [recorded separately](benchmarks/FINAL_RELEASE_CHECKPOINT_RAW.json.gz.sha256).
The parsed [summary](benchmarks/FINAL_RELEASE_PERFORMANCE_SUMMARY.json) separates
the initial rejected, corrected and input-ordered series.

Full production
composition, Candidate recovery acceptance, four-hour soak and Pi/live HA remain
**NOT RUN** at this performance checkpoint.
