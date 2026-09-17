"""Final runtime composition for preference, episode and manual-feedback contracts.

The existing ``fast_queue_main`` stack remains authoritative. Dependency-sensitive
Candidate/Tournament additions use the explicit composition hooks exposed there; no
installer function is monkey-patched.

The final entrypoint must stay cheap enough for Home Assistant Ingress to bind before
runtime composition.  Heavy Candidate/episode/preference modules are therefore imported
inside ``prepare_engine_extensions()``, which main.py invokes from its background runtime
initialization thread after the HTTP server is already listening.
"""
import fast_queue_main as runtime


core = runtime.core
_original_prepare_engine_extensions = core.prepare_engine_extensions


def prepare_engine_extensions():
    # Keep these imports off the pre-HTTP entrypoint path. They initialize substantial
    # Candidate/Trial/feedback code but are only needed once ENGINE and STORE exist.
    from agent_candidate_card_summary import install as install_candidate_card_summary
    from agent_candidate_preference_metrics import install as install_candidate_preference_metrics
    from agent_candidate_promotion_cycle import install as install_candidate_promotion_cycle
    from agent_live_card_refresh import install as install_agent_live_card_refresh
    from episode_evaluator import EpisodeEvaluator
    from episode_evaluator_runtime import (
        install_candidate as install_candidate_episode_evaluator,
        install_core as install_core_episode_evaluator,
        install_tournament as install_tournament_episode_evaluator,
    )
    from manual_feedback_live_isolation import install as install_manual_feedback_live_isolation
    from manual_feedback_unified import UnifiedManualFeedbackJournal
    from manual_feedback_workflow import install as install_manual_feedback_workflow
    from preference_model import LightingPreferenceModel, PreferenceDecisionComposer
    from provenance_runtime import install as install_provenance_runtime
    from observation_contract import install as install_observation_contract

    # Shared episode evaluation is available before Tournament/Candidate workers exist.
    evaluator = getattr(core.ENGINE, "episode_evaluator", None)
    if evaluator is None:
        evaluator = EpisodeEvaluator(core.STORE)
    install_core_episode_evaluator(core, evaluator)

    # Manual feedback is a durable fact before any learning layer consumes it. This
    # migration is additive; legacy labels remain valid/unlinked and are not reinterpreted.
    feedback = getattr(core.ENGINE, "manual_feedback_journal", None)
    if feedback is None:
        feedback = UnifiedManualFeedbackJournal(core.STORE)
        core.ENGINE.manual_feedback_journal = feedback
    if getattr(core.ENGINE, "teaching", None) is not None:
        core.ENGINE.teaching.feedback_journal = feedback

    # Stage 07 is an explicit service, not another process_agent monkey patch. Engine owns
    # the decision-composition contract; the final entrypoint only supplies the light-power
    # preference model backed by Stage-06 durable feedback facts.
    preference = LightingPreferenceModel(core.STORE)
    core.ENGINE.preference_model = preference
    core.ENGINE.decision_composer = PreferenceDecisionComposer(core.ENGINE, preference)

    installed = {"manager": None, "tournament": None}

    def after_fast_light(service):
        service = install_tournament_episode_evaluator(service, evaluator)
        installed["tournament"] = service
        return service

    def after_candidate_shadow_context(manager):
        # Stage-01 preference metrics and stage-05 episode evidence must both precede
        # atomic promotion so status/UI and the committed swap consume identical gates.
        manager = install_candidate_preference_metrics(manager)
        manager = install_candidate_episode_evaluator(manager, evaluator)
        installed["manager"] = manager
        return manager

    runtime.set_engine_extension_hook("after_fast_light", "episode_evaluator", after_fast_light)
    runtime.set_engine_extension_hook(
        "after_candidate_shadow_context", "preference_and_episode", after_candidate_shadow_context
    )
    try:
        _original_prepare_engine_extensions()
    finally:
        runtime.set_engine_extension_hook("after_fast_light", "episode_evaluator", None)
        runtime.set_engine_extension_hook("after_candidate_shadow_context", "preference_and_episode", None)

    # The legacy Engine still contains direct manual-demonstration updates. Suppress only
    # those exact physical-user updates; ordinary physical/environment outcomes remain
    # separate from Stage-07 preference evidence.
    install_manual_feedback_live_isolation(core)

    # Provenance can now resolve decision_id/episode_id for all feedback recorded after
    # runtime readiness. Missing historical links remain NULL/unknown, never fabricated.
    install_provenance_runtime(core)
    install_observation_contract(core)

    manager = installed["manager"] or (
        getattr(core.ENGINE, "agent_candidates", None) if core.ENGINE is not None else None
    )
    if manager is None:
        return

    # Stage 06 does not own the generation-workflow composition order. If that workflow is
    # present, bind Correct/Change decision to the same journal; otherwise Prompt 16 can
    # install the workflow first and this adapter remains a deterministic no-op today.
    manager = install_manual_feedback_workflow(manager)
    manager = install_candidate_card_summary(manager)
    manager = install_candidate_promotion_cycle(manager)
    install_agent_live_card_refresh(core)

    core.STORE.event(
        None, "info", "candidate_preference_metrics_ready",
        "Candidate metrics, episode evaluation, unified feedback and explicit preference model enabled",
        {
            "contract": getattr(manager, "candidate_preference_contract", None),
            "metric": getattr(manager, "candidate_fast_metric", None),
            "half_life_opportunities": getattr(manager, "candidate_preference_half_life_opportunities", None),
            "install_order": "episode_manual_feedback_preference_before_fast_stack;explicit_hooks;candidate_before_atomic_promote",
            "episode_evaluator_contract": 1,
            "episode_domain": "light_power",
            "episode_candidate_contract": getattr(manager, "candidate_episode_contract", None),
            "episode_tournament_contract": (
                getattr(installed.get("tournament"), "_episode_evaluator_installed", False)
            ),
            "episode_action_boundary": "observer_only_no_actionintent_no_executor_dispatch",
            "manual_feedback_contract": 1,
            "manual_feedback_context_contract": 2,
            "manual_feedback_conflicts": "unresolved_conflicts_remain_non_trainable_until_undo",
            "manual_feedback_action_boundary": "journal_only_no_actionintent_no_executor_dispatch",
            "manual_feedback_live_weights": "physical_manual_updates_suppressed_candidate_only",
            "manual_feedback_undo": "retire_labels_and_context_then_full_rebuild_candidate_no_inverse_update",
            "manual_feedback_workflow": getattr(manager, "manual_feedback_workflow_contract", None),
            "preference_contract": preference.CONTRACT_VERSION,
            "preference_model_version": preference.MODEL_VERSION,
            "preference_domain": "light_power",
            "preference_sources": "explicit_action_label+explicit_action_rating_only",
            "preference_bootstrap": "historical_policy_fallback_not_preference_evidence",
            "preference_episode_outcome": "separate_environment_measurement_not_preference_evidence",
            "preference_no_feedback": "no_preference_evidence",
            "preference_decision_order": "executor_constraints_after_scoped_instruction_then_preference_then_bootstrap_or_experiment",
            "preference_action_boundary": "decision_only_no_actionintent_creation_no_executor_dispatch",
            "composition_contract": "engine_decision_composer+fast_queue_named_engine_extension_hooks",
            "card_decisions": getattr(manager, "candidate_card_decision_contract", None),
            "candidate_display": getattr(manager, "candidate_display_contract", None),
            "live_agent_cards": bool(getattr(core, "_agent_live_card_refresh_installed", False)),
            "provenance_contract": 1,
            "provenance_install_order": "after_runtime_composition_before_workers",
            "observation_contract": 1,
            "observation_schema_version": 12,
            "observation_policy_version": 11,
            "observation_install_order": "after_provenance_before_workers",
        },
    )


core.prepare_engine_extensions = prepare_engine_extensions


if __name__ == "__main__":
    core.main()
