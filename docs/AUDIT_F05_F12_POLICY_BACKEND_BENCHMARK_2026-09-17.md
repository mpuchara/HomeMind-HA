# Stage 12 — policy backend benchmark (F05, F12)

## Decision

The production default stays `DiagonalLinUCB`. Stage 12 adds one linear alternative only: `FullRidgeLinUCBBackend v2`. PPO/DQN or another nonlinear backend is **not** installed automatically.

The candidate is deliberately limited to a small explicit semantic feature vector and models the full covariance matrix inside that vector. This addresses the main limitation of the diagonal learner — it can represent individual slots but cannot account for correlations between them.

## Numerical model

`FullRidgeLinUCBBackend` implements `PolicyBackend` and has versioned JSON persistence.

Per action and horizon it stores:

- `A = lambda I + sum(x x^T)`;
- `b = sum(r x)`;
- effective counts/reward sums;
- held-out validation reliability.

Prediction solves `A theta = b` and `A z = x` using Cholesky. The implementation symmetrizes matrix reads and permits only bounded numerical jitter (`0 .. 1e-4`) as a round-off fallback. Ridge itself is the model prior and decays back toward `lambda I`, not toward zero.

The production 128-dimensional policy vector is **not** turned into a 128x128 matrix. The benchmark selects a bounded semantic projection (default max 24) from the training split only.

For Observation v12, entity channels are selected atomically when labels are available: `value`, `valid`, communication/event age, quality, three lag/trend channels, time-since-edge and categorical bits stay together. A sparse zero in the vector therefore does not cause the selector to drop the accompanying validity/quality semantics. Older unlabelled rows remain supported as a conservative per-index legacy fallback.

New TrialRecords persist `policy_feature_labels`; manual benchmark episodes reconstruct the current v12 label map from the stored schema. Existing historical records are not rewritten.

## Fair benchmark contract

Both backends see:

1. the exact same chronological episodes;
2. the exact same allowed action set for every episode;
3. the same reward only for the action that was actually executed;
4. the same untouched future test split.

Data is split chronologically into train / validation / future test. Candidate feature selection sees training only. Ridge hyperparameter selection sees train + validation only. The future test is not used for either choice.

The current `DiagonalLinUCB` implementation remains the production reference. The benchmark now reports two diagonal controls:

- the current full-vector production DiagonalLinUCB;
- a representation-matched DiagonalLinUCB using the **same selected semantic projection** as the full-ridge challenger.

The backend-effect claim is made only against the representation-matched control. The candidate must also avoid regression against the production full-vector reference. This prevents a feature-selection change from being misreported as a backend improvement.

## Demonstrations versus bandit feedback

These are deliberately separate.

### Demonstration

An explicit manual/correction demonstration may say that action `a` was desired. Stage 12 may train `a` as a positive demonstration. It does **not** invent negative rewards for every other action.

Replay of a rigid automation is not counted as policy-quality evidence in the headline demonstration metric.

### Contextual-bandit TrialRecord

Stage-11 `TrialRecord` is the source of logged physical-trial data. Only `assigned_action` owns the measured reward. `action_set` propensities are retained.

For off-policy reporting, if the evaluated policy selects an action whose logged propensity is absent or zero, the benchmark reports `bandit_supported=false` and refuses an IPS estimate. It never fills in a counterfactual reward.

## Metrics

Action choice and calibration are reported separately.

The benchmark reports:

- explicit-demonstration action accuracy;
- IPS reward only when propensity coverage exists;
- reward calibration MAE only on rows where the evaluated action was actually executed;
- mean inference time;
- training time;
- serialized model bytes;
- approximate live Python numeric-state bytes (`python_state_bytes`) reported separately from process RSS;
- a small-correction learning curve at 1/2/4/8/16 explicit corrections;
- chronological split timestamps and selected feature indices/hyperparameters.

No claim of superiority is made from reproducing an existing fixed automation.

## Shadow / flag behavior

The live backend is never switched by this stage.

`tools/benchmark_policy_backends.py` is opt-in and requires either:

- `--enable-shadow`, or
- `HOMEMIND_POLICY_BACKEND_SHADOW=1`.

The tool reads manual demonstrations and labelled Stage-11 TrialRecords, persists a versioned row in `policy_backend_benchmarks`, and prints the report. It does not write `rl_models`, dispatch a service, create an `ActionIntent`, or invoke `Executor`.

`PolicyBackendShadowService` is non-controlling by contract (`dispatch_capability=false`) and is now wired into the shipped runtime as an **optional observer**. It can be enabled with `policy_backend_shadow_enabled=true` or `HOMEMIND_POLICY_BACKEND_SHADOW=1`.

When enabled it first requires the latest persisted benchmark for that agent to be `BENCHMARK_VERSION>=2` with `candidate_status=shadow_candidate_supported`. Without that proof it reports `shadow_waiting_for_supported_benchmark` and creates no challenger. A supported Shadow uses exactly the benchmark's feature projection and ridge/alpha. It then observes the same live feature vector and allowed action set after the production policy prediction, but its result is never used to choose the `ActionIntent`. It receives reward only for the action actually executed. Durable TrialRecords are also consumed exactly once using a persistent source marker, preserving their logged propensity. Disabled mode performs no shadow inference or learning.

## Additive persistence

Stage 12 adds only diagnostic state:

- `policy_backend_benchmarks` — persisted benchmark result;
- `policy_backend_shadow_models` — versioned shadow candidate state;
- `policy_backend_shadow_events` — shadow diagnostics;
- `policy_backend_shadow_sources` — exactly-once source markers for durable TrialRecord rewards.

Existing policy/schema versions, vectors, feedback, TrialRecords, candidate history and rollback data are untouched.

## Promotion policy

The benchmark output always records:

- `default_backend = diagonal_linucb`;
- `automatic_backend_switch = false`.

A candidate may be marked `shadow_candidate_supported` only when it has a positive untouched-future gain over the representation-matched diagonal control, has no measured regression on the other available action-quality metric, does not regress the production diagonal reference, and does not violate logged-bandit propensity coverage. Otherwise the result is `keep_diagonal_default`.

Even `shadow_candidate_supported` is **not** an automatic promotion. A later release would need independent real future evidence and the normal candidate/promotion safety gates before changing the production backend.

## Required scenarios

Deterministic tests cover:

- versioned `PolicyBackend` round-trip;
- correlated/duplicated feature stability;
- a dependency visible through an explicit interaction slot;
- missing features;
- chronological future holdout and no future leakage into feature selection;
- bandit reward applied only to the executed action;
- automation replay excluded from demonstration-quality claims;
- off-policy refusal when propensity coverage is absent;
- inference/training/storage and calibration reporting;
- Stage-11 TrialRecord propensity preservation;
- non-controlling shadow behavior.

The benchmark harness can represent drift because the future split is chronological rather than shuffled. No real HA deployment or physical experiment is required by the deterministic tests.

## Current-stack hardening

After Observation v12 and TrialRecord v2 the original Stage-12 implementation had three gaps:

1. full-ridge used a bounded projection while the only diagonal baseline used the whole vector, mixing backend and representation changes;
2. per-index feature selection could keep `:value` while dropping v12 `:valid`/`:quality` siblings;
3. `PolicyBackendShadowService` existed as a library/test helper but was not installed in the final runtime.

The current contract fixes all three. `BENCHMARK_VERSION=2`, the full-ridge backend uses `feature_contract=semantic_projection_v2`, and its backend version is bumped to v2. Old v1 shadow state is rejected as `NEEDS_RETRAIN` instead of being reinterpreted under the new projection.

Runtime path remains:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`

Shadow remains observer-only and cannot create an `ActionIntent` or call `Executor`.