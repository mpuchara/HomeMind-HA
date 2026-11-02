# U05b continuation (2026-11-03)

## Exact checkout and scope

Worktree: `C:\Users\mpuch\OneDrive\Dokumenty\HomeMind\HomeMind-HA-universal-232`.
Branch: `codex/universal-u05b2-quality-gate`.
Last implementation commit: **cfe673b97134b89390868973ed03e6d7f672a78c**.
A documentation-only commit may follow; inspect `git log -3 --oneline` for HEAD.

Base is U05b.1 PR #357 at **8d869fcffd8c5440546c8b82806ae163fcacd076**,
verified live before branching. Production reference remains **0.14.232**,
`f7384ba5476f26155ecb138034cc3350fa5bfda2`. Do not start from main/0.14.184.

Draft dependency chain: #355 compatibility -> #356 U05a -> #357 U05b.1 ->
[**#358 U05b.2**](https://github.com/mpuchara/HomeMind-HA/pull/358).
No PR was merged, version changed or installation release produced.

Completed: **U05b.1 optimization core and U05b.2 offline data/quality gate**.
**U05b as a whole remains incomplete.** Validated artifact publication,
transactional freshness/undo lifecycle, production collection and real Pi4/
HA/runtime acceptance remain open.

## Implemented U05b.2 changes

* `universal_scorer_training.py`: unlabeled `OfflineActionContext`, shared
  context feature path, raw offline scoring/abstention and stricter typed legal
  action/descriptor/resource/clock/immutable context checks. Valid existing
  feature tensors and checkpoint format remain compatible; training still
  admits trusted exact labels separately.
* `universal_scorer_validation.py`: exact U05a dataset/revision and used manifest
  checks, train-only normalization verification and fresh weight-based metrics.
  Baseline template/selection uses training data only. Whole episodes and
  overlapping time-window intervals form independent-evidence units; duplicate
  points do not multiply statistical support. Defaults derive minimum support
  from a declared Hoeffding precision/error budget; device/property families
  need their own future coverage. The existing exact paired sign test checks
  improvement. Global gains cannot hide observed device/property or action-class
  regressions, including more false activations.
* `tools/universal_training_fixture.py`: explicit synthetic timestamp-spacing
  option; default fixture behavior remains compatible.
* `tools/benchmark_universal_validation.py`: four host scale scenarios with
  passing/insufficient-evidence receipts. A small quality gate smoke was added
  to CI, preserving all prior checks.
* `tests/test_universal_scorer_validation.py`: 21 new cases.

Detailed contract, defaults, baseline rules, independence assumptions and limits:
`UNIVERSAL_U05B2_QUALITY_GATE.md`. Core math remains documented in
`UNIVERSAL_U05B_NEURAL_CORE.md`; U05a source audit and collection limits still apply.

## Validation receipts

35 focused core/gate tests: **19.444 seconds, OK**, native Windows Python 3.11.
Includes the existing 14 optimizer tests and 21 new gate/inference tests.
One-device synthetic default-policy case has 43 future evidence units, accuracy
1.0 versus baseline .48837, 22 winning units, zero losing units and exact p-value
2.384185791015625e-7. These are deterministic constructed evidence, not physical
home-control or Raspberry Pi quality claims.

Full local regression runs in WSL Ubuntu Python 3.12 / NumPy 2.3.3 from an exact
native source copy: **684 source/test/tool files**, zero SHA256 mismatches.
Source manifest digest:
`5e8375afdffa878eba9a2111f929f709f41068d0c738612b768ccab57a4863db`.

Persistent validation directory:
`/home/mpuchara/homemind-universal-u05b2/`.
Read `full-tests.log` for the final complete-suite result. The run was executing
at documentation capture; final full-suite/CI results are updated in PR #358's
body. `source-parity.json` records exact source identity; `benchmark.json` and
`benchmark.log` retain the host run. The versioned benchmark is
`benchmarks/UNIVERSAL_U05B2_HOST.json`.

Code CI: [run 41547889325](https://github.com/mpuchara/HomeMind-HA/actions/runs/41547889325).
A later documentation HEAD has its own PR check run. Resolve the exact current
HEAD checks rather than assuming an older SHA's green status applies.

Host validation times for 5/1, 50/10, 200/30, 1000/200 sensors/devices were
.037/.591/2.701/14.780 seconds. The one-device cohort passes the offline gate.
All larger cohorts intentionally return `insufficient_evidence` because their
future evidence per device is too small; required units per slice are 48/57/72.
They are not converted into acceptance by relaxing defaults. Values include
bounded feature auditing/cooperative yields and are not event-to-intent or
HA/UI/Pi4 acceptance measurements.

Predecessor U05b.1 full local suite passed 2944 tests; its final HEAD CI passed
Python 3.11/3.13, image and runtime benchmarks. See PR #357 for those receipts.

## Non-publication boundary and interpretation

An offline report may say `passed`, but report and checkpoint still retain
`trained=false`, `promotion_eligible=false`, `control_eligible=false`.
No producer, service call, runtime/HTTP hook, Candidate update, migration or
active backend change is installed. Engine, RuntimeComposition, Correct,
Candidate/Shadow, DeviceAgent, Executor, training budget and version remain intact.

Independence and no holdout peeking are qualified-input assumptions. Distinct
ids/60 seconds cannot prove independence. Checksum integrity is not producer
authentication or proof of actual training provenance. The report covers the
pinned manifest only, not every agent in an installation; it is not a new
promotion manager. Comparing installed Ridge/TinyMLP remains later lifecycle work.

The current U05a 2048-row cap cannot supply 72 future units to each of 200
tracked devices in one dataset. Larger-install validation needs a separate
bounded/chunked aggregation contract, not lower thresholds or omitted devices.

## Exact continuation order

1. Read current PR #358 HEAD, local full-suite tail and live CI checks; keep the
   stacked branch chain and 0.14.232 reference. Run focused tests after changes.
2. **U05b.3 Checkpoint Lifecycle**: introduce a distinct validated artifact
   contract/factory. Pin model/encoder/dataset/supervision/policy/report identities
   and recompute/check current evidence inside an atomic publication protocol.
   A passing mutable JSON report alone must never set trained/promotable flags.
3. Add a trusted fresh-evidence provider using native journals and current undo
   state, persisted artifact invalidation and safe isolated child rebuild. Never
   subtract an undone correction from weights or mutate the active parent.
4. Bind validation policy/model selection before evaluation and track holdout
   reuse/attempts; repeated tuning against the same holdout is not independent
   confirmation. Add optimizer/RNG/cursor resume if required; current cancellation
   discards the child and restart redoes deterministic training from the parent.
5. **U05b.4 Performance/Acceptance**: repeated host/resource/load runs and real
   Pi4/HA/UI/event-to-intent/dispatch-ACK/soak gates. Metadata-based early
   insufficiency admission could avoid unnecessary dense feature audits; measure
   first. Keep broad regression/affected-agent validation in the established
   Candidate/Shadow lifecycle rather than inventing another manager.
6. Production historical registry snapshots, legal masks and completeness
   collection still do not exist. Do not manufacture them or use current HA
   metadata as historical evidence. Complete offline/synthetic inputs only.
7. Keep physical control and promotion disabled; no merges, release/version bump
   or install package without completing the relevant gates.

Commands from the checkout:

```powershell
python -m unittest discover -s tests -p 'test_universal_scorer*.py' -v
python tools/benchmark_universal_validation.py --output validation-benchmark.json
```

For full Linux tests refresh the native copy first, then:

```sh
cd /home/mpuchara/homemind-universal-u05b2/current
/home/mpuchara/homemind-universal-232-validation/venv/bin/python -m unittest discover -s tests -v
```
