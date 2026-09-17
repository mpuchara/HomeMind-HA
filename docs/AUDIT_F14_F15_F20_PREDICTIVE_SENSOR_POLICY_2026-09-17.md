# Audit F14–F15–F20 — predictive sensor policy promotion

Date: 2026-09-17

## Runtime path

The packaged entrypoint remains:

`run.sh -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py`

Stage 09 is composed in `fast_queue_main.prepare_engine_extensions()` after the existing
Tournament metrics, promotion, primary protection and sensor-quality layers, and before
schema history / requalification / probation. This order is intentional: the old champion
must still be snapshotted by probation, while the policy visible after a Tournament
promotion must already be the exact trained challenger.

## What changed

### 1. Broad observed pool is separate from the active feature schema

`context_tournament_observed_pool` is a new additive SQLite table. It keeps bounded,
interpretable evidence for a broader set of eligible Home Assistant entities while the
live `ExplicitFeatureSchema` remains small.

The pool records availability/unknown/unavailable observations, changes, recent scalar
history, independent target episodes, own-action leakage hits and semantic screening
results. It does **not** directly alter control policy weights.

This fixes the previous coupling where an entity practically had to be in, or very close
to, the compact schema before it could accumulate useful selection evidence.

### 2. Historical relevance and correlations are screening, not causality

Historical `context_relevance` remains useful to decide which hypotheses are worth
measuring, but it is now explicitly combined with semantic screening and exposed as
screening only.

The semantic groups are:

- current value,
- sensor quality/health,
- 1 s / 3 s / 10 s lag values,
- trend,
- time since the last edge/change,
- a small set of interactions with the highest-priority active/primary features.

A screening score can select or prioritize a challenger. It cannot promote it. The public
result name is `predictive_gain`; Stage 09 makes no `causal influence` claim.

The selected interaction screen is important for signals whose marginal correlation is
near zero but which become predictive together with an existing feature.

### 3. `targeted_sensor` is research priority only

The existing Targeted Explore workflow can still force a selected Home Assistant entity
into challenger priority. Stage 09 does not convert that choice into evidence. The sensor
must still accumulate future paired outcomes, pass health and leakage gates, clear the
predictive-gain threshold and survive the existing post-promotion probation.

A targeted sensor can therefore end in:

- `needs_more_data`,
- neutral/no measurable gain,
- blocked by quality/redundancy/leakage/cost,
- or promoted after independent future evidence.

### 4. The challenger is now the exact policy that can be deployed

This is the F15 fix.

For each selected challenger Stage 09:

1. snapshots the current real `MultiHorizonPolicy`;
2. chooses and freezes a target schema (append or replacement) while preserving explicit
   user inputs and healthy primary-source protection;
3. clones the policy and applies the existing label-based `_migrate_schema()` to that
   clone;
4. predicts in Shadow with the cloned target-schema policy;
5. scores champion and challenger on the same future target transition;
6. only after scoring, teaches that exact candidate from the observed outcome;
7. persists the trained candidate policy in the Tournament shadow state;
8. on promotion, copies the trained candidate heads/schema/model revision into the live
   policy rather than adding a zero-weight column to the champion.

The comparison order is therefore:

`predict -> paired score -> candidate learn`

and the promotion object is the candidate that produced the challenger predictions.

If the final quality/primary replacement planner selects a different target schema than
the one evaluated, promotion is blocked, paired evidence is reset and the new exact
schema must be trained/evaluated again. Evidence from one schema is never transferred as
proof for another schema.

### 5. Same future episodes and version contract

The existing prequential Tournament epoch remains authoritative. Stage 09 additionally
persists for the policy candidate:

- active Tournament schema revision,
- champion model revision,
- policy version,
- feature-schema version,
- exact candidate target schema,
- candidate model revision and training sample count.

The champion and challenger are scored on the same pending future transition. Candidate
learning occurs only after this pair has been scored. A champion/schema revision change
continues to invalidate the Tournament epoch through the existing metrics layer.

### 6. Redundancy, health, own-action leakage, multiple testing and feature cost

Promotion has additional conservative gates:

- **sensor health:** insufficient observations or <80% availability blocks Stage-09
  promotion;
- **broken primary:** a primary source with enough observations and <50% availability is
  no longer protected by the extra primary replacement margin; explicit user-selected
  inputs remain protected;
- **redundancy:** a challenger with >=12 aligned independent target episodes and absolute
  value correlation >=0.97 with an active feature is treated as a duplicate and blocked;
- **own-action leakage:** if a sensor repeatedly changes within 5 seconds after the
  agent's own command, it cannot justify promotion when the leak rate reaches the
  conservative gate;
- **multiple testing:** the required predictive gain receives a small logarithmic penalty
  based on the number of screened hypotheses;
- **feature cost:** the required gain also includes a small explicit penalty for schema
  complexity/interactions and very high event frequency.

These controls only make promotion stricter. Existing default Tournament thresholds were
not lowered.

## Migration and rollback

The migration is additive:

- new table: `context_tournament_observed_pool`;
- existing `context_tournament_state`, shadow, promotion, schema-history and probation
  tables remain readable;
- `ExplicitFeatureSchema` and `MultiHorizonPolicy` versions are **not** changed in Stage 09;
- stored feature vectors are not reinterpreted under a new layout;
- existing schema migration still maps evidence by explicit feature labels;
- old Tournament shadow rows without a policy candidate lazily acquire a fresh exact
  candidate for the current evaluation epoch.

Post-promotion `context_schema_probation` is unchanged and still stores the exact previous
serialized policy. A regression therefore restores the complete former model/schema, not
just the entity list.

## Tests added

`tests/test_context_tournament_policy_candidate.py` covers:

- sensor useful as a precursor through lag features,
- dependence visible only in a selected interaction,
- duplicate/redundant sensor,
- chance historical/screening correlation with neutral future gain,
- broken primary replacement,
- new sensor without enough health/history,
- Control path without independent evidence,
- own-action effect leakage,
- F15 regression: promoted live policy receives the trained challenger's non-zero sensor
  weight instead of a newly appended zero-weight column.

## Example event flow

Assume a hallway sensor tends to change three seconds before a kitchen light transition:

1. the hallway entity is in the observed pool but not the active schema;
2. repeated independent transitions make its `lag_3` screening group interesting;
3. it enters the challenger set — this is still only a hypothesis;
4. an exact kitchen-light policy clone is built with the hallway sensor in its frozen
   target schema;
5. on future event N, champion and candidate both predict before the kitchen outcome;
6. event N is scored for both models from the same target transition;
7. the exact candidate is then trained from event N;
8. only after enough future paired evidence, health checks, multiplicity/cost gates and
   consecutive windows can the trained candidate be promoted;
9. the promoted policy enters the existing Shadow probation against the previous frozen
   champion and can still roll back automatically.

At no point does the Stage-09 candidate dispatch a physical command.

## Current limitations

- Semantic screening is intentionally small and interpretable; it is not a causal
  discovery system and does not attempt interventions.
- Redundancy currently uses aligned value correlation on independent target episodes; it
  does not yet perform multivariate conditional-independence testing.
- Own-action leakage is a conservative temporal post-command detector. Some genuine
  environmental feedback signals may therefore require more data rather than immediate
  promotion.
- Candidate training uses the existing `MultiHorizonPolicy` update contract and does not
  introduce a second policy backend or a large neural network.
- No real Home Assistant deployment or physical experiment is part of this audit task;
  validation uses deterministic tests/simulators and the existing CI image smoke test.
