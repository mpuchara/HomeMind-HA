# U05b continuation (2026-11-02)

## Exact checkout and completed scope

Worktree: `C:\Users\mpuch\OneDrive\Dokumenty\HomeMind\HomeMind-HA-universal-232`.
Branch: `codex/universal-u05b-trainable-scorer`.
Last implementation commit: **a0173874e0e4b09a097c3f538042a723137407a9**.
A subsequent documentation-only commit may be HEAD; use `git log -3 --oneline`.

Base: current U05a HEAD **e32f0b1802a66333d797af0d0fa87a2012975501**, verified
against PR #356 before branching. Production reference remains **0.14.232**, commit
`f7384ba5476f26155ecb138034cc3350fa5bfda2`. Do not restart from main or 0.14.184.

Draft dependency chain: #355 compatibility -> #356 U05a ->
[**#357 U05b.1**](https://github.com/mpuchara/HomeMind-HA/pull/357).
No PR is merged, no version or installation release is created.

This run completes **U05b.1 Neural Training Core**, with an initial U05a adapter
and experimental checkpoint serialization. **U05b as a whole is not complete.**
Formal quality acceptance, operational data admission, validated checkpoint
publication/undo lifecycle and real Pi4/runtime acceptance remain open.

## Implemented code

`adaptive_ai/src/universal_scorer_training.py`:

* Actual shared scorer forward/backpropagation with per-device ragged legal-set
  cross entropy, trusted sample weights, clipped Adam, L2 and small mini-batches.
* Frozen U03 and detached scorer child, train-only normalization, deterministic
  seeded training and best-training-loss early stopping.
* Existing U05a TrainingDataset identity/versions/ids, chronological_split and
  U03/U04 features reused. Exact legal desired values only; no nearest rounding.
* Bounded retained features/action groups/examples, cooperative TRAINING_BUDGET,
  one fit per process and cancellation that discards the child. Deterministic
  restart from the same parent/dataset is supported; optimizer resume is not.
* Deterministic checksum-protected JSON with frozen encoder, weights, moments
  of input normalization (not optimizer state), provenance and learning metrics.
* `load_core_checkpoint` reproduces offline predictions and rejects corrupted,
  duplicate-key, incompatible and falsely verified artifacts.
* `core_artifact_status` diagnoses requires_rebuild on changed dataset/revision.
  It is not a transactional publication check or persisted revocation ledger.

`tools/universal_training_fixture.py`: explicitly synthetic complete U05a
receipts/replay/coverage, not a replacement production collector.
`tools/benchmark_universal_training.py`: host-only optimizer/preparation/inference
timing and memory benchmark. Small smoke added to existing CI.
`tests/test_universal_scorer_training.py`: 14 focused learning/calculus/safety
tests. Implementation details: `UNIVERSAL_U05B_NEURAL_CORE.md`.

## Validation receipts and locations

Focused native Windows Python 3.11 run: **14 tests in 3.418 seconds, OK**.
Binary motion fixture: future accuracy **50% -> 100%**, lower CE loss and changed
scorer weights; parent and frozen encoder checkpoints remain exactly equal.
Additional continuous and categorical legal-set learning tests pass.
Both layers' weight/bias gradients match finite differences to six decimal places.
Serialized/disk-reloaded scores and repeated training artifacts match exactly in
the tested numerical environment. Holdout label changes do not change learned
weights or normalization.

Full suite runs in WSL Ubuntu Python 3.12 / NumPy 2.3.3 from an exact native copy.
**681 source/test/tool files** matched via SHA256 with no differences. Source
manifest digest:
`1d2e2fd5350c6a1b572b064de2ff131e9acf4ef9cb4451bc67f0f0ca4e619114`.

Persistent validation directory:
`/home/mpuchara/homemind-universal-u05b/`.
Read `full-tests.log` for the completed full-suite result; the run was still
executing when this handoff snapshot was written. The immutable source receipt
is `source-parity.json`; `benchmark-final.json` and `benchmark-final.log` retain
the benchmark. The versioned copy is `benchmarks/UNIVERSAL_U05B1_HOST.json`.

Code CI: [run 41405753825](https://github.com/mpuchara/HomeMind-HA/actions/runs/41405753825).
Final documentation HEAD may have a later run on PR #357. Inspect the current
PR checks rather than substituting an older SHA's green status. Full-suite/CI
final outcomes are also recorded in the PR body after completion.

Host-only benchmark (24 epochs): 5/1 took .295 s, 50/10 .849 s, 200/30 3.267 s,
1000/200 20.533 s; all synthetic future accuracies reached 1.0. Largest process
peak RSS was 52.30 MiB, including earlier scenarios and fixture setup. Each epoch,
CPU/total/preparation/validation time, updates and full-offline-encoding prediction
costs are retained in the JSON. No Pi4, HA socket/dispatch/ACK, UI contention or
event-to-intent acceptance claim is made.

## Deliberate non-publication boundary

An optimizer run does not establish a verified model. Core artifacts retain
`trained=false`, `validation_status=not_validated`, `promotion_eligible=false`
and `control_eligible=false`, despite actual learned weights. Never flip these
flags manually or feed this experimental format into an active backend.
No RuntimeComposition/Engine/REST archive/Correct/Candidate/Shadow/DeviceAgent/
Executor/training-budget implementation is replaced. There are no new runtime
hooks, SQLite operations, service calls or automatic backend changes.

## Exact next steps

1. Confirm PR #357 current HEAD, full-suite tail and CI results. Preserve the
   base/dependency chain and run focused tests before extending the core.
2. **U05b.2 Dataset Integration/quality gate:** extend the initial adapter into
   an explicit acceptance contract for qualified offline datasets. Define and
   justify minimum independent support, neutral train-only baseline and paired
   future-period improvement/regression gates, including device/property slices.
   Do not tune weights/normalization/epochs against the final holdout. Add an
   unlabeled offline inference API so future callers do not fabricate a desired
   label merely to call the current training-sample feature adapter.
3. **U05b.3 Checkpoint Lifecycle:** add a distinct validated artifact version,
   fresh transactional evidence provider at publication, dataset/revision/undo
   rechecks and durable invalidation/rebuild state. Keep parent snapshots isolated.
   Add optimizer/RNG/cursor resume if needed; current restart redoes training.
   Current checksum is integrity checking, not a substitute for evidence validation.
4. **U05b.4 Performance/Acceptance:** benchmark repeated independent scenarios,
   measure actual RAM/CPU bounds, concurrency and cancellation under real runtime
   load; run existing HA/UI/Correct/Candidate/Shadow regression and Pi4 acceptance.
5. Production historical registry/action-mask/coverage collection remains absent
   from U05a. Do not manufacture it or substitute current HA state. Use complete
   offline inputs or explicitly synthetic fixtures until a durable collector exists.
6. Keep physical control/promotion disabled, preserve existing ActionIntent ->
   Executor safety and do not merge existing drafts or publish an install release.

Commands from this checkout:

```powershell
python -m unittest discover -s tests -p test_universal_scorer_training.py -v
python tools/benchmark_universal_training.py --output benchmark.json
```

For full Linux tests first refresh the native copy of the current source, then:

```sh
cd /home/mpuchara/homemind-universal-u05b/current
/home/mpuchara/homemind-universal-232-validation/venv/bin/python -m unittest discover -s tests -v
```
