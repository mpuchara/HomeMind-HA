# Episode evaluation — fast light power v1

This document describes the stage-05 episode contract introduced after audit findings F05, F06, F13 and F24. The first implementation is deliberately limited to `target_property=power` for lights/fast binary targets.

## Production composition

The production entrypoint is still:

`adaptive_ai/src/run.sh` → `preference_queue_main.py` → `fast_queue_main.py` → `queue_main.py/core`.

`preference_queue_main.prepare_engine_extensions()` creates one `EpisodeEvaluator` **before** the fast runtime extensions are installed. The same evaluator is then shared by:

- resolved Live reward/outcome observations,
- `Experiments`,
- fast Sensor Tournament shadow comparisons,
- Candidate Shadow and Candidate promotion summaries.

The current runtime is still extension-composed. To preserve the existing atomic-promotion capture order, the final composition root temporarily intercepts the existing fast-light and Candidate-shadow installers, installs the episode adapter at the required point, then restores the module-level installer functions in `finally`. The important order is:

1. create/migrate `EpisodeEvaluator`,
2. attach Live + Experiment observation,
3. install fast runtime and Sensor Tournament,
4. install fast-light timing objective,
5. attach Tournament episode observation,
6. install Candidate Shadow context,
7. install preference metrics,
8. attach Candidate episode evaluation,
9. install atomic Candidate promotion,
10. install provenance and observation contracts before workers start.

This is a compatibility composition step for the current layered runtime, not a second physical control path. `episode_evaluator_runtime.py` never constructs `ActionIntent`, never calls `Executor.submit`, and never calls a Home Assistant service. Shadow, Tournament and unpromoted Candidate remain observation-only.

## Episode contract

A finalized episode has:

- stable `episode_id`, domain and agent,
- `start_ts` and `end_ts`,
- context metadata,
- observations and their observability,
- separate occupancy and light-need labels,
- optional replay of the old automation,
- the actual physical power trajectory when it was observed,
- one or more policy results evaluated on that exact episode,
- an explicit end reason.

A policy result records whether the policy was physically executed or is counterfactual. Shadow/Candidate/Tournament desired trajectories are therefore never presented as physical comfort outcomes.

## Evidence classes

The evaluator distinguishes:

- `physical` — the policy actually operated the device and physical power was observed,
- `counterfactual_proxy` — an unexecuted policy is compared with independent labels,
- `automation_replay_proxy` — agreement/disagreement with the previous automation only,
- `observed` — an independent label such as a verified no-arrival outcome or manual correction,
- `unknown` — insufficient observability.

`unknown` is represented as `None` plus evidence metadata. It is not converted to numeric zero and is not counted as a success.

The previous automation is stored in `automation_replay`. It is **not** copied into `light_need`. A later automation OFF is therefore not evidence that an earlier OFF was comfortable or correct.

## Metrics

For each policy and episode the evaluator can produce:

- `needed_on_delay_seconds`,
- `off_while_needed_seconds`,
- `unnecessary_on_seconds`,
- `false_arrival_prediction`,
- `arrival_prediction_confirmed`,
- `retrigger_count`,
- `chatter_count`,
- `manual_correction_count`,
- `automation_disagreement_seconds`,
- `physical_power_observed_seconds`.

Metrics are aggregated at episode level. Runtime ticks are not independent successes.

A day/interval with an independently known `light_need=false` and no device transition is still a meaningful negative opportunity. A fully observed predicted arrival with no arrival is also a negative opportunity. A failed sensor produces an unknown outcome instead of a zero-valued success.

## Candidate promotion gates

Stage-01 named promotion gates remain authoritative. The Candidate adapter exposes `episode_comparison` and `episode_evidence_mode` on the normal comparison summary.

When the Candidate and its direct parent have enough **independently labelled** common episodes to satisfy the existing coverage defaults (including per-action coverage), episode-level success/harm counts and episode-level preference confidence replace the legacy transition-event counts in the existing named gates. The defaults themselves are not lowered.

Until that independent coverage exists, the existing transition-based promotion metric remains a compatibility fallback. Automation-replay-only episodes are visible as `automation_replay_proxy`, but they do not get relabelled as comfort evidence and do not replace the independent episode gate input.

The existing hard vetoes from stage 01 (configuration/version, freshness, false-early safety, corrections, execution prerequisites and atomic promotion ownership) remain in the same final gate report.

## Experiments

For presence-focused power probes, a verified local no-arrival window is now evaluated as a negative prediction opportunity. Instead of the old weak-positive "observed without correction" interpretation, the Experiment adapter reuses the existing `RewardEngine` false-positive weight. This changes learning only when:

- the trial is a probe,
- the target is power,
- the outcome sources were verified and observable,
- no target-area arrival occurred within the defined observation window.

Missing/failed outcome sensing remains unknown (`reward=None` in the existing Experiment path).

A confirmed arrival is still only presence-prediction evidence. The evaluator does not manufacture a light-comfort label for the unobserved counterfactual.

## Persistence and migration

The migration is additive. Two new tables are created:

- `episode_evaluator_episodes`,
- `episode_evaluator_policy_results`.

No existing table is rewritten, and no stored feature vector changes interpretation. Existing agents, models, settings, labels, Candidate lineage, generation history and rollback data remain untouched.

Finalized episodes are immutable. Re-recording the exact same payload is idempotent; reusing an `episode_id` with different content raises an error rather than silently changing history.

Old installations have no backfilled episode truth. Historical transition pairs remain available as the explicit legacy/proxy fallback; the migration does not fabricate occupancy, light need or physical outcomes for old rows.

## Example event flow

Example: Candidate predicts early ON while the old automation remains OFF.

1. Live and Candidate run on the same state snapshot. Candidate remains Shadow; no `ActionIntent` is created for it.
2. Episode observation starts with physical power OFF. Candidate Desired=ON is stored as a counterfactual policy decision.
3. If the old automation later turns ON, that target transition is stored under `automation_replay`; it is not copied into `light_need`.
4. If a future independent label says light was needed during the interval, `needed_on_delay_seconds` can be compared for both policies on the same episode. Candidate numbers remain marked `counterfactual_proxy` because Candidate was not executed.
5. If independent light-need evidence is missing, comfort metrics remain unknown; only automation replay disagreement is available as proxy.
6. Once enough independently labelled common episodes exist, Candidate promotion gates consume episode-level quality. Atomic promotion is still performed by the existing promotion layer and ownership remains unchanged.

Example: presence experiment predicts an arrival but nobody comes.

1. Verified local presence source is OFF before the probe.
2. Probe executes early ON through the normal Executor path and receives ACK.
3. The observation window ends with the verified local presence source still OFF.
4. One episode is finalized with `false_arrival_prediction=1`; `light_need` remains unknown.
5. Experiment learning receives the existing false-positive reward penalty. There is no tick-by-tick sequence of successes or a fabricated comfort score.

## Known limitations of v1

- HomeMind still lacks a general independent light-need label source for every room. Therefore many current Live/Tournament/Candidate episodes are intentionally proxy/unknown until later preference/perception stages provide that evidence.
- Sensor Tournament records same-episode comparisons and exposes them in diagnostics, but its existing sensor-specific promotion metric remains the compatibility decision metric in this stage.
- Counterfactual Shadow metrics describe what the policy would have requested against known labels; they are not proof of physical comfort or actuator behaviour.
- The episode layer currently covers fast binary light power only. HVAC, dimming and long-horizon processes require separate dynamics and outcome contracts.
