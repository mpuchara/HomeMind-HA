# Episode evaluation — fast light power v1

This document describes the stage-05 episode contract introduced after audit findings F05, F06, F13 and F24. The first implementation is deliberately limited to `target_property=power` for lights/fast binary targets.

## Production composition

The production entrypoint remains:

`adaptive_ai/src/run.sh` → `preference_queue_main.py` → `fast_queue_main.py` → `queue_main.py/core`.

`preference_queue_main.prepare_engine_extensions()` creates one `EpisodeEvaluator` **before** the fast runtime extensions are installed. The same evaluator is shared by resolved Live outcomes, `Experiments`, fast Sensor Tournament shadow comparisons, and Candidate Shadow/promotion summaries.

`fast_queue_main` exposes two named, deterministic composition hooks instead of requiring another module-function monkey patch:

- `after_fast_light`, after the existing fast-light timing objective is attached to Sensor Tournament;
- `after_candidate_shadow_context`, after exact Candidate Shadow context exists and before atomic promotion is installed.

The final composition root registers its hooks only for the duration of composition and removes them in `finally`. The order is:

1. create/migrate `EpisodeEvaluator`,
2. attach Live + Experiment observation,
3. install fast runtime and Sensor Tournament,
4. install fast-light timing objective,
5. attach Tournament episode observation via the named hook,
6. install Candidate Shadow context,
7. install preference metrics and Candidate episode evaluation via the named hook,
8. install atomic Candidate promotion,
9. install provenance and observation contracts before workers start.

The named hooks are a composition contract, not a second physical control path. `episode_evaluator_runtime.py` never constructs `ActionIntent`, never calls `Executor.submit`, and never calls a Home Assistant service. Shadow, Tournament and unpromoted Candidate remain observation-only.

## Episode contract

A finalized episode has a stable `episode_id`, domain and agent, `start_ts`/`end_ts`, context metadata, observations and observability, separate occupancy and light-need labels, optional old-automation replay, the physical power trajectory when observed, one or more policy results evaluated on the same episode, and an explicit end reason.

A policy result records whether it was physically executed or counterfactual. Shadow/Candidate/Tournament desired trajectories are therefore never presented as physical comfort outcomes.

## Evidence classes

The evaluator distinguishes:

- `physical` — the policy actually operated the device and physical power was observed;
- `counterfactual_proxy` — an unexecuted policy is compared with independent labels;
- `automation_replay_proxy` — agreement/disagreement with the previous automation only;
- `observed` — an independent observation such as verified no-arrival or manual correction;
- `unknown` — insufficient observability.

`unknown` is represented as `None` plus evidence metadata. It is not converted to numeric zero and is not counted as success.

The previous automation is stored in `automation_replay`. It is **not** copied into `light_need`. A later automation OFF is therefore not evidence that an earlier OFF was comfortable or correct.

## Metrics

For each policy and episode the evaluator can produce `needed_on_delay_seconds`, `off_while_needed_seconds`, `unnecessary_on_seconds`, `false_arrival_prediction`, `arrival_prediction_confirmed`, `retrigger_count`, `chatter_count`, `manual_correction_count`, `automation_disagreement_seconds`, and `physical_power_observed_seconds`.

Metrics are aggregated at episode level. Runtime ticks are not independent successes. A known `light_need=false` interval with no device transition is still a meaningful negative opportunity. A fully observed predicted arrival with no arrival is also a negative opportunity. Failed sensing produces unknown, not a zero-valued success.

## Candidate promotion gates

Stage-01 named promotion gates remain authoritative. The Candidate adapter exposes `episode_comparison` and `episode_evidence_mode` on the normal comparison summary.

When Candidate and direct parent have enough **independently labelled** common episodes to satisfy the existing coverage defaults, including per-action coverage, episode-level success/harm counts and episode-level preference confidence replace legacy transition-event counts in the existing named gates. The thresholds themselves are not lowered.

Until independent coverage exists, the existing transition-based metric remains a compatibility fallback. Automation-replay-only episodes are visible as `automation_replay_proxy`, but they are not relabelled as comfort evidence and do not replace the independent episode gate input.

Existing hard vetoes from stage 01 — configuration/version, freshness, false-early safety, corrections, execution prerequisites and atomic promotion ownership — remain in the same final gate report.

## Experiments

For presence-focused power probes, a verified local no-arrival window is evaluated as a negative prediction opportunity. Instead of the old weak-positive "observed without correction" interpretation, the adapter reuses the existing `RewardEngine` false-positive weight. This change applies only when the trial is a probe, target property is power, outcome sources were verified/observable, and no target-area arrival occurred within the defined window.

Missing or failed outcome sensing remains unknown in the existing Experiment path. A confirmed arrival is presence-prediction evidence only; the evaluator does not manufacture a light-comfort label for an unobserved counterfactual.

## Persistence and migration

The migration is additive. Two tables are created:

- `episode_evaluator_episodes`;
- `episode_evaluator_policy_results`.

No existing table is rewritten, and no stored feature vector changes interpretation. Existing agents, models, settings, labels, Candidate lineage, generation history and rollback data remain untouched. A dedicated migration test snapshots existing tables/data and verifies that only the two new episode tables are added.

Finalized episodes are immutable. Re-recording the exact same payload is idempotent; reusing an `episode_id` with different content raises an error rather than silently changing history.

Old installations have no backfilled episode truth. Historical transition pairs remain available as explicit legacy/proxy evidence; the migration does not fabricate occupancy, light need or physical outcomes for old rows.

## Example event flow

Candidate predicts early ON while the old automation remains OFF. Live and Candidate run on the same state snapshot, but Candidate remains Shadow and creates no `ActionIntent`. The episode starts with physical power OFF and Candidate Desired=ON stored as a counterfactual decision. If the old automation later turns ON, that transition is stored under `automation_replay`, never copied into `light_need`. If an independent future label says light was needed during the interval, both policies can be compared on the exact same episode; Candidate numbers remain `counterfactual_proxy`. If independent light-need evidence is missing, comfort metrics remain unknown and only replay-proxy evidence is available. Once enough independently labelled common episodes exist, the normal stage-01 gates consume episode-level quality. Atomic promotion is still performed by the existing promotion layer and preserves ownership.

For a presence experiment that predicts arrival but nobody comes, a verified local presence source is OFF before the probe, the probe executes only through the normal Executor path, ACK is observed, and the observation window ends with the verified local presence source still OFF. One episode is finalized with `false_arrival_prediction=1`; `light_need` remains unknown. Experiment learning receives the existing false-positive reward penalty. There is no tick-by-tick series of successes and no fabricated comfort score.

## Known limitations of v1

HomeMind still lacks a general independent light-need label source for every room, so many current Live/Tournament/Candidate episodes intentionally remain proxy/unknown until later preference/perception stages provide that evidence. Sensor Tournament records same-episode episode diagnostics, but its existing sensor-specific promotion metric remains the compatibility decision metric in this stage. Counterfactual Shadow metrics describe what a policy would have requested against known labels; they are not proof of physical comfort or actuator behaviour. The episode layer currently covers fast binary light power only; HVAC, dimming and long-horizon processes require separate dynamics/outcome contracts.
