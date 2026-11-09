# Receipt writer CPU profile — 2026-11-06

This is thread-CPU component diagnosis, **not performance acceptance**.
Exact source identities and correctness/disposition are in
[the optimization report](FINAL_RELEASE_RECEIPT_OPTIMIZATION.md).

## Method and limits

`tools/profile_native_writer.py` compiles temporary diagnostic AST copies of
bounded writer functions. It does not modify production to accommodate profiling.
Each named call/context records calls, inclusive and exclusive thread CPU.
Per-entity generator/comprehension probes are omitted to limit distortion.
Nested binding/revision wrappers are installed before `verify_for_write()` so
their own breakdown remains visible. SQL trace counters retain verbs only,
including trigger-expanded repetitions; they are not SQLite VM instruction counts.
Configuration before trace callback installation is excluded from SQL counts.

The unchanged harness is loaded as a diagnostic module; a wrapper around its
original `stream()` identifies setup, load and finalization. Neither fixture nor
stream body is edited. A batch crossing a phase boundary retains its entry phase.
This is a stated component boundary, not an assertion that every nanosecond
matches the harness's slightly wider process-CPU accounting interval.

Context entry includes Store connection borrowing/configuration and storage-batch
entry; exit includes native commit/storage checks/cleanup. Connection-session
startup outside `_write()` is outside this writer profile. Exception exit counts
describe actually observed exceptions; no rollback CPU is invented for successful
runs. Capture includes nested JSON construction, byte bounds, numeric validation
and allocation; outer receipt encoding and SHA-256 have separate call sites.
`asdict()` and insertion preparation are measured independently. Allocation and
Python/probe bookkeeping outside named calls remain in the enclosing exclusive
residual; the probe does not pretend to isolate allocator CPU precisely.

Source revision, declaration revision and topology lookup counts are retained
without event payloads. Distinct hashes across agents/sources are not automatically
interpreted as an intervening native change. A source revision count describes
the actual verification reads, not an immutable-transaction proof.

## Pre-implementation evidence

The initial phase-scoped diagnostic ran before production edits, using exact
#368 and the unchanged previous production tree under the same compatible harness.
Its raw results are preserved as `initial-profiles/`. The final common probe adds
metadata/batch-kind counters and reruns all three frozen arms consistently; do
not compare its instrumented timings as unprofiled acceptance.

The initial Normal load writer used **50 -> 143 attempts**, **167.97 -> 268.77 ms**
CPU (#368 -> previous production), with **5 -> 13** complete native receipts.
Setup alone used **770.24 -> 733.30 ms**, which must not be attributed to the
load interval. For 50/10, the load writer used **124.28 -> 197.35 ms**, with
**104 -> 175** persisted complete receipts (some finalized after load).

In previous-production 50/10 load, outer `encoded()` was **16.020 ms / 230 calls**,
nested `capture()` JSON encoding **12.141 ms / 166 calls**, binding action-mask
validation **3.734 ms / 166**, declaration validation **2.918 ms / 166** and
configuration encoding **2.744 ms / 166**. `asdict()` is much smaller than the
two JSON contracts. The repeated universe work contributes **1.716 ms encoding**,
**0.379 ms hashing**, **0.680 ms sorting**, **0.269 ms set construction**, plus
membership set construction (**0.332 ms / 332 set calls**, including affected ids).
These are instrumented call-site CPU observations, not savings forecasts.

The largest established avoidable *receipt computation* is recomputing that
unchanged universe representation within a consecutive decision interval.
It is small relative to total writer CPU. Native transaction/storage setup and
commit, outer JSON, and the two required persisted representations remain larger
costs. No claim is made that eliminating a few milliseconds can by itself remove
the registered +45.6% 50/10 process-CPU failure.

Final common-probe tables, SQL counts and exact fingerprints are recorded below. Raw per-site rows preserve inclusive/exclusive CPU,
calls and phase boundaries; normalized per-batch/capture figures are diagnostics,
not a way to normalize away extra real background work.

## Final common-probe results

All CPU values below are instrumented milliseconds. Complete receipt counts are
the final persisted native inventory; load captures/outer encodes can be fewer
because the final drain is separate. No profiled value is an acceptance result.

| Workload/arm | Setup CPU | Load CPU | Finalization CPU | Load batches | Load outer encodes | Final decisions / complete V2 | CPU/load batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| normal-base | 951.66 | 150.39 | 9.51 | 44 | 368 | 384 / 5 | 3.418 |
| normal-before | 829.55 | 319.37 | 4.61 | 145 | 368 | 384 / 11 | 2.203 |
| normal-source | 852.91 | 365.92 | 4.69 | 161 | 368 | 384 / 13 | 2.273 |
| small50-base | 50.32 | 164.60 | 16.97 | 27 | 230 | 240 / 113 | 6.096 |
| small50-before | 50.38 | 238.06 | 13.98 | 52 | 230 | 240 / 175 | 4.578 |
| small50-source | 41.79 | 223.56 | 9.69 | 54 | 230 | 240 / 175 | 4.140 |

The per-site raw rows also permit CPU per outer decision encode or complete
capture. Dividing total writer CPU by only qualified receipts would conflate
unqualified decisions, native observations, barriers, transaction setup and
retries; it is not an estimate of marginal qualification cost.

| Workload/arm | Load SELECT | Load PRAGMA | Load BEGIN / COMMIT | Load INSERT / UPDATE / DELETE | Source revisions (reads) | Distinct declaration / topology lookups |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| normal-base | 105 | 565 | 59 / 59 | 606 / 26 / 3 | 1 (5) | 5 / 1 |
| normal-before | 231 | 1539 | 172 / 170 | 739 / 48 / 3 | 1 (11) | 11 / 1 |
| normal-source | 255 | 1688 | 187 / 187 | 739 / 48 / 3 | 1 (13) | 13 / 1 |
| small50-base | 399 | 397 | 41 / 40 | 590 / 26 / 0 | 1 (113) | 10 / 1 |
| small50-before | 621 | 854 | 78 / 77 | 644 / 48 / 0 | 1 (175) | 10 / 1 |
| small50-source | 623 | 872 | 80 / 79 | 646 / 48 / 0 | 1 (175) | 10 / 1 |

These are callback events, including trigger expansion and diagnostic protection
queries. A BEGIN/COMMIT count difference is not by itself proof of a lost
transaction: retry/exception and phase-boundary details remain in raw rows.
Distinct declaration hashes reflect different agents as well as potential changes;
they must not be presented as a count of declaration mutations.

### Component breakdown of the two previous-production loads

Calls / inclusive / exclusive CPU are retained for every measured site in the raw
profiles. The following largest exclusive sites show where the remaining writer
CPU goes (including probe residual).

**small50-before**

| Site | Calls | Inclusive ms | Exclusive ms |
| --- | ---: | ---: | ---: |
| `universal_history_collector.HistoricalTrainingCollector._write` | 52 | 238.057 | 19.806 |
| `universal_history_collector.HistoricalTrainingCollector._write:147:encoded` | 230 | 19.027 | 19.027 |
| `observation_contract.FeatureJournal.record_batch:81:_insert_journal_rows` | 26 | 17.872 | 17.872 |
| `universal_training_data.HistoricalDecision.capture:16:json.dumps` | 166 | 14.823 | 14.823 |
| `universal_history_collector.HistoricalTrainingCollector._write:16:_writer_probe_call('universal_history_collector.HistoricalTrainingCollector._write:16:self.store.conn', self.store.conn):exit` | 51 | 14.273 | 14.273 |
| `universal_history_collector.HistoricalTrainingCollector._write:21:insert_bounded_rows` | 51 | 11.727 | 11.727 |
| `observation_contract.FeatureJournal.record_batch:28:_writer_probe_call('observation_contract.FeatureJournal.record_batch:28:self.store.conn', self.store.conn):exit` | 26 | 9.333 | 9.333 |
| `universal_history_collector.HistoricalTrainingCollector._write:3:require_storage_available` | 52 | 7.393 | 7.393 |
| `universal_affected_sensors.validate` | 166 | 15.431 | 6.976 |
| `universal_training_data.HistoricalDecision.capture` | 166 | 23.766 | 6.438 |
| `universal_history_collector.HistoricalTrainingCollector._write:16:_writer_probe_call('universal_history_collector.HistoricalTrainingCollector._write:16:self.store.conn', self.store.conn):enter` | 52 | 5.968 | 5.968 |
| `observation_contract.FeatureJournal.record_batch:28:_writer_probe_call('observation_contract.FeatureJournal.record_batch:28:self.store.conn', self.store.conn):enter` | 26 | 5.137 | 5.137 |

**normal-before**

| Site | Calls | Inclusive ms | Exclusive ms |
| --- | ---: | ---: | ---: |
| `universal_history_collector.HistoricalTrainingCollector._write:16:_writer_probe_call('universal_history_collector.HistoricalTrainingCollector._write:16:self.store.conn', self.store.conn):exit` | 143 | 48.354 | 48.354 |
| `universal_history_collector.HistoricalTrainingCollector._write:21:insert_bounded_rows` | 143 | 34.684 | 34.684 |
| `universal_history_collector.HistoricalTrainingCollector._write` | 145 | 319.373 | 31.277 |
| `universal_history_collector.HistoricalTrainingCollector._write:16:_writer_probe_call('universal_history_collector.HistoricalTrainingCollector._write:16:self.store.conn', self.store.conn):enter` | 145 | 28.664 | 28.664 |
| `observation_contract.FeatureJournal.record_batch:81:_insert_journal_rows` | 26 | 24.378 | 24.378 |
| `universal_history_collector.HistoricalTrainingCollector._write:3:require_storage_available` | 145 | 19.276 | 19.276 |
| `universal_history_collector.HistoricalTrainingCollector._write:147:encoded` | 368 | 15.932 | 15.932 |
| `universal_history_collector.HistoricalTrainingCollector._write:16:_writer_probe_call('universal_history_collector.HistoricalTrainingCollector._write:16:c.storage_batch', c.storage_batch):exit` | 143 | 10.906 | 10.906 |
| `universal_history_collector.HistoricalTrainingCollector._write:16:_writer_probe_call('universal_history_collector.HistoricalTrainingCollector._write:16:c.storage_batch', c.storage_batch):enter` | 145 | 10.399 | 10.399 |
| `universal_history_collector.HistoricalTrainingCollector._write:17:c.execute` | 145 | 8.791 | 8.791 |
| `observation_contract.FeatureJournal.record_batch:28:_writer_probe_call('observation_contract.FeatureJournal.record_batch:28:self.store.conn', self.store.conn):exit` | 26 | 8.171 | 8.171 |
| `universal_history_collector.HistoricalTrainingCollector._write:32:c.execute` | 143 | 7.293 | 7.293 |

## Fingerprints

Full normalized Python manifests precede the final common-probe runs in
`protocol.json`. The registered matrix additionally freezes its own manifests.
Baseline/previous/candidate sources are independent disposable ext4 trees.

- Common harness SHA-256: `6b604002e0d36a596f377f9620e87159ea55719d38d886db96506e4b5c1976f1`.
- Common fixture SHA-256: `1c796f8ec795ec188b0a72317d996461248b9eba757c7f0cf7f5e1f2e507a729`.
- Diagnostic probe SHA-256: `01e428da4f52c397defd3c448504ac6a38882c1cb82821084dd00f6bc246287f`.

## Additional native-budget attribution after registered measurements

A separate optional probe runs the previous production `9c245577`, whose add-on
tree is the restored production tree. It preserves the same compatible fixture
and load, with extra method/call instrumentation in `StorageBudget.sample()`,
`check()` and `observe_pages()`. Native connection lifecycle methods are wrapped
without recompiling their closures. Its timings include additional instrumentation
and must not be compared numerically with the lighter probe or acceptance CPU.
Raw source/probe hashes and receipts are in `native-budget/`.

| Load / operation | Calls | Inclusive CPU ms | Exclusive CPU ms |
| --- | ---: | ---: | ---: |
| Normal writer | 134 | 443.224 | 28.352 |
| Normal sample | 619 | 100.982 | 44.981 |
| Normal check | 886 | 102.071 | 22.134 |
| Normal observe_pages | 318 | 73.311 | 21.248 |
| Normal connection exit | 161 | 86.280 | 23.708 |
| 50/10 writer | 52 | 275.353 | 18.128 |
| 50/10 sample | 285 | 33.926 | 15.651 |
| 50/10 check | 462 | 36.996 | 9.179 |
| 50/10 observe_pages | 152 | 15.219 | 6.683 |
| 50/10 connection exit | 79 | 29.569 | 14.540 |

Inclusive rows overlap: checks and connection exits contain some samples/page
accounting. Do not sum all rows. Normal sampling performs **1,238 file-size reads**
(619 database + 619 WAL) and **619 disk-usage reads**. Their call-site CPU is
15.566 + 16.887 ms and 11.268 ms respectively. Reconstructing the WAL Path is
4.349 ms / 619 calls. Page accounting performs **954 native page PRAGMAs**
(318 each for page_count, freelist_count and page_size); execute CPU is
17.275 + 8.045 + 13.183 ms, with fetch/other work recorded separately.
No load-phase explicit commit/rollback/close call is observed: the persistent
worker borrows a connection, and its guarded context exit performs commit.
Setup/finalization lifecycle calls and actually observed SQL verbs remain raw.

**Next proven cost:** fresh storage-budget/filesystem sampling, native page
accounting and guarded transaction entry/exit, rather than receipt-universe
computation. This is a cost attribution, not proof that these checks can be
removed. Required fresh sampling at entry/exit/commit and the 50 ms / 1 MiB
within-batch limits remain authoritative. Investigate redundant Python/statement
preparation while retaining those exact checks. Do not replace their native
values with transaction-local assumptions merely because BEGIN IMMEDIATE is held.
The experimental derived-universe optimization was rejected and restored by
`c1dbfd9f`; the complete failed matrix and continuation are in the optimization
report. Neither this deeper profile nor the pure component speedup closes Gate D.
