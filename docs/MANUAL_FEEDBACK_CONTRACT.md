# Stage 06 — unified manual feedback and retractable corrections

This document describes the stage-06 manual-feedback contract introduced for F16–F18.
It applies to the runtime composed by `run.sh -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py/main.py`.

## Goals

Manual interaction is split into two independent effects:

1. **Immediate effect** — a physical user change, UI correction, one-shot Desired override or manual hold.
2. **Learning effect** — a durable feedback fact and, when a correct action is known and context is usable, an isolated Candidate update/rebuild.

A negative rating does not imply that an arbitrary alternative action is correct.  The journal therefore permits `correct_action = NULL`.

The ActionIntent–Executor boundary is unchanged.  The journal never creates an ActionIntent and never calls Home Assistant.  Physical UI corrections continue through the established service/executor path; Shadow and unpromoted Candidate generations remain non-controlling.

## Durable feedback fact

New feedback is stored in `manual_feedback_journal` and may link effects through `manual_feedback_effects`.
A record contains:

- immutable `feedback_id`;
- optional `decision_id`, `episode_id` and generation/root-generation linkage;
- `selected_ts` — the moment selected by the user, not merely the HTTP request time;
- `source` and rejected action;
- optional correct action;
- error kind: `state`, `too_early`, `too_late`, `brightness`;
- scope: `one_time`, `episode`, `similar_context`, `persistent_preference`;
- feature-schema and policy versions;
- semantic context signature and digest;
- application status, immediate effects, learning effects and undo status.

Repeated writes with the same `feedback_id` are idempotent only when immutable content is identical.  A different payload for an existing id is rejected.

Legacy Teaching and Teach-RL rows remain valid.  They are not reinterpreted.  New stage-06 feedback can link to new legacy-store labels so a later undo can retire the complete effect.

## Context signature and conflicts

`Teaching.signature()` now includes the shared seven home-trajectory features rather than dropping the tail of the policy vector.  It also records the feature-schema version, policy version and whether the home forecast is known.

The matcher deliberately ignores derived interaction terms, compares only compatible schema/policy versions and gives higher weight to presence/activity and home-trajectory features.  A local sensor snapshot that looks identical while the home trajectory is materially different is therefore not automatically treated as the same context.

When two active instructions prescribe materially different actions for semantically indistinguishable contexts, both are marked as `conflict`.  Conflict is sticky: adding a third label that agrees with only one side does not silently reactivate that side.  Conflict is resolved by undoing a conflicting fact or by supplying context that makes the cases distinguishable.

`one_time` corrections do not create persistent context conflicts.

The system does not try to force contradictory instructions to 100% fit by repeated model updates.

## Channel unification

The same journal is used by:

- physical HA-user changes;
- Change decision / Wrong decision;
- Teaching;
- Teach Desired;
- Teach RL;
- Correct and generation workflow corrections.

The workflow adapter links Correct/Change decision to the same fact and preserves generation lineage.

### Physical change

A real user device change remains immediate and receives manual priority/hold.  Under the stage-06 Candidate-only contract the historical direct negative/positive writes to the Live policy are suppressed.  The durable feedback fact is then routed to Candidate learning if a correct action and complete context are available.

Only one compatibility wrapper of `Engine.process_agent` is used: the pre-existing physical-equivalence bridge.  `manual_feedback_live_isolation.py` is now only an opt-in shim which sets `manual_feedback_candidate_only`; it does not wrap Engine methods.  Runtimes that do not enable this stage-06 flag retain the legacy behaviour.

### Teach Desired / Teaching

Teaching changes the current Desired decision without dispatching a physical command.  With complete context it records a retractable label and queues Candidate learning.  If context is incomplete, the user still gets a one-shot runtime Desired correction, but the system does not generalize from missing information.

### Negative-only feedback

A negative rating records the rejected action and error type but leaves `correct_action` unknown.  It cannot fabricate a positive label or train an arbitrary alternative.

## Undo

Undo is journal-driven and idempotent.

1. Linked Teaching/Teach-RL labels are retired.
2. Linked broad manual-context observations are removed.
3. Caches are invalidated.
4. If the fact could already have affected a Candidate/model, a **full historical rebuild child** is queued from the current root generation.
5. The current Live parent is not destructively rewritten.  Ordinary atomic promotion remains responsible for swapping ownership/model state.

No inverse gradient/update is used.  This makes undo valid after restart, rebuild and later Candidate generations while preserving lineage and rollback history.

## UI contract

Responses expose `ui_message` derived from the durable status.  The message distinguishes what happened immediately from what is merely queued for learning, for example:

- `urządzenie zmienione od razu; nauka idzie do Candidate`;
- `decyzja lokalna zmieniona od razu; etykieta zapisana do nauki`;
- `Feedback zapisany jako konflikt; nie uczę sprzecznej preferencji.`;
- `Korekta cofnięta; czysty Candidate został przebudowany z dziennika.`

This is intentionally different from claiming that Live weights changed.

## Additive migration

The migration only adds:

- `manual_feedback_journal` plus indexes;
- `manual_feedback_effects` plus index.

No saved feature vector, model, existing label, settings row, generation history or rollback state is rewritten.  A regression test snapshots existing tables and a sentinel row, runs the migration and verifies that the prior schema/data remain intact.

## Deterministic coverage

Stage-06 tests cover:

- a single correction;
- negative feedback without a correct action;
- delayed correction linked to nearby `decision_id` / `episode_id`;
- conflicting indistinguishable contexts;
- a one-time exception that does not poison persistent context;
- undo after restart after the label has reached Candidate learning;
- idempotent repeated undo;
- the same local observation under materially different home trajectories;
- physical user correction preserving immediate behaviour while not mutating Live policy;
- additive migration.

No test sends commands to a real Home Assistant instance and no physical experiment is run.

## Known limitation

The current compatibility architecture still uses the already-existing physical-equivalence wrapper around `Engine.process_agent`.  Stage 06 does not add a second wrapper: Candidate-only isolation is folded into that single contract.  A future core cleanup may move this callback directly into `Engine.process_agent`, but that is not required for the behavioural contract and would be a broader architectural change outside F16–F18.
