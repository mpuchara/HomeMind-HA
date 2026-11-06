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

Resume in `C:\Users\mpuch\OneDrive\Dokumenty\HomeMind\HomeMind-HA-universal-232`
on `codex/universal-u05b4c1g-evidence-scalability`. Fetch origin and verify the
reporting HEAD/worktree and unchanged add-on tree first. Frozen ext4 study:
`/home/mpuchara/homemind-native-writer-study-20261106`; preliminary/rejected
probes and test scratch: `/home/mpuchara/homemind-writer-20261106`.
The writer profiling/empty-protection/preflight unit is complete. Next investigate
remaining per-receipt/native metadata transaction CPU with a frozen oracle and
proof of transaction-local invalidation; do not repeat snapshot optimization or
change cadence. Retain every registered failure and rerun the matrix after an
actual next production improvement. Candidate Recovery, production composition,
actual-data migration, inherited retention miss, four-hour soak and Pi/HA remain
open; no release/deployment is authorized by these results.

## Earlier checkpoint (historical)

# Current continuation — Gate D snapshot implementation, 2026-11-06

**BLOCKER.** Resume Draft PR #369 on
`codex/universal-u05b4c1g-evidence-scalability` in this worktree.
Final code `eb6650e2ba90a9e53470178386e623dd73bd4501`, add-on tree
`3fc56e32a4001fd2c2cc7a36716d4c18f0944b67`. Measured performance source
`cf09e214b4a97f367e8a7ca9169b2f30273b2c63`; baseline exact #368
`951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`. Reporting HEAD is recorded in
PR #369. Verify clean state/remote refs and compare production against `eb6650e2`;
for docs/tools-only reporting changes, do not repeat the earlier PR inventory.
Version stays 0.14.232. Do not merge, publish, create a ZIP or deploy.

Completed code: `51b49b91` implements bounded revision/schema/trigger-validated
snapshot reuse with isolated savepoint evaluations and independent writer schema
verification. `cf09e214` bounds decoded configs. `57575bda`/`eb6650e2` complete
stop-before-start cleanup and cache-free adapter compatibility. Final local tests:
295 Universal PASS (182.977s), 36 focused PASS (35.017s). The intermediate
295-test run with one adapter error is retained. Measured-source CI `42235276985`
passed; final-code CI `42237574777` was in progress at the saved receipt.
The full timing matrix has not been repeated after the lifecycle-only fixes.

Preparation CPU/turn fell 16.75 → 9.92 ms with one durable materialization across
unchanged revisions. Full RAM snapshots remain fresh. Gate D stays BLOCKER:
Normal 4 (+16.75% CPU), unsupported 400, 50/10 and 200/30 breach margins.
Five other Normal pairs, both Busy cases, OFF and incomplete pass. All 26 arms
preserve inputs/original signatures/native persistence with zero HA calls or
collector errors/drops. Throughput clears 2x: qualified 43 → 102, consumed
46 → 106 in the primary workloads. Six same-revision Normal pairs preserve
signatures; one breaches tails. None of the registered failures is waived.

Read [the current root-cause report](FINAL_RELEASE_GATE_D_ROOT_CAUSE.md) for the
complete matrix, profiles, allocation windows, rejected attempts and next step.
Environment: WSL Ubuntu/ext4, `~/homemind-perf-venv`, root
`~/homemind-gated-20261106`. `source` is immutable measured `cf09e214`, `base`
is exact #368 with the common corrected fixture/harness, and `before` is the
reporting predecessor. `source-51b49b91` is retained. `verification-final` is a
mutable test copy matching final production code. `profiles`, `matrix`, `variance`
and `supplement` retain commands, outputs and source/harness manifests. Never
replace old results. The committed raw archive contains 219 files plus an adjacent
SHA-256; its summary/CI receipt are linked from the report.

First unfinished implementation unit: optimize proven native collector writer
cost inside one independently verified native transaction, then freeze source
and rerun the full original registered protocol. Supplemental 50/10 writer CPU
80.77 → 155.45 ms accounts for 74.69 of 95.59 extra process CPU ms; preparation
is already lower than #368. Profile protection/setup, revision/schema reads,
per-decision validation and serialization before selecting one safe operation.
Keep independent revision/schema and all per-decision/causal/retention guards.
Reset any transaction-local reuse on own writes/DDL, commit, rollback, snapshot
boundary and exceptions. Do not change cadence, workload, evidence or training.
The full-copy RAM prototype was rejected (only ~4.1% mean component improvement);
cross-turn registry reuse is not proven safe. Candidate Recovery and every later
release gate remain pending.

## Earlier continuation (historical)

# Final release continuation — 2026-11-06

Overall **BLOCKER**. No deployment, main merge, release/tag or installable package.
Do not turn a short component run or generated fixture into production acceptance.

## Exact resume identity

- Worktree: `C:\Users\mpuch\OneDrive\Dokumenty\HomeMind\HomeMind-HA-universal-232`.
- Branch: `codex/universal-u05b4c1g-evidence-scalability`; Draft PR #369 → #368.
- Last tested code/harness HEAD: `84563536831689588f90824887ddd2e936f31659`.
- Active add-on runtime commit: `f858414cd9ea494738211f2843ded44c5b770991`.
- Add-on tree: `70af83dba9e631f9cc5002b20906bf372d561867`.
- Performance baseline: exact #368 `951dcd70ed20b5f74220307cfb7411fa2c6bcc5d`.
- Stable/main audited source: `91c7343324d38a1922bfed6486b4ef6dd7a3ba06` (0.14.184).

The reporting commit after this code HEAD changes documentation/evidence only.
Its exact HEAD is recorded in the PR body and final session response. On resume,
fetch origin, compare local HEAD with #369's `head_sha`, require a clean worktree,
and check `git diff 84563536 HEAD -- adaptive_ai tools tests .github` is empty.
Also verify `git rev-parse HEAD:adaptive_ai` equals the tree above. If these hold,
continue below without repeating the full PR inventory audit. Investigate only
actual intervening code/ref changes if they differ.

## Completed units

1. Audited all 68 open PRs, exact ancestry, current refs and CI checks. #347 is
   the only open head outside #369 ancestry. #355 already integrates #349–354.
   Main's publishing workflow automatically releases after successful main CI;
   keep main unchanged until every required gate closes.
2. Traced original descriptor state reads and added sibling/remap/exclusion,
   ownership and concurrent-revision regressions. Full snapshots remain intact.
3. Reduced detached descriptor evaluation 3→1 per turn. Initial `e2669cc0` created
   a service/closure/bound-method cycle; the deterministic GC-disabled test fails
   there. `f858414c` removes the override in `finally`; seven ownership/scope tests pass.
4. Preserved six same-revision pairs, the stopped initial candidate study, a full
   13-pair corrected matrix and separate profiles. Same-revision and corrected
   series exposed nondeterministic root input order, not a proven policy change.
5. `84563536` makes fixture target order explicit and fingerprints workload inputs.
   Reversed Store enumeration fails before the fix and passes after it. Seven
   fresh paired confirmation cases all match input hashes and original signatures.
   Both Normal orders still exceed CPU (+39.1% and +27.0%); forward also exceeds
   p95/p99. Gate D stays BLOCKER. No acceptance threshold or workload was weakened.
6. Four bounded Candidate queue recovery tests pass; they use a controlled backend,
   not full Candidate/training/WS/UI acceptance.
7. `verify_release_upgrade.py` passes a generated 0.14.184→current copy test:
   six models, 16 protected tables, options, modes, lineage, Correct/Undo, two
   restarts and full old-data/app restore. Actual existing-installation data is NOT RUN.
8. Added readiness report, audit matrix, update/rollback procedure and raw evidence.

## Evidence and environment

- WSL Ubuntu/ext4; `source ~/homemind-perf-venv/bin/activate` (Python 3.12.3).
- Root: `~/homemind-final-release-20261106`.
- `rejected`: immutable archive of `e2669cc0`; initial protocol/raw files are at root.
  Initial driver was interrupted during `normal-3-right` after proving the ownership
  defect. Unfinished arms are NOT RUN; retain the partial/unpaired output.
- `corrected-source` / `corrected-5ce4a78e`: immutable runtime `f858414c`, harness
  `5ce4a78e`, complete 13-pair matrix and profiles. Three pairs have mismatched
  root input order; do not count them as valid comparisons.
- `ordered-source` / `ordered-base` / `ordered-84563536`: exact common corrected
  fixture/harness on current and #368, seven confirmation pairs with input hashes.
  These are a negative screen/fixture repair confirmation, not a replacement for
  the registered complete acceptance protocol. Never mutate these source snapshots.
- `previous`: exact stable 0.14.184 source; `upgrade184-1/-2/-3`: failed attempts
  and passing fixture. Attempt 2 exposed cursor backfill already performed by old
  code; the passing baseline is explicitly old-code-restarted, without excluding
  protected columns from comparison.
- `candidate` is a mutable local test scratch tree, **not** a performance source identity.
- Committed `docs/benchmarks/FINAL_RELEASE_CHECKPOINT_RAW.json.gz` contains protocols,
  manifests, logs, raw JSONL and reproduction/analysis drivers; a SHA-256 is adjacent.
  `FINAL_RELEASE_PERFORMANCE_SUMMARY.json` separates all series. No actual user DB
  or token is included.

Known local successful checks: snapshot 7; predecision 15; collector 44;
Candidate queue recovery 4; fixture order 1.
The later complete Universal suite passed 295 tests in 164.730s after the fixture
order fix; its cases overlap the predecision/collector focused counts.
Full CI `42200241646` passed 3,150
tests each on 3.11/3.13, required benchmarks and image for `ed53f3e6` (same add-on
tree). Read the saved CI receipt for later exact fixture/reporting SHAs; do not
assume an ancestor's successful run certifies a later commit.

## First unfinished task: Gate D

Read `FINAL_RELEASE_PERFORMANCE.md` and the ordered protocol first. The next
implementation must address measured preparation CPU, not another arbitrary
cadence adjustment. The separate profiles retained full RAM/native materialization
but only one descriptor; preparation-thread CPU was 553.2→510.5ms over 31 turns.
Profile the stable input-order harness before attributing aggregate CPU changes.

Use fresh ext4 source snapshots and new output names. Keep exact #368 and an
identical harness in both arms. Preserve the original 24×400ms/fanout-16 workload,
256-config bound, intent counts/values/signatures, affected-sensor/revision/clock
contracts, zero HA calls and collector accounting. Require matching input
fingerprints before assessing performance. Keep profiling separate and serialize
all timing runs. Do not co-run unit tests, build work or other benchmarks locally.

Explore only proven redundant work in full RAM/native snapshot preparation.
The five descriptor-scope cases are an oracle, **not** proof that a reduced state
snapshot is universally equivalent. Preserve full snapshots until differential
tests cover every arbiter input, explicit topology/mapping exclusion/inclusion,
same-device peers, registry/state changes and ownership. Any snapshot reuse must
still establish fresh trustworthy SQLite revision/DDL/trigger and RAM binding.

After one safe candidate, run the registered alternating Normal comparison and
all Busy/OFF/incomplete/unsupported/smaller controls. Preserve every result. If
mixed, run the additional reversed-order pairs and fixed-input same-revision
variance series; do not amend margins after seeing results. Both ordered Normal
CPU breaches are current negative evidence, not just a suspected host artifact.

## Remaining required gates

1. Candidate Recovery: real parallel Shadow/Candidate and training, explicit bounded
   overload, drain/fresh wave with preparation ON/OFF, actual interruption/restart,
   discard/new-generation and SQLite/storage observability. TrainingQueue is
   deliberately in RAM; durable Candidate recovery and pending general training
   are different contracts. Preserve honest drops/coalescing and effects on learning.
2. Production Composition: use the shipped `trial_queue_main` root, real scheduler,
   controlled HA WebSocket and loopback HTTP. Exercise `/api/agents`,
   `/api/candidates`, `/api/agents/{id}/teach-rl-status`, final Correct/history
   routes, Correct/Undo and maintenance under load. Record transport-boundary
   service attempts and require no unauthorized actions. Existing ingress/component
   runners do not cover the complete transport/HTTP lifecycle.
3. Existing-installation migration: locate an authorized consistent data backup and
   operate on copies only. Generated fixture PASS is not actual-data acceptance.
   Cover pending/interrupted jobs, all model formats, full composition, storage and
   retention/WAL bounds. Do not require Rebuild or reset user agents to obtain PASS.
4. Preserve/reproduce the inherited intermittent retention/rollback receipt miss.
   The isolated old successes did not resolve its cause.
5. Run >=14,400s only after freezing the candidate and composing all required
   activities. `benchmark_qualified_soak.py` is a **component** runner: synthetic
   collector reconciliation is not real fake-WS transport or full HTTP composition.
   Four hours of that runner alone does not automatically close this release's soak.
6. Approve/integrate only tested code, resolve the #347 sibling intentionally,
   prevent premature auto-publishing, select a new release version after those
   decisions, then full final-SHA CI/image and exact committed ZIP/source/manifest
   checks. Publish only after all mandatory gates pass. Physical Pi/real HA stays
   separately NOT RUN until actually tested; no device deployment is authorized.

Update these reports and commit each completed unit. The task remains incomplete
until the user's release criteria are met. No work is promised outside an active session.
