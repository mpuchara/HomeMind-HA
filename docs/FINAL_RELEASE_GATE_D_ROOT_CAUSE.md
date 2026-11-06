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

# Gate D reusable arbiter snapshot study — 2026-11-06

Status: **BLOCKER**. All 26 registered arms completed; four pairs breach margins.
Measured candidate `cf09e214b4a97f367e8a7ca9169b2f30273b2c63`;
final lifecycle code `eb6650e2ba90a9e53470178386e623dd73bd4501`
(add-on tree `3fc56e32a4001fd2c2cc7a36716d4c18f0944b67`);
predecessor `4b989c6970f78b57b04dc2529a35db5d6cfc7ed0` (runtime `f858414c`);
acceptance baseline exact #368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.
The intervening reporting commits changed no production, harness or test code.

## Implemented architecture

`universal_predecision.py` owns one bounded `RevisionValidatedArbiterSnapshot`
per evidence worker, serialized by a nonblocking preparation lock. Each turn
reads revision, schema version, required table DDL and exact required revision
trigger bodies inside one authoritative native SQLite read transaction. Source
file identity and collector epoch are part of the identity. A changed revision,
schema, epoch or owner invalidates the connection. Missing/modified triggers,
missing tables, errors and uncertain state reject evidence and dispose the cache.
The original 256-agent, 10,000-row and 4 MiB source bounds remain. Decoded configs
also receive a 4 MiB structural bound. The retained estimate is capped at 8 MiB
and includes SQLite allocated pages, decoded configs, source structural accounting
and a 1 MiB connection/cache reserve. SQLite has a 1 MiB page-cache setting and
a 1,024-page database limit. This accounting is conservative, not an allocator
measurement; process RSS remains an independent acceptance constraint.

Only durable rows and normalized configs persist between turns. Full states,
resolved registry, devices, current agent, runtime holds and RAM-first manual
metadata are freshly detached each turn. No old mask, clock, descriptor,
deadline, service or evidence is cached. Original arbitration executes inside a
savepoint. Resource/manual writes remain visible within that evaluation; rollback
and release complete before evidence enters the prepared queue. Exceptions,
cancellation or rollback errors dispose the baseline. Worker entry/exit close it.
No Engine lock spans native SQLite I/O.

`verify_for_write()` still independently verifies the native revision. New
receipts additionally bind `arbiter_schema_version`; a DDL/trigger change between
preparation and persistence rejects the receipt even if the data revision did
not advance. Legacy receipt qualification is unchanged. The collector remains
disabled by default, version 0.14.232 is unchanged, and no policy, training,
cadence, dispatch or retention contract was adjusted.

## CPU root cause and component result

WSL Ubuntu/ext4, Python 3.12.3 in `~/homemind-perf-venv`; exact input-ordered
24 x 400 ms, fanout 16, 1,000 sensors/200 roots. Timing measurements run serially
after tests. Separate profiles never count as latency acceptance. Both source
trees use the same corrected benchmark and fixture. Raw files retain complete
source manifests and commands. Counts below include finalization turns.

| Lightweight phase, inclusive thread CPU ms | Before (26 turns) | Cache (31 turns) |
| --- | ---: | ---: |
| Full preparation | 435.39 | 307.37 |
| CPU per preparation turn | 16.75 | 9.92 |
| Full RAM bounded copy | 129.08 | 148.72 |
| Registry reconstruction | 24.70 | 30.07 |
| Native snapshot transaction | 201.61 | 59.31 |
| Configuration normalization | 49.86 | 1.90 |
| Detached connection creation | 1.77 | 0.11 |
| Detached SQL execution | 34.17 | 22.04 |
| Detached row insertion | 12.38 | 0.41 |
| Original legal mask | 20.35 | 18.55 |
| Runtime manual hold evaluation | 15.80 | 14.18 |
| Affected evidence | 4.04 | 4.28 |
| Other bounded copies | 4.04 | 7.13 |
| Detached cleanup | 2.68 | 0.10 |

Nested phases are **not additive**. The cache's other bounded copies include
the additional bounded config graph on rebuild. RAM work is retained, not moved
out of the measured turn. The native transaction still reads fresh declarations
and affected topology. Cache validation remains native work each turn.

Before: 10,400 native materialization rows, 5,200 detached inserts and 5,200 normalizations.
After: 400 native materialization rows, 200 detached inserts and 200 normalizations.
These row counters exclude the still-required DDL/revision/declaration/topology
validation reads; complete SQL call counts are preserved separately. All 26 and
31 respective revision observations were 200: 25 and 30 consecutive unchanged
revision transitions. The cache had one rebuild, 30 reused turns and no cleanup
failure. At load end it retained an accounted 3,673,392 bytes (22 hits/one miss;
the remaining preparation occurs during finalization). Shutdown releases it.

**Dominant redundant durable operation:** repeated materialization and decoding
of identical arbiter tables. The native transaction was about 7.75 ms CPU/turn
before and 1.91 ms after, with its nested phases included. The remaining largest
preparation component is the full RAM copy (about 4.80 ms/turn), followed by
registry reconstruction. Removing durable repetition is a component result,
not proof of process CPU or Gate D acceptance.

Separate intrusive, thread-local call-tree CPU traces expose exclusive and
inclusive function CPU. They change admission/budget timing markedly (6/7
turns) and must not estimate production CPU savings. Representative **exclusive
/ inclusive ms** across those traces:

| Function | Before, 6 turns | Cache, 7 turns |
| --- | ---: | ---: |
| Preparation body | 7.54 / 2350.34 | 5.57 / 2310.09 |
| `bounded_copy` entry (children separate) | 0.09 / 1440.95 | 0.11 / 1805.17 |
| `_agent_dict` | 38.21 / 163.32 | 6.76 / 29.59 |
| `resolved_registry` | 16.94 / 29.36 | 20.55 / 34.75 |
| `legal_action_mask` | 0.24 / 77.31 | 0.27 / 83.03 |
| `_persist_runtime_manual_holds` | 1.87 / 64.27 | 2.12 / 70.35 |
| Affected `prepare` | 0.30 / 2.42 | 0.32 / 2.62 |
| Evidence `encoded` | 0.12 / 1.06 | 0.71 / 1.96 |
| Cache source identity | — | 3.18 / 19.09 |
| Savepoint rollback/release | — | 0.07 / 1.88 |

Full Python/C rows, SQL execution/fetch/insertion calls, cleanup, structural
allocation budgets, thread scheduler accounting and timeline clocks are retained.
Structural allocated work is distinct from actual allocator peaks. Intrusive
profiling overhead is explicitly included. Garbage collection outside the
preparation call tree is not attributed to preparation.

## Differential validation

34 focused tests pass on the final candidate (18.314s). The frozen fresh oracle
is the exact `4b989c69` module under `tests/reference/predecision_fresh.py`.
Tests compare complete old receipts and resource-read traces under identical
clocks. Only the intentionally new schema guard and its structural byte cost
are checked independently. No original mask/descriptor/deadline expected value
was changed. Coverage includes multiple agents, normalization-free reuse,
external configuration writes, RAM/durable mismatch, missing and replaced
triggers/tables, shared-device siblings/remaps/exclusion/resource groups,
Control ownership, leases and dwell, manual-hold rollback/expiry, nested RAM,
RAM-first metadata, registry/epoch changes, between-prepare/write DDL changes,
exception/cancellation, failed native decoding, actual SQL rollback failure,
memory rejection, disposal, restart and thread-owner handoff. Existing scope
tests retain concurrent registry/state/source-revision and GC-disabled ownership
coverage. 295 Universal tests passed on `51b49b91` (204.475s); the final change
adds decoded-config bounds and is covered by the final focused tests.

## Rejected/incomplete diagnostic attempts

- Initial cProfile thread-CPU traces captured unrelated foreground functions
  and impossible/negative CPU costs. Freezing in the owner thread did not fix
  it. Both attempts and a duplicate are retained and rejected. The valid
  replacement uses a thread-local `sys.setprofile` call tree.
- The exact #368 profiling attempt exposed an existing probe incompatibility:
  #368 has no `_sqlite_row_bytes`. It failed before load and is **not a result**.
  Unprofiled acceptance does not enter this optional instrumentation branch.
- Initial snapshot export with Linux Git failed on the Windows worktree's
  absolute `.git` pointer; an empty-tree test invocation failed discovery.
  Windows `git archive` followed by ext4 extraction replaced that setup. The
  initial 22-test run used the not-yet-integrated path; it is not cache validation.
- Original runtime modules in prior ordered snapshots were not changed. An
  additional profiling entrypoint was temporarily placed alongside them;
  its exact updated copy is retained with the diagnostic evidence, and that
  added entrypoint is removed from the old immutable tree.

## Registered performance acceptance

The serial study registers six alternating Normal pairs, Busy 128+128 and
200+56, OFF, incomplete, unsupported 400 configs and 50/10 and 200/30 controls.
If results are mixed, six fixed-input same-revision Normal pairs are registered.
Every arm keeps native persistence and the original collector/background work.
Matching fingerprints and original signatures are required before ratios.
Thresholds remain +10% p95, +15% p99, +15% process CPU and +10% RSS. Throughput
must be at least 2x aggregate consumed **and** qualified evidence with improvement
in every supported case. Controls must remain fail closed with no unexplained
collector loss or HA calls. Complete outcomes will be appended after the run.

## Complete registered matrix (`cf09e214`)

All 13 pairs match input fingerprints, exact original counts and decision
signatures. All 26 arms exited zero, persisted every required native decision,
and have zero HA calls, collector errors/drops and unexplained event loss.
OFF/incomplete/unsupported produce no V2 qualification; unsupported retains
`agent bound`. RSS is the registered post-run RSS field, with load-end retained
cache bytes separately exposed. No profile or test ran concurrently with timing.

| Pair | p95 | p99 | Process CPU | RSS | Qualified baseline → cache | Screen |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Normal 1 | -0.6% | -11.5% | +8.2% | +1.2% | 4 → 12 | PASS |
| Normal 2, reverse | +9.9% | -10.7% | +9.9% | +1.2% | 5 → 11 | PASS |
| Normal 3 | -4.9% | -14.3% | +4.5% | +1.1% | 5 → 13 | PASS |
| Normal 4, reverse | +6.7% | +6.8% | **+16.8%** | +2.0% | 5 → 13 | **FAIL CPU** |
| Normal 5 | +2.0% | +0.9% | +7.0% | +1.5% | 5 → 12 | PASS |
| Normal 6, reverse | -5.2% | -18.2% | +4.0% | +1.6% | 5 → 13 | PASS |
| Busy 128+128 | +8.8% | +14.3% | +5.1% | -0.2% | 9 → 17 | PASS |
| Busy 200+56, reverse | +7.6% | +5.4% | +9.5% | +0.8% | 5 → 11 | PASS |
| OFF | -7.0% | -22.2% | -7.6% | -0.3% | 0 → 0 | PASS |
| Incomplete, reverse | -0.4% | +1.5% | -2.2% | +0.4% | 0 → 0 | PASS |
| Unsupported 400 | **+66.3%** | **+53.1%** | **+32.6%** | -4.1% | 0 → 0 | **FAIL** |
| 50/10, reverse | **+22.7%** | **+60.1%** | **+32.9%** | +9.7% | 97 → 175 | **FAIL** |
| 200/30 | -15.4% | -20.8% | **+15.7%** | +3.8% | 47 → 114 | **FAIL CPU** |

The original three active preparation workloads, including all six Normal
repetitions, total consumed **46 → 106** and qualified **43 → 102**. Every active
case improves. Including both smaller controls, consumed is **190 → 395** and
qualified **187 → 391**; both broader totals also exceed 2x. Thus throughput is
not the current blocker. Failed margins remain failures despite the component
speedup, aggregate gains and five passing Normal pairs. Same-revision controls
will quantify variance, not waive any registered failure.

## Same-revision controls and remaining CPU

All 12 additional Normal arms use the identical `cf09e214` source. Every pair
matches inputs/signatures, collector accounting and original decisions.

| Same-revision pair | p95 | p99 | CPU | RSS | Screen |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | -4.3% | -3.0% | -4.5% | +0.1% | PASS |
| 2, reverse | -2.7% | +1.5% | -1.8% | -0.0% | PASS |
| 3 | -29.0% | -17.3% | -24.2% | +0.1% | PASS |
| 4, reverse | -0.2% | +10.5% | -0.5% | -0.2% | PASS |
| 5 | **+17.8%** | **+23.7%** | +11.5% | -1.1% | **FAIL tails** |
| 6, reverse | -9.9% | -30.5% | -11.4% | +0.2% | PASS |

Variance is material, but does not waive any failure or turn the cache into
a release PASS. The previously failed #368 profile was repaired in a separate
diagnostic-only copy: the missing optional accounting helper is installed as a
dormant probe. The baseline runtime is unchanged. Supplemental profiles use
the same compatible harness on all three source trees; none are acceptance.

The following are **disjoint thread CPU ms inside the load interval**. Only
complete writer/preparation spans are included; partial spans and other threads
remain in the residual. Original-agent processing includes its producer capture,
not just model inference. Process CPU is measured independently.

| 50 sensors / 10 roots | Exact #368 | Before cache | Reusable cache |
| --- | ---: | ---: | ---: |
| Process CPU | 339.36 | 426.28 | 434.95 |
| Preparation | 30.74 | 44.28 | 28.43 |
| Native collector `_write` | 80.77 | 138.13 | 155.45 |
| Original-agent processing | 185.64 | 191.34 | 195.00 |
| Remaining process CPU | 42.21 | 52.53 | 56.07 |
| Qualified evidence | 86 | 166 | 171 |
| Preparation turns, including finalization | 11 | 20 | 20 |
| Collector batches, including setup/finalization | 26 | 54 | 52 |
| Native SQL calls during load | 930 | 1594 | 1675 |

Thus about **74.69 of 95.59 extra process CPU ms** versus #368 occurs in
collector writes, while preparation is already 2.31 ms lower. Cache preparation
savings alone cannot explain or close the small-case CPU failure. Producing
more qualified evidence adds genuine validation and persistence work. More
writer turns also repeat transaction/journal setup; these are distinct costs,
not permission to reduce evidence, cadence or persistence frequency.

A separate intrusive writer call tree sees 24 → 52 writes and 69 → 175
`verify_for_write` calls. Its inclusive writer CPU is 504.3 → 886.0 ms;
FeatureJournal `record_batch` 133.5 → 198.5 ms, native revision reads
20.3 → 46.7 ms, per-decision validation 38.7 → 67.2 ms and JSON dumps
32.7 → 58.3 ms. These nested, instrumented totals are not additive or substitutes
for the lightweight table. They identify native transaction/protection queries,
independent evidence verification and repeated record processing, rather than
just RAM copying, as the next writer investigation targets.

The unsupported supplemental load has **zero evidence** in both arms. Process
CPU is 7515.22 → 7987.69 ms, preparation 269.05 → 197.70 ms, collector writes
149.90 → 340.13 ms, original processing 2513.59 → 2713.90 ms and residual
4582.68 → 4735.95 ms. Rejected preparation turns increase 14 → 28. Genuine Busy
work also differs: policy updates 606 → 546, model writes 19 → 18 and Candidate
processed 261 → 250. Candidate queue drops (107 → 122) are explicitly accounted
bounded overload behavior, not collector loss or Candidate Recovery acceptance.
Do not attribute all unsupported CPU/tails to the snapshot, erase background
work, or normalize these results into a passing comparison.

## Allocation windows and rejected secondary optimization

Separate `tracemalloc` diagnostics record process-wide Python allocation windows
during preparation. Concurrent work is included, so these are not exclusive
per-function allocation claims. Maximum window peak growth is 1,698,870 bytes
before and 1,684,656 bytes with reuse (26/34 windows). The cached database retains
73,728 allocated SQLite page bytes; the fresh path has zero retained SQLite pages
after return because it closes the connection. Native allocator overhead is not
covered by tracemalloc: retain the conservative connection/config/page budget
and measured process RSS, rather than treating Python allocation as total memory.

An isolated second candidate in `experiment_bounded_snapshot_copy.py` inlines
exact immutable string mapping keys while preserving the entire snapshot and
all structural/depth/node budgets. It passes 2,000 seeded random differential
value/error comparisons. Ten alternating CPU micro pairs average 4.473 → 4.288
ms/copy; medians 4.444 → 4.329 ms, and two pairs are slower. This is only about
4.1% mean / 2.6% median on a 1,200-state synthetic graph, not an end-to-end gain.
It does not address the measured dominant writer delta or the small-state
control, and it has **not been adopted into production**. No acceptance result
is attached to that experiment. Registry cross-turn reuse also remains rejected
without proof that revision tracking covers all relevant mutable inputs.

## Final code, validation and concrete continuation

`57575bda` adds deterministic disposal even when a synchronously prepared
collector is stopped before its thread starts. Its 35 focused tests pass, but
the 295-test Universal run exposes one adapter compatibility error: a test
adapter has no `close()` method. `eb6650e2` makes lifecycle cleanup conditional
on that optional method, preserving old cache-free adapters. The focused run
including that regression passes **36 tests**. The final full Universal run passes **295 tests in 182.977s**. Both final logs
and the rejected intermediate run are retained. The complete timing matrix remains specifically **cf09e214**;
these later lifecycle/diagnostic changes have not received a fresh full timing
matrix. No final-code performance PASS is claimed.

First unfinished implementation unit: profile the native collector writer with
lightweight, nested exclusive CPU spans around FeatureJournal protection/setup,
per-receipt revision/schema reads, binding validation, serialization and commit.
Then remove one proven repeated operation inside a **single native writer
transaction** (for example repeated statement classification or source-contract
reads), while preserving independent native verification and every per-decision
binding. Any transaction-local reuse must reset on commit, rollback, snapshot
boundary, own writes/DDL and exceptions; test external writers, custom triggers,
retention protection and partial metadata commits before adoption. The profile
does not yet prove one such redesign safe or sufficient. Do not force it or
adjust cadence, workload, retention, training or qualification to obtain PASS.
Freeze the final source and rerun the complete registered matrix after an actual
writer improvement; preserve the current failed results and same-revision series.

Runtime files changed: `universal_predecision.py`, `universal_history_collector.py`.
Tests: `test_revision_validated_arbiter_snapshot.py` and the frozen original
reference module. Host-only tools: the qualified-evidence profiler, serial study
and supplement runners, analyzer, memory profiler and isolated copy experiment.
Reports and raw evidence contain the remaining changes. Version stays 0.14.232.

Candidate Recovery, Production Composition, actual-data migration, inherited
retention regression, four-hour soak and Pi/live HA remain unaccepted.


## Evidence and CI receipt

[CI 42235276985](https://github.com/mpuchara/HomeMind-HA/actions/runs/42235276985)
passed for measured `cf09e214`. [CI 42237574777](https://github.com/mpuchara/HomeMind-HA/actions/runs/42237574777)
for final code `eb6650e2` was still in progress at the saved receipt. Do not
promote an ancestor's CI or timing result to final-code acceptance.

[Parsed summary](benchmarks/FINAL_RELEASE_GATE_D_SNAPSHOT_SUMMARY.json),
[raw archive](benchmarks/FINAL_RELEASE_GATE_D_SNAPSHOT_RAW.tar.gz),
[checksum](benchmarks/FINAL_RELEASE_GATE_D_SNAPSHOT_RAW.tar.gz.sha256) and
[CI receipt](benchmarks/FINAL_RELEASE_GATE_D_SNAPSHOT_CI.json) preserve the
registered protocols, exact source/harness manifests, commands, all 38 acceptance
and same-revision arms, before/after profiles, compatible baseline retry,
allocation windows, copy experiment and failed/final test logs. The supplemental
profiles are diagnostic only, not extra passing acceptance arms. Local ext4
sources and raw originals remain under `~/homemind-gated-20261106`.
