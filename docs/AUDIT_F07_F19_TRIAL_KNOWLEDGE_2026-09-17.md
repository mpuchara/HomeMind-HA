# Audit F07/F19 — durable experiment knowledge (2026-09-17)

## Scope

Stage 11 fixes the Free Explore → outcome → Candidate ownership gap. The audited flow let the `Experiments` residual learner mutate the root Live owner and then queued the child for ordinary continuation/history training. That made the physical trial and the generation that was meant to learn from it different owners of knowledge.

The selected integration is **explicit child training from versioned TrialRecords**. We intentionally do not add a runtime base+residual policy composition. A Candidate is one complete policy snapshot: exact parent model + explicit labelled trial updates. This makes rollback identical to the existing generation rollback — restore the parent snapshot — and avoids a second runtime policy component that would need separate ownership and rollback rules.

## Runtime composition

The executable path is now:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`

`trial_queue_main.py` installs Stage 11 after the existing Agent Explore composition and before workers start. Existing `ActionIntent -> Executor` ownership is unchanged. Candidate Shadow never sends a physical command and Stage 11 adds no HA service call.

## TrialRecord v1

The additive `experiment_trial_records` table stores one row per started trial. It records:

- version and stable `trial_id`;
- Live/root owner, Explore `session_id`, direct child generation;
- bounded hypothesis and context;
- policy/schema/experiment versions;
- action set with logging propensities;
- assigned action and its actual propensity;
- baseline/reference;
- dispatch and ACK;
- verified outcome sources;
- final episode-style result, reward and termination reason;
- exactly-once learning marker: generation, resulting model revision and timestamp.

No existing experiment metadata, policy vector, schema, labels, generation history or rollback rows are reinterpreted.

## Knowledge ownership

During Free Explore, the existing `Experiments._finish()` path still completes its normal session accounting. Stage 11 snapshots the Live residual learner before that call and restores it afterwards. Therefore budget/cooldown/config restoration and events remain compatible, but a labelled Free Explore outcome no longer changes the Live residual policy.

When the session has enough labelled outcomes, the already-created direct child is built as follows:

1. copy the exact parent model/config/history snapshot;
2. load only labelled TrialRecords owned by that Explore session;
3. update the exact assigned action at the exact recorded horizon/context with the recorded reward;
4. persist the complete child model and TrialRecord application markers in the same SQLite transaction;
5. run the existing offline regression gate;
6. continue through the existing Candidate paired-comparison/promotion lifecycle.

Ordinary historical replay is not executed in this path. In particular, a trial with reward `-1` remains a negative update to the assigned action even if the physical state stayed at that action later. A trial without an outcome remains unlabelled and cannot update the policy.

## Restart/idempotency

The child model write and `learning_applied_generation_id` marker commit together. A worker retry or restart sees the durable marker and does not apply the same trial twice. Active experimental state still follows the existing Experiments restart rule; interrupted observations do not fabricate a reward.

## Safe hypothesis catalog

The Stage-11 Free Explore MVP accepts only light targets with one of:

- `earlier_on`: a bounded OFF→ON trial for a light;
- `small_brightness_adjustment`: a small adjacent brightness/level correction, additionally capped to 5% of the configured range and all existing device/control bounds.

Other target/property combinations are rejected at the Free Explore workflow boundary.

## Informational exploration

Free Explore defaults to `information_exploration=true`. Normal Experiments proposal runs first. Only when it reaches its final `No nearby action reachable within bounded perturbation` branch may Stage 11 nominate a safe adjacent action for information value even if the base-policy gradient is zero/non-positive.

This fallback does **not** bypass earlier guards. Control qualification, global single physical-probe ownership, pending action priority, manual hold/cooldown, daily budget, interval, confidence, context support/novelty and device legality were already checked by the normal proposer. The fallback chooses only within the restricted light hypothesis catalog and uses an explicit 25% reference / 75% probe logging policy so propensity is known.

## Off-policy reporting

`TrialJournal.off_policy_report()` checks logged action-set propensities. If the requested action had no positive logging propensity, it returns `supported=false`, `reason=no_propensity_coverage`, and no estimate. Stage 11 deliberately does not invent an off-policy estimator merely because TrialRecords exist.

## Rollback

Trial learning exists only inside the child complete policy snapshot. The parent is not mutated. Discarding/rolling back the child therefore removes the whole trial-derived update by restoring/retaining the exact parent policy. There is no separate residual state to forget to roll back.

## Compatibility and limitations

- Migration is additive (`experiment_trial_records` only).
- Existing experiment `app_meta` format is retained.
- Existing Agent Explore session rows and Candidate generation history are retained.
- No policy or ExplicitFeatureSchema version is changed.
- Logging propensity reconstruction for legacy gradient-nominated trials follows the current residual choice rule; information-only fallback owns its randomization directly.
- Stage 11 stores coverage for later off-policy analysis but intentionally does not compute a counterfactual value estimate.
- No real Home Assistant deployment or physical experiment is performed by tests.


## Hardening po EpisodeEvaluator / Observation v12

TrialRecord ma obecnie kontrakt v2. Nowe rekordy dostają addytywne `episode_id` i
`learning_status`; istniejące rekordy v1 zachowują własny `record_version` i nie są
reinterpretowane przy retry lub migracji.

Finalizacja Free Explore najpierw rozwiązuje wspólny wynik Stage-05 EpisodeEvaluator,
a dopiero potem przekazuje ten sam `reward/reason` do Agent Explore, dziennika i treningu
Candidate. Dzięki temu np. obserwowalny false-arrival nie może być dodatni w TrialRecord,
jeżeli EpisodeEvaluator zaklasyfikował go jako negatywny. Pełny payload epizodu jest
zapisywany w `episode_result_json` wraz z trwałym `episode_id`.

Naprawiono także kolejność trwałości: TrialRecord outcome jest commitowany przed
wybudzeniem workera Candidate. Worker nie może już zobaczyć `FREE_TRAIN_REASON` zanim
reward/status próby znajdzie się w dzienniku.

Przy każdej nowej partii wyników dziecko jest odbudowywane z dokładnego parent snapshot,
a następnie deterministycznie odtwarzane są wszystkie zgodne, oznaczone wyniki prób z
tej sesji. Nie są stosowane wyłącznie 'nowe' rekordy, bo taki algorytm usuwał wpływ
wcześniejszych prób po drugim rebuildzie.

Interpretacja rekordu jest wersjonowana przez policy/schema contract. Rekord z niezgodną
wersją pozostaje audytowym faktem, ale otrzymuje `learning_status=ineligible:*` i nie
jest używany do aktualizacji polityki. Observation v12 jest odczytywany z `schema.VERSION`,
nie z historycznego pola instancji `.version`.
