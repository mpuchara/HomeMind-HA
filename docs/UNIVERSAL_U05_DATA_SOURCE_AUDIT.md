# U05 data-source audit on pinned 0.14.232

This audit precedes U05a implementation. Existing production learning semantics
remain untouched. A source containing an action is not automatically supervision.

| Existing source | Useful evidence | Universal training eligibility |
| --- | --- | --- |
| HA Recorder via history importer | Event history, attributes, HA contexts | Import time cannot stand in for historical receipt time; missing receipts/topology are ineligible |
| `entity_history` local archive | Event `ts`, nullable `received_ts`, state, attributes, source | Require both clocks; preserve live-source precedence and reject legacy missing receipts |
| `observation_contract.FeatureJournal` | Versioned event key, event/received clocks, quality, protected windows | Reuse U02 bounded reader and causal replay; retention does not prove full 14-day coverage |
| `ManualFeedbackJournal` | Explicit source, selected/created times, desired action, generation/episode, conflicts and undo | Only explicit trusted sources and active non-conflicted entries; re-read current status for each build |
| `teaching_rl_labels` | Timestamp, desired, fingerprint, stable supervision id, undo | Reuse stable id through `manual_feedback_effects` links; unlinked rows lack sufficient universal provenance |
| `manual_context_feedback` | Broad context, source, supervision id and metadata | Useful provenance, not proof of complete dual-clock sensor replay or historical registry/mask |
| Correct broad capture | Bounded archive as-of states and candidate-context diagnostics | Existing `_historical_states` selects by event time and current topology; do not reinterpret it as a strict universal replay receipt |
| Agent decisions / historical experiences | Prediction, model lineage and actual later target states | Observations only; never self-confirmed labels |
| HomeMind executed actions | Dispatch evidence, provenance and resulting state | Observations only until independent trusted assessment |
| Candidate / Shadow predictions | Passive counterfactual comparison | Never evidence that a physical action was executed or correct |
| Physical outcomes | Measured effect after action | Separate outcome evidence, excluded from pre-decision inputs and from direct desired-action labels |
| HA automations | Provenance/context explaining a target transition | Not user preference or trusted desired-action supervision |

## Missing historical evidence

The universal live observers retain RAM state and do not persist historical
semantic registry revisions, device descriptors or legal-mask receipts. Therefore
ordinary old Correct data cannot simply be fed into the shared scorer. A strict
offline foundation must require explicitly supplied historical decision receipts
and reject missing evidence, rather than quietly using today's DeviceAgent or HA.

For a later Correct label, `created_ts` may follow the selected decision time.
It belongs only to supervision. A chronological train/holdout cut must account
for label availability too, so future corrections cannot enter past training.
Episode and supervision identities must never cross the partition boundary.

Undo must be resolved before deduplication: a revoked logical event cannot
survive through an older active physical copy on another generation. The future
trainer must rebuild an isolated child after revocation; filtering new data
cannot subtract an already learned correction from a checkpoint.

## Initial conservative sensor policy

U05a must exclude known target-affected sensor ids and initially exclude power
and illuminance inputs until the production causal photometry contract is
adapted. Old post-action lux cannot be used to explain a pre-action decision.
Missing historical required sensors, registry or action authority cause explicit
ineligibility. Unknown/unavailable readings may be represented by existing masks,
but an entirely unusable context cannot qualify as a training example.

## Scope boundary

U05a supplies versioned, bounded, deterministic offline samples and independent
future holdouts. U05b owns gradient training and versioned model checkpoints;
U05c owns preference adapters/Correct rebuilds; U05d owns lifecycle integration.
No new production backend, automatic promotion, active-model mutation or dispatch
is permitted by this data foundation.
