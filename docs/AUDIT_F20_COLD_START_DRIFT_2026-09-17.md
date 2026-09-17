# Stage 14 — cold start and controlled drift adaptation (F20)

## Runtime path

The executable path remains:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py`

Stage 14 is installed as the last Candidate-side observer before workers start. It never creates `ActionIntent`, never calls Home Assistant services and never bypasses `Executor`. Its only adaptation action before promotion is to request the existing hidden Candidate workflow.

## Cold start

`AdaptationService.cold_start()` reports evidence instead of weakening gates.

It exposes:

- whether a persisted policy model exists;
- available/recognised context sensors and their current availability;
- target history size;
- explicit ON/OFF demonstration counts;
- an existing Home Assistant automation fallback when the ownership/handoff knowledge layer knows one;
- exact missing evidence;
- at most two optional demonstration questions.

No-history behaviour is `fallback_collect_evidence`, `fallback_plus_shadow_learning` or `shadow`. It never enables Control because history is missing and it never lowers Stage-13/Candidate thresholds.

The optional-question contract is deliberately passive: questions are suggestions only, are never asked automatically by this layer and never dispatch an action. An answer must enter through an existing explicit-demonstration/preference path.

## Drift inputs

The monitor consumes existing durable evidence instead of tick accuracy:

1. immutable `episode_evaluator` Live outcomes and episode costs;
2. explicit manual-correction counts;
3. time/context distribution of episodes;
4. active sensor health and unavailable fraction;
5. sensor-to-area topology signature from the current ContextEngine mapping;
6. durable `persistent_preference` revisions.

The monitor deduplicates episode IDs through its own additive journal and never converts missing episode evidence into success.

## Drift classification

Priority is intentionally diagnostic-first:

1. `sensor_failure` — three consecutive environment snapshots show sustained health/unavailability degradation;
2. `topology_change` — three old and three new stable snapshots disagree on sensor-to-area topology;
3. `new_preference` — a new durable persistent preference appears; it is an instruction and does not decay;
4. `new_habit` — future episode quality falls while activity time/context distribution or explicit corrections shift.

A single transient sensor outage is insufficient for adaptation.

A new household member is not inferred as identity. It can appear only as a changed anonymous context/occupancy distribution and is therefore classified as `new_habit`, not as a named-person claim.

## Candidate isolation and promotion

Sustained drift creates or reuses one existing hidden Candidate with reason `drift:<kind>`. Live is not reset and the Candidate cannot dispatch.

Stage 14 does **not** promote automatically. The existing Candidate chain, offline gates and Stage-13 fixed future evaluation remain authoritative. In particular, insufficient evidence, separate ON/OFF requirements, backend-specific recalibration and abstain/fallback semantics are unchanged.

## Decay contract

Stage 14 publishes one explicit semantic table:

- policy learning: wall-clock decay using the existing `policy_half_life_days` (default 30 days);
- independent future evaluation: episode-order decay using the Stage-13 half-life (40 episodes), outside the locked fixed-test window;
- persistent preference/instruction: **no decay**;
- regression anchor: retained for evaluation with `training_weight=0`.

This does not reinterpret old model vectors or rewrite historical weights. It documents which evidence is statistical versus instructional and uses the existing decay mechanisms rather than adding another learner-specific decay rule.

## Regression anchors

At drift detection up to eight pre-drift episode IDs are retained in `adaptation_regression_anchors`. They are references for regression tests only. `training_weight` is permanently `0`, so old examples remain auditable without acquiring unlimited training influence.

## Post-promotion recovery and rollback

When an adaptation Candidate is promoted through the normal atomic promotion path, Stage 14 records the exact `agent_generation_backups` snapshot produced immediately before the swap.

After at least six new meaningful Live episodes it reports:

- post-promotion quality;
- episode count to recovery;
- recovery timestamp.

If quality returns within 5 percentage points of the pre-drift baseline with no corrections, the adaptation is marked `recovered`.

If post-promotion quality is at least 20 points below the pre-drift baseline or at least two manual corrections occur in the six-episode monitoring window, the previous model/generation snapshot is restored under the existing Executor target lock. The previous generation number/model/benchmark state is restored and lineage points back to the previous Live generation. Control ownership is preserved when both old and new modes are Control; Control-to-Shadow rollback releases ownership through Executor.

## Additive persistence

New tables only:

- `adaptation_episode_observations`
- `adaptation_environment_snapshots`
- `adaptation_state`
- `adaptation_regression_anchors`

Existing agents, policies, feature schemas, TrialRecords, manual feedback, Candidate lineage, generation backups and Stage-13 evaluation epochs are not rewritten.

## Required scenario coverage

`tests/test_cold_start_drift.py` covers:

- new home with no history: missing evidence, fallback/Shadow, no relaxed gate;
- moved sensor: stable topology change creates an isolated Candidate only;
- changed activity time: quality drop + hour shift -> `new_habit`;
- new household pattern: anonymous context-distribution shift -> `new_habit`;
- durable correction after long silence: `new_preference`, no instruction decay;
- one transient sensor outage: no drift Candidate;
- sustained sensor degradation: `sensor_failure`;
- quality recovery: episodes-to-recovery is measured;
- post-promotion degradation: previous model snapshot is restored;
- no premature promotion: monitor never calls Promote.

## Known limits

- The context-distribution detector is intentionally small and interpretable; it is not a causal change-point model.
- New-household-member handling is anonymous by design. HomeMind detects changed occupancy/context patterns, not identity.
- Environment snapshots use active/recognised sensors. A completely new unused sensor is still discovered through the existing Stage-9 observed pool/tournament, not promoted directly by Stage 14.
- Rollback uses the existing 24-hour generation backup created by atomic Promote. If that snapshot has already been removed externally, automatic rollback refuses rather than inventing an older model.
- No physical Home Assistant drift experiment is performed by this stage.
