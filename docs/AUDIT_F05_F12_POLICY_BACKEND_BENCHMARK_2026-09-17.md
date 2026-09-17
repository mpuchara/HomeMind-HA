# Stage 12 — policy backend benchmark (F05, F12)

## Decision

The production default stays `DiagonalLinUCB`. Stage 12 adds one linear alternative only: `FullRidgeLinUCBBackend v1`. PPO/DQN or another nonlinear backend is **not** installed automatically.

The candidate is deliberately limited to a small explicit semantic feature vector and models the full covariance matrix inside that vector. This addresses the main limitation of the diagonal learner — it can represent individual slots but cannot account for correlations between them.

## Numerical model

`FullRidgeLinUCBBackend` implements `PolicyBackend` and has versioned JSON persistence.

Per action and horizon it stores:

- `A = lambda I + sum(x x^T)`;
- `b = sum(r x)`;
- effective counts/reward sums;
- held-out validation reliability.

Prediction solves `A theta = b` and `A z = x` using Cholesky. The implementation symmetrizes matrix reads and permits only bounded numerical jitter (`0 .. 1e-4`) as a round-off fallback. Ridge itself is the model prior and decays back toward `lambda I`, not toward zero.

The production 128-dimensional policy vector is **not** turned into a 128x128 matrix. The benchmark selects a bounded set of observed explicit feature slots (default max 24) from the training split only. Existing HomeMind feature slots already carry semantic value/lag/trend/edge/interaction meaning; Stage 12 does not replace that representation.

## Fair benchmark contract

Both backends see:

1. the exact same chronological episodes;
2. the exact same allowed action set for every episode;
3. the same reward only for the action that was actually executed;
4. the same untouched future test split.

Data is split chronologically into train / validation / future test. Candidate feature selection sees training only. Ridge hyperparameter selection sees train + validation only. The future test is not used for either choice.

The current `DiagonalLinUCB` implementation is used directly as the baseline adapter; it is not replaced by a reimplementation.

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
- serialized model bytes as a deterministic storage/RAM proxy;
- a small-correction learning curve at 1/2/4/8/16 explicit corrections;
- chronological split timestamps and selected feature indices/hyperparameters.

No claim of superiority is made from reproducing an existing fixed automation.

## Shadow / flag behavior

The live backend is never switched by this stage.

`tools/benchmark_policy_backends.py` is opt-in and requires either:

- `--enable-shadow`, or
- `HOMEMIND_POLICY_BACKEND_SHADOW=1`.

The tool reads manual demonstrations and labelled Stage-11 TrialRecords, persists a versioned row in `policy_backend_benchmarks`, and prints the report. It does not write `rl_models`, dispatch a service, create an `ActionIntent`, or invoke `Executor`.

`PolicyBackendShadowService` is also non-controlling by contract (`dispatch_capability=false`). It is provided for controlled shadow integration; it learns only logged executed actions and never generates a physical command.

## Additive persistence

Stage 12 adds only diagnostic state:

- `policy_backend_benchmarks` — persisted benchmark result;
- `policy_backend_shadow_models` — versioned shadow candidate state;
- `policy_backend_shadow_events` — shadow diagnostics.

Existing policy/schema versions, vectors, feedback, TrialRecords, candidate history and rollback data are untouched.

## Promotion policy

The benchmark output always records:

- `default_backend = diagonal_linucb`;
- `automatic_backend_switch = false`.

A candidate may be marked `shadow_candidate_supported` when it wins the untouched future demonstration metric without violating logged-bandit coverage/performance. Otherwise the result is `keep_diagonal_default`.

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
