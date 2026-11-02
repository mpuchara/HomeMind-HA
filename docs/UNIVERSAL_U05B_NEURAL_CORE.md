# U05b.1: actual shared neural ranking optimization

This PR completes **U05b.1 Neural Training Core**, with a working U05a adapter
and experimental serialization. It does **not** complete the production data
admission/quality contract of U05b.2, publication lifecycle of U05b.3, or physical
performance/acceptance work of U05b.4. No model becomes control eligible.

Base: U05a `e32f0b1802a66333d797af0d0fa87a2012975501`, production reference
0.14.232 `f7384ba5476f26155ecb138034cc3350fa5bfda2`. The U05a and compatibility
PRs remain drafts. No release/version bump or merge into main.

## Network and optimization

`universal_scorer_training.train_scorer` takes the existing `TrainingDataset`,
existing `SharedSetEncoder` and `UniversalActionScorer`. Both networks are cloned
from their checkpoints before any optimization; the supplied parent and encoder
remain unchanged. U03 is frozen throughout. Inputs reuse U03 home/room embeddings
and the existing U04 action features: domain, property, categorical semantics,
kind, current value, range, relative action and missingness. Parameter count
does not depend on the number of sensors, devices or actions. Entity ids have
no dedicated trained parameters; local preference adapters remain U05c work.

For a decision with legal actions A, label y, scores z and trusted weight w:

`L = sum_decisions(w * (logsumexp(z_A) - z_y)) / sum_decisions(w)`.

The softmax denominator contains only that decision's legal proposals. More
actions do not increase the decision's supervision weight. Binary, continuous
and categorical values use their existing descriptor/range semantics. A desired
value must occur exactly once in its historical ActionSet; no nearest-value
substitution or implicit action generation is performed. Single-action examples
are rejected because they provide no ranking gradient in this first core.

The implementation includes an actual two-layer ReLU forward pass, analytic
backpropagation, L2 on weights, global gradient clipping, mini-batches of complete
decision groups and Adam updates (beta1=.9, beta2=.999, epsilon=1e-8). Moments
and derivatives use float64; stored weights round to float32 after every update.
Finite-difference tests cover both layers' weights and biases.

Training-only moments normalize features, with a .001 scale floor and clipping
to +/-6. Every decision contributes equal normalization mass despite variable
action counts. Source weights apply to the loss. A seeded NumPy generator
determines group order. Early stopping watches **training loss only**, restores
the best epoch and never selects weights using the future evaluation period.
The holdout is measured before and after optimization for diagnostic evidence.

Defaults (80 epochs, 8 decisions/batch, Adam .01, L2 .0001, clip 5, patience 8,
min_delta .000001) are explicit bounded numerical-core settings, **not** quality
or release thresholds. Two train and two future-holdout decisions are merely a
minimum for exercising the optimizer. Statistical sample sufficiency, comparison
against a neutral baseline and per-device regression acceptance remain U05b.2.

## U05a contracts and resource boundaries

The adapter verifies the existing content-derived dataset identity, versions,
unique supervision/sample ids, trusted source, historical source/registry ids,
target/mask consistency and exact desired action. It reuses `chronological_split`
for future-only evaluation and episode exclusion. It does not duplicate causal
replay or fill missing metadata with live HA. Empty/insufficient evidence returns
`insufficient_evidence` with no checkpoint. Missing production registry/mask/
coverage collection is still a hard operational limitation of U05a.

Core bounds: 2048 examples, 250000 token references, 10000 tokens per sample,
2-128 legal actions per decision, 32 MiB of retained raw float32 feature matrices,
at most 32 decisions per optimizer batch and 4 MiB serialized checkpoints.
Limits fail explicitly. Normalized float64 matrices, temporary tensors and Python
objects consume additional memory; 32 MiB is not a total-process RSS guarantee.

Admission allows one explicit fit **per process**. Production cross-process job
admission is not installed. Preparation, hashing, mini-batches and metric loops
yield through the existing `TRAINING_BUDGET`. Caller cancellation discards the
child and reports the number of discarded updates. Restart retrains from the
same unchanged parent, dataset and seed; optimizer-state resume is not provided.
No new worker, HTTP route, SQLite query or runtime startup import is installed.

## Experimental checkpoints, not publication

`checkpoint_bytes` emits deterministic JSON with architecture/data versions,
weights, normalization, full frozen encoder and encoder id, parent id, dataset
id, supervision revision, exact used supervision/sample/source/generation/episode
ids, training settings, updates, best epoch, losses and before/after metrics.
Timing is excluded from the checksum so repeated training produces identical
artifact bytes in the same numerical environment. Cross-platform bitwise
identity is not claimed. JSON load rejects duplicate keys, invalid checksum,
nonfinite/incompatible tensors, malformed receipts and false verification flags.
Pickle is never used. Disk round-trip tests reproduce predictions exactly.

The artifact explicitly says:

```json
{"stage":"U05b.1","optimization_complete":true,"trained":false,
 "validation_status":"not_validated","promotion_eligible":false,
 "control_eligible":false}
```

The weights have learned; formal validation/publication has not happened. Setting
`trained=true` merely because Adam ran would violate the requested acceptance
boundary. The existing U04 scorer checkpoint remains a structural observer-only
format. A core wrapper applies its persisted normalization for offline scoring;
no runtime consumes the new artifact format.

`core_artifact_status(artifact, freshly_prepared_dataset)` diagnoses
`requires_rebuild` when dataset content or supervision revision changed, including
undo. It never subtracts a correction from weights or changes a parent. This is
not a persisted invalidation ledger or an atomic fresh-evidence publication gate;
those are required U05b.3 work. Nothing from this PR is promotable even if its
diagnostic holdout accuracy is perfect.

## Tests and host evidence

Fourteen focused tests cover real learning, finite-difference gradients, variable
action counts/weights, continuous/categorical learning, future evaluation,
holdout-label noninterference, deterministic serialization, disk reload,
corruption, undo diagnosis, missing/forbidden data, resource caps, cancellation,
admission and production-path isolation.

The synthetic motion -> room context -> action fixture improves future accuracy
from 50% to 100%, reduces loss and changes scorer weights, with exact parent/
encoder checkpoint parity. This demonstrates numerical learning on a constructed
relation, not recognition/control quality in an actual home.

Host WSL Python 3.12/NumPy 2.3.3, 24 epochs, actual cooperative budget, one BLAS
thread, frozen encoder included in preparation:

| Sensors/devices | Samples | Total seconds | Process peak RSS MiB | Future accuracy |
| --- | ---: | ---: | ---: | ---: |
| 5/1 | 64 | .295 | 38.38 | 1.0 |
| 50/10 | 80 | .849 | 39.26 | 1.0 |
| 200/30 | 120 | 3.267 | 40.51 | 1.0 |
| 1000/200 | 200 | 20.533 | 52.30 | 1.0 |

`benchmarks/UNIVERSAL_U05B1_HOST.json` retains each epoch duration, total and CPU
time, preparation, validation, update count, feature bytes, checkpoint bytes,
prediction costs and platform/source identity. Peak RSS is process high-water
across sequential scenarios, not independently measured per-scenario allocation.
Prediction timings include a **full offline frozen encoding**, not incremental
runtime inference. Values include cooperative yielding and are informational;
they are not Raspberry Pi results or event-to-intent/HA/UI acceptance measurements.

Run:

```sh
python -m unittest discover -s tests -p test_universal_scorer_training.py -v
python tools/benchmark_universal_training.py --output benchmark.json
```

CI retains all .232 checks and adds a small host-only optimizer smoke. Final
source/full-suite and CI receipts are linked from `UNIVERSAL_U05B_HANDOFF.md`.
