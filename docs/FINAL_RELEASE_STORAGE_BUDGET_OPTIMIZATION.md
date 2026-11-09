# Gate D storage budget study

The study starts from reporting `765f8201105e969fbb3e0bb0363e1440bb48c94c` and retained runtime `9c245577727c93ede3c59100f11844bf336acb1a` with add-on tree `cd0c22a0193f91cc3b9ac7dc7e477dbf73aa7f0f`. GitHub Draft PR #369, its branch and the clean local worktree matched at the start. The registered acceptance comparator is #368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.

## Page accounting

Three direct PRAGMAs and one statement joining the three table-valued PRAGMAs return equal values on populated WAL databases after external allocation, deletion, rollback and checkpoint, and during an open write transaction. These measurements use the actual Windows and WSL Python SQLite libraries.

| SQLite and state | CPU µs per observation, direct → combined | Wall µs, direct → combined | Execute calls | VM progress callbacks |
| --- | ---: | ---: | ---: | ---: |
| 3.38.4 Windows autocommit | 20.31 → 16.02 | 21.01 → 17.48 | 3 → 1 | 15 → 38 |
| 3.38.4 Windows write transaction | 9.77 → 7.81 | 10.07 → 7.83 | 3 → 1 | 15 → 38 |
| 3.45.1 WSL autocommit | 10.21 → 7.55 | 10.21 → 7.54 | 3 → 1 | 20 → 43 |
| 3.45.1 WSL write transaction | 5.88 → 5.72 | 5.88 → 5.72 | 3 → 1 | 20 → 43 |

CPU and wall columns are descriptive medians of six alternating pairs of 20,000 observations. The VM counter comes from a separate interval-one progress handler. The first probe used a Python generator for the reference and remains archived as a preliminary run. The direct-call measurements above correct that reference overhead. The first WSL authorizer command failed during shell argument handling and left an empty output. The retry completed.

**Decision: REJECT.** On both SQLite versions, an authorizer that permits native PRAGMAs and denies `SQLITE_SELECT` allows the existing observation and rejects the combined statement with `SQLITE_AUTH` (23). The changed error behavior excludes the combined statement from the runtime candidate.

## Filesystem sampling

Experimental commit `f8c80136a1e15dad4afee1da649d4b7a8972da1d` and add-on tree `760f28d72e2fa59ec0e37590f9932e9b64825eb5` reuse lexical WAL and directory path objects. The database size, WAL size and free-space reads still execute for every successful sample.

The nine differential cases compare complete sampled state, write check outcome and filesystem call order. They cover missing files, changing sizes, normal, soft and hard disk pressure, soft and hard WAL pressure, database page pressure, failed stat and failed free-space reads. All outputs match. The 27 existing storage safety tests pass on the candidate. Two alternating component blocks on WSL give retained medians of 8.76 and 9.02 µs per sample and candidate medians of 5.19 and 4.86 µs.

**Decision: REVERT.** Normal CPU changes by −22.74% and +9.17% in opposite execution orders. 50/10 changes by +1.49% and −0.004%. The Normal reverse pair also misses the p95 screen at +13.19% and consumes 11 instead of 13. The unprofiled results fail to establish a repeatable CPU improvement. Commit `b8e11ab4db5ecc76aa31900962286e55a0b27aa2` restores the retained add-on tree.

## Statement classification

The evidence diagnostic counts roughly 21,000 calls to `_clean_sql` in a complete Normal process, including initialization and finalization. The timed wrapper attributes 72–80 ms to those calls. This is instrumented whole-run CPU and cannot be used as a load-only effect size.

Experimental commit `5c632db4e88c2f12d21a28a93a48e29b0dc031d9` and add-on tree `80132ff577dc0316e57c352584bd1bca4c2bacb5` normalize SQL once and inspect the PRAGMA name only for PRAGMA statements. Each execution makes its live admission and page-dirty decisions from the original conditions. The 250 differential classifications cover read and write verbs, CTEs, comments, case, whitespace, qualified and unknown PRAGMAs and malformed statements. Six alternating pairs measure 1.94 → 1.00 µs CPU per classification.

Candidate validation passes 27 storage tests, 2 receipt writer differential tests and 295 Universal tests. The existing suites exercise page and WAL limits, rollback, pressure transitions, busy handling, writer recovery, evidence revisions and binding, retention, Correct/Undo and Candidate state. The 250 lexical cases compare classifications directly. The integration suites exercise runtime behavior against their existing assertions.


**Decision: REVERT.** Both 50/10 pairs reduce process CPU, by 4.56% and 0.84%. Normal changes by −3.03% and +6.59%, with +17.62% p99 in the reverse pair. Commit `e822381ea6efc5081d207f179ec848bdd0079f62` restores the retained add-on tree.

## Unprofiled screen receipts

The existing screen driver used two alternating pairs for each workload and separate disposable databases. Each arm ran 24 ticks at 400 ms with configured fanout 16. Values are retained → experimental. The screen compares the experiments to the retained runtime. The Gate D baseline remains #368.

| Experiment | Pair | CPU s | p95 ms | p99 ms | RSS MB | Consumed | Qualified | Screen |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| path | small50-1 | 0.391 → 0.397 | 8.15 → 7.51 | 33.78 → 35.37 | 60.48 → 65.14 | 166 → 175 | 166 → 175 | PASS |
| path | small50-2 | 0.446 → 0.446 | 8.59 → 8.86 | 44.97 → 41.08 | 60.54 → 61.32 | 175 → 175 | 175 → 175 | PASS |
| path | normal-1 | 2.406 → 1.859 | 103.70 → 74.93 | 128.76 → 91.12 | 79.88 → 79.70 | 13 → 13 | 13 → 13 | PASS |
| path | normal-2 | 1.871 → 2.042 | 74.75 → 84.60 | 96.52 → 96.23 | 78.84 → 79.76 | 13 → 11 | 13 → 11 | FAIL |
| sql | small50-1 | 0.389 → 0.371 | 6.89 → 7.06 | 31.88 → 34.92 | 61.07 → 61.58 | 166 → 166 | 166 → 166 | PASS |
| sql | small50-2 | 0.391 → 0.387 | 7.41 → 7.92 | 39.87 → 31.43 | 61.27 → 61.93 | 166 → 175 | 166 → 175 | PASS |
| sql | normal-1 | 1.603 → 1.554 | 62.29 → 57.97 | 70.36 → 63.70 | 79.55 → 79.68 | 13 → 13 | 13 → 13 | PASS |
| sql | normal-2 | 1.580 → 1.684 | 61.63 → 63.80 | 66.63 → 78.36 | 79.57 → 79.62 | 13 → 13 | 13 → 13 | FAIL |

Every pair passes original input and decision parity and records zero HA calls, collector errors, collector drops and event drops. The screens leave both experiments below the repeatability requirement. Gate D remains **BLOCKER**. The 13 registered acceptance pairs await a repeatable improvement. The earlier retained runtime's 9/13 result remains historical evidence for the exact add-on tree.

[Retained-runtime history](FINAL_RELEASE_NATIVE_WRITER.md) · [Evidence timing](FINAL_RELEASE_EVIDENCE_THROUGHPUT_ANALYSIS.md)

## Instrumented storage controls and writer cost

The additional 50/10 diagnostic uses the native budget probe on the retained runtime and the classification experiment. Its measurements include probe overhead and are reported separately from the unprofiled screen. The different batch counts affect the number of calls to the existing guards.

| Load metric | Retained | Classification experiment |
| --- | ---: | ---: |
| Process CPU s | 0.501 | 0.490 |
| Writer batches | 54 | 51 |
| Writer CPU ms | 212.23 | 214.06 |
| Preparation CPU ms | 33.21 | 31.07 |
| Filesystem samples | 289 | 278 |
| Budget checks | 460 | 451 |
| Page observations | 154 | 148 |
| Direct page PRAGMA reads | 462 | 444 |
| SQLite SELECT trace events | 622 | 619 |
| SQLite PRAGMA trace events | 863 | 839 |
| SQLite BEGIN / COMMIT trace events | 79 / 78 | 77 / 75 |
| Consumed / qualified | 175 / 175 | 175 / 175 |

The SQL trace counts callbacks on instrumented connections. Triggers may add events and the trace scope does not cover every SQLite call. Inclusive CPU is 27.00 → 26.24 ms for sampling, 30.11 → 29.04 ms for checking and 13.12 → 13.09 ms for page observations. These nested spans overlap. The lower process CPU in this profile accompanies slightly higher writer CPU and equal throughput.


## Transaction parity and same-revision controls

All three runtime sources produce identical transaction receipts for successful commit, deferred foreign key failure at commit (787), rollback, an external writer causing SQLITE_BUSY (5), page growth, WAL checkpoint and reopen. Observed used bytes rise from 12,288 to 77,824. Reopen retains the same two rows and valid foreign keys.

Both roles of each registered variance pair use the retained runtime.

| Pair | CPU delta | p95 delta | p99 delta | RSS delta | Consumed | Qualified | Screen |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| variance-1 | +3.25% | +6.92% | +14.49% | +0.06% | [13, 13] | [13, 13] | PASS |
| variance-2 | +3.49% | +2.07% | -1.75% | +0.07% | [13, 13] | [13, 13] | PASS |
| variance-3 | +10.47% | +7.86% | +58.47% | +0.02% | [12, 13] | [12, 13] | FAIL |
| variance-4 | -11.44% | -24.94% | -40.66% | +0.21% | [13, 13] | [13, 13] | PASS |
| variance-5 | +10.26% | +13.38% | +19.38% | -0.29% | [13, 13] | [13, 13] | FAIL |
| variance-6 | -8.27% | -8.43% | +14.05% | -0.44% | [13, 13] | [13, 13] | PASS |

Pair 3 varies from 12 to 13 qualified and fails p99 at +58.47%. All pairs pass input and original decision parity. Both harness and fixture hashes match across the five checked trees. Tracked add-on blob verification records any CRLF normalization.

[Source integrity](benchmarks/storage-budget-study-20261108/source-integrity.json) · [Raw archive](benchmarks/storage-budget-study-20261108/raw.tar.gz) · [SHA-256](benchmarks/storage-budget-study-20261108/raw.tar.gz.sha256)
