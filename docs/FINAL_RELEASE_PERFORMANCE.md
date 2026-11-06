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
