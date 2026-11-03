# U05b.4b.3 handoff

## Baseline and scope

Branch `codex/universal-u05b4b3-granular-retention` starts at verified Draft
PR #362 HEAD `4755830a0244c6ae33e115e27ce321600806c341`, not main.
Baseline [CI 41655642962](https://github.com/mpuchara/HomeMind-HA/actions/runs/41655642962)
was successful before work. The initial worktree was clean. Reference remains
0.14.232 / `f7384ba5476f26155ecb138034cc3350fa5bfda2`.
The new Draft PR must target `codex/universal-u05b4b2-affected-sensors`, preserving
the #355–362 dependency chain. No merges, package/version change, installation,
automatic neural training, Candidate/Executor/Correct UI or Control activation.

Implementation commit and final CI links will be recorded in the measurement
follow-up. This initial implementation record is **validation in progress**,
not stage acceptance.

## Implemented API

Read `UNIVERSAL_U05B4B3_GRANULAR_RETENTION.md` for the schema, trust boundary,
resource/precision budgets and migration semantics. New collected segments are
V3; V1/V2 retain their contracts. Native retention triggers track actual
deletion/mutation and backdated insert atomically, across restarts, disabled
collection and external SQLite connections. Prune buffers are capped at 512.
No new runtime/WS/Engine queries or additional sensor history are introduced.

`ReplayCoverage` separates full historical topology/universe from required
sensor history. `source_from_collection` creates a scoped witness only from
newly collected V3 evidence. U05a verifies tracker, revisions, source/time scope,
native original seed keys/clocks, immutable topology/universe and damage.
Lifecycle publication/guarded load check the witness and supervision revision
in the existing writer transaction. Correct Undo retains its original owner.

Initial migration initializes the live row count once and advances the legacy
global epoch. Missing/altered trigger repair rotates tracker identity; no old
source is repaired into authority. Tables store no sensor values. One envelope
per entity (10,000 maximum) is deliberately conservative on disjoint changes;
overflow advances a global rejection floor. Large prune batches share one
transaction and can still hold the writer for a long operation.

## Validation status

During implementation: 21 native retention tests and 7 collected-V3 lifecycle
tests passed under WSL Python 3.12.3. Additional external REPLACE/resource-budget
and training-concurrency cases are being verified. The full local suite and
required Python 3.11/3.13 CI are pending at this initial record.

Native integration covers original HA-event-style clocks, real predecision and
affected declarations, native collector/source/U05a, unchanged universe hash
after unrelated seed deletion, original seed loss/reinsertion, mutation, age,
per-entity/global protected hard caps, disabled/restart/second process, ROLLBACK,
process exit before/after COMMIT, missing/corrupt metadata and legacy semantics.
The lifecycle fixture collects a fresh chronological synthetic stream of 128
explicit V1 historical decision receipts into real V3 coverage; it does not
upgrade earlier coverage or claim physical HA acceptance.

## Benchmarks in progress

The four requested scales (5/1, 50/10, 200/30, 1000/200) have initial component
results: V3 retained 1/10/30/200 samples after unrelated sensor retention; exact
PR #362 retained zero. Both rejected after required sensor deletion. Initial
timings are mixed and include host contention; no regression-free/Pi claim.
The final comparison must use final trigger definitions consistently: external
REPLACE hardening was added while development runs were still active.

`tools/benchmark_universal_retention.py --source-root ROOT --output FILE` runs
the same native component fixture against each revision. Store full component
A/A, adverse results and composed-runtime A/A JSON under `docs/benchmarks`.
`benchmark_universal_affected_impact.py` accepts `--baseline-label`; use exact
PR #362 and preserve missing-declaration/dropped-history results at high load.

## Start U05b.4b.4

First verify this branch's final HEAD, Draft base and full CI; do not start from
main or assume this in-progress record is acceptance. U05b.4b.4 owns the complete
validated-artifact end-to-end path. Reuse NativeDatasetSource, U05a,
CheckpointLifecycle and these V3 native guards; retain original masks,
affected declarations, independent holdout and Undo provenance. Do not introduce
current-HA reconstruction, relaxed lux/power eligibility, automatic training,
Candidate integration or Control activation. Pi/real HA acceptance remains
separate from host results.
