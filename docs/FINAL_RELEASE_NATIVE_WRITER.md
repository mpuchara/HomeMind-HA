# Receipt CPU investigation checkpoint — 2026-11-06

**Gate D remains BLOCKER.** The isolated derived-universe experiment
`4dc2d2d630aa8ee144c0e7d8492173c462647a34` was implemented, tested and **rejected**:
it has no repeatable process-CPU improvement versus previous production.
Restoration commit `c1dbfd9fd468a9ea24c114cdaf044fbe5b0d8244` returns the add-on tree
to `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`, exactly the `9c245577` production tree.
Version **0.14.232** is unchanged. Reporting-only HEAD is recorded in PR #369.

[Checkpoint CI 42305210953](https://github.com/mpuchara/HomeMind-HA/actions/runs/42305210953)
is **SUCCESS for exact `56fd84f8d8bb23fdcb88e127c4f60a07594cb26b`**: 3,176 tests
on Python 3.11 and 3,176 on 3.13, required benchmarks and image build/smoke;
package/release is skipped. Earlier reporting `630f4453` CI `42304381708` also
passes. [Exact CI receipt](benchmarks/FINAL_RELEASE_RECEIPT_CI.json) distinguishes
both sources. Neither run certifies rejected experimental `4dc2d2d6` or a final
release. Later receipt/reporting commits leave add-on, tests and tools unchanged.

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

# Native writer investigation — 2026-11-06

Gate D remains **BLOCKER**. No release, version, cadence, qualification or
throughput requirement changes are authorized by these diagnostics.

Resume checked at local and fetched origin HEAD `79bf067432c2cf6b7e0c4122352a6c609bb36067`;
worktree was clean and `git diff eb6650e2 HEAD -- adaptive_ai` was empty.

## Observation before implementation

Disposable WSL ext4 root: `/home/mpuchara/homemind-writer-20261106`.
`compatible-profiles/small50-current.json` uses the current identical-compatible
benchmark, 24 ticks, 400 ms period, fanout 16, 50 sensors/10 roots, native persistence.
The diagnostic-only AST probe times bounded operations using thread CPU and
records aggregate call-site counts. It records transaction context entry/exit,
journal preparation/protection/insertion, revision and binding validation,
affected declaration/topology, receipt construction/serialization/persistence.
It retains no event payloads. Instrumentation overhead is included; this is
not latency or end-to-end performance acceptance.

All **26** observed journal protection reads found an empty
`feature_window_entities` table in the authoritative writer transaction.
The original grouped protection queries consumed **9.605 ms CPU** in total.
Repeated SQL text construction was below 0.1 ms in an earlier diagnostic;
caching that text is not justified as the primary writer improvement.

Proposed first removal: when a fresh SELECT in the SAME BEGIN IMMEDIATE
transaction proves there are no window/entity links, every original protection
join is empty and its result is exactly zero. Use those zeros directly. Keep
the complete original grouped query for every nonempty table, regardless of
whether its rows appear relevant. Do not retain the emptiness result beyond
this invocation. External writers cannot add links while BEGIN IMMEDIATE owns
the writer reservation. No native write intervenes between the empty check and
the original observation insertion. Insertion triggers remain visible to the
next batch. The oracle computes protection before those triggers as well.

The frozen pre-change method is `tests/reference/journal_record_batch_79bf0674.py`.
Compare exact keys and every journal/window/link column, including floating
protection values, duplicates, late samples, overlapping/disjoint windows,
multiple agents/shared sources, insertion-trigger changes and rollback.
No revision, schema, causal binding or receipt verification is reused/skipped.

Earlier probes are preserved, not acceptance: `initial.*` includes preparation
calls and per-item instrumentation; `rejected-intrusive/` has excessive per-item
overhead; `harness-mismatch-profiles/` has differing harness hashes and includes
an interrupted arm. The corrected probe excludes per-item calls, scopes to
writer execution and requires a common harness placed inside each disposable
source tree. Never include those rejected measurements in the acceptance matrix.

## Implemented candidate and correctness

Production/performance source: `9c245577727c93ede3c59100f11844bf336acb1a`.
Add-on tree: `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`.
Version remains **0.14.232**. PR #369 stays Draft, based on exact #368
`951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.

`FeatureJournal.record_batch()` implements the proven empty-link fast path.
Nonempty tables retain the original grouped query verbatim and pay one extra
bounded read. This optimization applies only to an actually empty native link
table; it is not evidence of a speedup on a populated live installation.
Observation preparation, insertion/upsert, pruning, admission, transaction
boundaries and commit/rollback are unchanged. The frozen oracle compares every
journal/window/link column and exact event keys/JSON strings.

Preparation now reads at most 257 native agent rows before copying RAM. Paused
Candidates count toward the existing 256 bound, even when absent from the RAM
active-agent map. The count is never retained or used to qualify evidence; the
original later native bound/schema/revision checks remain. No Engine/context
lock spans this preflight. Original inference, events and collector accounting
are unchanged. A rejected source may become supported on the very next turn.

No verified-source cache was added to the writer. Independent per-decision
revision/schema checks, affected declarations, topology/binding verification
and receipt serialization remain. Owning BEGIN IMMEDIATE alone does not prove
immutability across internal writes, custom triggers, DDL or partial commits;
the profile did not justify weakening those checks. The two JSON encodings
serve different persisted contracts and remain intact.

Local WSL Python 3.12.3 validation on disposable data:

- Final production: **295 Universal PASS**, 148.095 s.
- Focused writer/predecision/snapshot/collector run: **88 PASS**, 46.516 s.
- New differential/preflight cases including external writer exclusion:
  **10 PASS**, 13.653 s. The external writer is denied before commit; after
  commit its newly added link changes the next batch's protection as required.
- Observation contract and edge cases: **19 PASS**, 1.544 s.

These runs overlap; do not sum them as unique test counts. The earlier
295-test/239.634 s run belongs to the preliminary RAM-hint preflight variant,
not the final native-only preflight. All logs are retained.
[CI 42277851913](https://github.com/mpuchara/HomeMind-HA/actions/runs/42277851913)
is **SUCCESS** for exact `9c245577`: Python 3.11/3.13, required benchmarks and
image build/smoke. The package/release job is skipped. Its receipt is adjacent
to the raw evidence. This does not certify a later reporting SHA automatically.

## Complete registered acceptance matrix

All **26 arms / 13 pairs** completed on frozen ext4 trees with identical common
harness and original fixture. Original 24 x 400 ms, fanout 16, alternating order,
six Normal pairs, Busy 128+128 and 200+56, OFF/incomplete/400 and smaller controls
are preserved. Limits remain +10% p95, +15% p99/CPU and +10% RSS.

| Pair | CPU | p95 | p99 | RSS | Screen |
| --- | ---: | ---: | ---: | ---: | --- |
| Normal 1 | +7.9% | -1.9% | -0.7% | +3.4% | PASS |
| Normal 2, reverse | **+19.7%** | **+15.0%** | +6.6% | +1.4% | FAIL |
| Normal 3 | **+21.3%** | **+17.8%** | +8.6% | +1.7% | FAIL |
| Normal 4, reverse | +9.9% | -0.3% | -4.3% | +3.2% | PASS |
| Normal 5 | +10.3% | -2.7% | -3.0% | +3.6% | PASS |
| Normal 6, reverse | **+17.6%** | +6.0% | +5.2% | +1.3% | FAIL |
| Busy 128+128 | +1.5% | +5.0% | +14.5% | +1.6% | PASS |
| Busy 200+56, reverse | +4.1% | +5.1% | -4.9% | +1.4% | PASS |
| OFF | -0.1% | +0.6% | +10.8% | -0.1% | PASS |
| Incomplete, reverse | -7.7% | -19.9% | -35.0% | +1.2% | PASS |
| Unsupported 400 | -1.8% | +5.8% | +8.4% | -1.1% | PASS screen, still unsupported |
| 50/10, reverse | **+45.6%** | **+38.1%** | +1.1% | +1.4% | FAIL |
| 200/30 | +9.1% | -24.0% | -9.0% | +0.6% | PASS |

**Gate D remains BLOCKER: 9/13 screens pass.** Inputs and original signatures
match in every pair; every arm has zero HA service calls, collector errors,
collector drops and event drops. Native decisions remain persisted. Negative
controls remain unqualified, including 400 with `agent bound`.

Primary aggregate consumed is **43 -> 112**, qualified **41 -> 107**. Including
smaller supported controls: consumed **170 -> 401**, qualified **168 -> 396**.
Both totals exceed 2x and every active supported case improves. Throughput
acceptance remains satisfied; no cadence, batching or evidence-admission
reduction was used to obtain the writer improvement. These gains waive no margin.

The previous `cf09e214` matrix and its four failures remain historical evidence;
none of those timings is attributed to `9c245577`. Unsupported and 200/30 clear
this new screen, but Normal failures are more frequent. Cross-series differences
are not a causal ablation or proof that a prior failure is permanently resolved.

## Bounded component diagnostics

The four-arm diagnostic compares exact #368, pre-cache `4b989c69`, reporting
source `79bf0674` (production `eb6650e2`) and candidate `9c245577`. All use the
same compatible harness. The 16 processes exited zero. Aggregate writer CPU
includes setup/finalization, tracing/probe overhead and unequal batch/evidence
counts; it is **not acceptance** and does not isolate the whole process delta.

| Workload | #368 writer CPU ms | Pre-cache | Current | Candidate | Current -> candidate qualified |
| --- | ---: | ---: | ---: | ---: | ---: |
| 50/10 | 144.03 | 224.87 | 247.70 | 201.96 | 175 -> 175 |
| 1000/200 | 803.75 | 1005.27 | 909.58 | 864.88 | 13 -> 12 |
| 200/30 | 250.23 | 379.65 | 366.59 | 299.20 | 114 -> 114 |
| Unsupported 400 | 874.37 | 1053.04 | 1019.36 | 1088.89 | 0 -> 0 |

The unsupported candidate has 180 writer batches versus 167 on the current
source; do not attribute its aggregate CPU increase to one query or normalize
away its background work. Raw per-site exclusive CPU, transaction entry/exit,
revision/schema/declaration/topology call counts, batch sizes, traced SQL verbs,
upserts and persisted receipts are retained. SQL trace events include trigger
expansion; they are not SQLite VM instruction counts. Additional diagnostic
empty checks are separately timed and are absent from acceptance runs.

## Same-revision variance and independent cardinality ablation

Six additional alternating Normal pairs use identical `9c245577` production.
Four pass and two fail their individual screen:

- Pair 4: CPU **+33.8%**, p95 **+42.5%**, p99 **+77.7%**.
- Pair 5: CPU +6.0%, p95 **+11.6%**, p99 +10.2%.

All inputs/signatures and safety accounting match. This is evidence of material
run-to-run variance, not grounds to waive the registered failures.

The separate 400-config ablation changes **only** `universal_predecision.py`:
both arms have the optimized journal, and the control uses the original
`79bf0674` predecision module. Two alternating pairs retain 24 x 400 ms/fanout 16,
200 roots plus 200 Candidates, native persistence and all background work.

| Preflight versus writer-only control | CPU | p95 | p99 | Qualified |
| --- | ---: | ---: | ---: | ---: |
| Pair 1 | -1.1% | +2.8% | +2.3% | 0 -> 0 |
| Pair 2, reverse | +1.6% | -0.05% | -0.36% | 0 -> 0 |

Every original decision/signature matches, rejection remains `agent bound`, and
collector errors/drops remain zero. These results do **not** demonstrate a
repeatable aggregate CPU improvement attributable to preflight. Tests prove
that unsupported sources avoid RAM copying; aggregate CPU still includes real
background work. Pair 1 policy updates/model writes/Candidate processed are
767/24/296 -> 779/25/294; pair 2 is 703/22/281 -> 765/24/290. Bounded Candidate
queue drops are 72 -> 78 and 87 -> 78, with zero collector drops. These are
accounted overload behavior, not Candidate Recovery acceptance or missing
collector events. Do not normalize the additional background work away.

## Frozen-source checks and continuation

Post-run checks compare **253 add-on files in each arm** against its exact Git
archive: no differences. All prior Python runtime/tools manifests for profiles,
matrix and variance remain identical. The cardinality ablation differs in
exactly the predecision file, with the control matching the original current
source. No original benchmark source snapshot was modified.

The final external-writer test additionally asserts `SQLITE_BUSY` specifically
and closes the external connection before Windows temporary-directory cleanup.
The final rerun passes **10 tests in 3.909 s**; this test-only refinement does
not change measured production source or the add-on tree.

Reproduction tools: `profile_native_writer.py`, `run_native_writer_profiles.py`,
`run_gate_d_snapshot_study.py`, `run_native_writer_preflight_ablation.py`,
`analyze_native_writer_study.py`, `verify_native_writer_study.py`.
Raw protocols, commands, manifests, logs, JSONL, rejected probes and test logs
are in `docs/benchmarks/FINAL_RELEASE_NATIVE_WRITER_RAW.tar.gz` with adjacent
SHA-256. Parsed results are `FINAL_RELEASE_NATIVE_WRITER_SUMMARY.json`; CI is
`FINAL_RELEASE_NATIVE_WRITER_CI.json`. A first summary attempt failed on the
OFF control's intentionally empty count dictionary; aggregation now handles
that representation without changing any raw result or expected test output.

This unit removes a proven redundant native protection operation and safely
rejects unsupported cardinality before RAM preparation. It **does not close
Gate D**. Continue with per-qualified-receipt validation/serialization and
native metadata transaction setup costs; preserve exact receipt bytes and
independent native verification until a strictly bounded invariant interval is
proven, including own writes/triggers/DDL and partial commits. Do not change
cadence, workloads, admission, throughput targets or acceptance margins.
Candidate Recovery, full production composition, actual-data migration,
inherited retention/rollback behavior, four-hour soak and Pi/live HA remain
unaccepted. Collector remains default-off; no deployment or release occurred.
