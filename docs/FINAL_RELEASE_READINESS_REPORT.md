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

# Current readiness — reusable arbiter snapshot, 2026-11-06

**Overall BLOCKER; Gate D remains BLOCKER.** PR #369 remains Draft.
No merge, release, ZIP, version change or deployment was performed.

Final code `eb6650e2ba90a9e53470178386e623dd73bd4501`, add-on tree
`3fc56e32a4001fd2c2cc7a36716d4c18f0944b67`, version **0.14.232** unchanged.
Performance source `cf09e214b4a97f367e8a7ca9169b2f30273b2c63`.
Later code adds stop-before-thread-start cleanup and optional lifecycle support
for cache-free adapters. The timing matrix was not repeated after these lifecycle
fixes. Reporting HEAD is recorded in PR #369; compare production against
`eb6650e2` before resuming a docs/tools-only reporting checkpoint.

Implemented one bounded revision/schema/trigger-validated durable snapshot,
fresh full RAM each turn, original arbitration inside a rollback savepoint,
and independent native writer revision/schema verification. Final focused
validation passes 36 tests. The intermediate 295-test lifecycle run had one
adapter error and is retained; final full regression and CI status are recorded
in the current root-cause report.

All 26 acceptance arms completed. Nine of 13 pairs clear every margin; Normal 4,
unsupported 400, 50/10 and 200/30 fail. Every fingerprint, original decision count
and signature matches. Native persistence and collector accounting pass, with
zero unauthorized HA calls or collector errors/drops. Throughput clears 2x.
The separate same-revision series has one tail failure and waives no limit.

See the [current root-cause report](FINAL_RELEASE_GATE_D_ROOT_CAUSE.md) and
[performance report](FINAL_RELEASE_PERFORMANCE.md) for profiles, raw results,
rejected experiments, limitations and the next writer optimization unit.
Candidate Recovery, Production Composition, actual-data migration, inherited
retention regression and four-hour soak remain open. Pi/live HA is NOT RUN.
Collector remains disabled by default.

Final code `eb6650e2` local results: 295 Universal PASS (182.977s), 36 focused
PASS (35.017s). Measured-source CI `42235276985` passed; final-code CI
`42237574777` remains in progress at the saved receipt.

## Prior readiness checkpoint (historical)

# Final release readiness — development checkpoint, 2026-11-06

**Overall: BLOCKER. The requested final release is not complete and must not be
published or recommended for installation.** No HA/device deployment, stable
release, tag, main merge, agent reset or user-data operation was performed.

## Version, source and integration

- Development version: **0.14.232**, unchanged. No new release number assigned.
- Last code/harness checkpoint: `84563536831689588f90824887ddd2e936f31659`.
- Active production runtime change: `f858414cd9ea494738211f2843ded44c5b770991`.
- Add-on Git tree: `70af83dba9e631f9cc5002b20906bf372d561867`.
- Exact performance baseline #368: `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.
- Branch: `codex/universal-u05b4c1g-evidence-scalability`, Draft
  [PR #369](https://github.com/mpuchara/HomeMind-HA/pull/369), based on #368.
- Main remains the audited 0.14.184 source `91c7343324d38a1922bfed6486b4ef6dd7a3ba06`;
  published prerelease 0.14.231-rc.1 is a separate sibling branch.

No PR was merged in this session. The development ancestry already contains
#302–346 and #348–369, including #349–354 via the #355 compatibility integration.
#347 is not an ancestor: its ingress-copy and capture-bookkeeping optimizations
need a separate integration decision. All 68 saved open heads, exact refs,
ancestry and individual CI check attempts are in the audit receipts. Open drafts
are not automatically approved release inputs.

## Changes and performance outcome

The original detached predecision preparation evaluated an identical target
descriptor three times per turn. The current implementation reuses it only on
that private service, then removes the override in `finally`. The full RAM and
SQLite snapshot, ownership, mappings, revision guards, clocks, original arbiter,
default-off collector and physical dispatch boundaries remain in place.
An initial implementation retained the snapshot through a reference cycle;
the failing ownership regression and rejected experiment were preserved, and
the cycle was fixed before the retained checkpoint.

Separate profiles observed 93→31 descriptor evaluations for 31 turns and
553.2→510.5ms preparation-thread CPU including finalization. This does **not**
establish an end-to-end speedup or Gate D PASS.

The complete 13-pair corrected matrix exposed three mismatched target orders.
The diagnostic now retains original decision rows. A fixture-only repair fixes
root order to the declared target list and exports an input fingerprint. Its
reversed-enumeration regression fails before the repair and passes after it.

Seven fresh post-repair pairs preserve identical input fingerprints and original
384-intent signatures in both arms. There are no HA service calls or unexplained
collector losses. Busy and control screens passed in those pairs, but Normal
CPU still increased **39.1% and 27.0%** versus the registered **15%** limit.
The forward Normal pair also exceeded p95/p99. **Gate D is BLOCKER.**
See [performance evidence](FINAL_RELEASE_PERFORMANCE.md), including failed,
unpaired, interrupted and input-mismatched results.

## Tests and migration

Local WSL Python 3.12.3 checks passed: 7 snapshot/ownership regressions,
15 existing predecision tests, 44 collector tests, 4 new bounded Candidate
recovery tests and 1 fixture-order regression. Initial failed test attempts and
their corrections are retained rather than counted as successful runs.
After the fixture-order repair, the complete 295-test Universal suite also
passed on WSL in 164.730s. This overlaps the focused predecision/collector tests;
these counts must not be added as if every test were distinct.

Full checkpoint CI on `ed53f3e6c014bdeb4dd0557ad8a5a2314b0731db` passed
**3,150 tests on Python 3.11 and 3,150 on 3.13**, required benchmarks and image
build/smoke: [run 42200241646](https://github.com/mpuchara/HomeMind-HA/actions/runs/42200241646).
That commit has the same add-on runtime tree as this checkpoint. The later
`84563536` fixture/harness changes have their own CI, still running at the saved
snapshot; do not transfer a green result to an untested SHA. Exact snapshots
are recorded in `benchmarks/FINAL_RELEASE_CHECKPOINT_CI.json`.

The copied previous-code fixture passed two current restarts, exact comparison
of all existing rows/columns in 16 protected tables, six persisted model
checksums, modes, Candidate generations, Correct/Undo and unchanged options.
It also passed restoration of old code with the entire old data copy. No fixture
agent required Rebuild. This verifies selected Store/schema/model guards, not
complete startup on an actual installation. See
[migration scope and preserved failures](FINAL_RELEASE_MIGRATION.md).

## Release gates

| Gate | Status | Evidence / remaining work |
| --- | --- | --- |
| A · Integration audit | PASS (source/inventory scope) | 68 PR heads and dependencies audited; approval/integration decisions still required |
| B · Gate D Performance | BLOCKER | Input-matched Normal CPU exceeds +15% in both new orders; registered full acceptance still not met |
| C · Candidate Recovery | BLOCKER | Four queue components PASS; real concurrent training/inference, interruption, full discard/restart and refreshed UI acceptance NOT RUN |
| D · Production Composition | NOT RUN | Exact installed scheduler + controlled WS + local HTTP + Correct/Undo/storage load measurement pending |
| E · Existing-installation migration | BLOCKER | Generated previous-code fixture PASS; actual copied installation, full runtime startup, interrupted jobs and storage fault matrix NOT RUN |
| F · Four-hour soak | NOT RUN | No >=14,400s acceptance run; short component tests are not a full soak |
| G · Approved integration/release | BLOCKER | No merges; #347 sibling unresolved; main workflow auto-publishes after green CI and must not be triggered prematurely |
| Final release CI | NOT RUN | No final approved release SHA exists; green checkpoint CI is recorded separately |
| Final image/packages/ZIP/SHA-256 | NOT RUN | No final install package built or release published while gates remain open |
| Pi / real HA acceptance | NOT RUN | All new tests use disposable host data; physical acceptance is separate |

The inherited intermittent retention/rollback receipt miss remains unresolved.
The existing 256-durable-config evidence bound remains enforced; a 400-config
control is unsupported and must not be described as a qualified scalability PASS.
No long-term RSS/thread/queue/WAL trend, physical response or real Recorder
performance conclusion is supported by this checkpoint.

## Update, restore and continuation

The [update and rollback procedure](FINAL_RELEASE_UPGRADE_AND_ROLLBACK.md)
requires a consistent full data/application/options backup, a test of a **copy**,
the same add-on/data identity, checksum/source verification and post-restart
agent/model/history comparison. Ordinary update must not be replaced with
Rebuild/reset. Application-only downgrade is not assumed safe; retain the
complete-data restoration procedure.

There is **no published installation-package link** for this task. The raw host
evidence archive and its SHA-256 are development evidence, not an install package.
The unfinished work and exact resume checks are in
[FINAL_RELEASE_HANDOFF.md](FINAL_RELEASE_HANDOFF.md). This checkpoint can be
continued with “Kontynuuj”; it is not completion of the final-release objective.
