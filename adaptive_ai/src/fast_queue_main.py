"""HTTP-first entrypoint; runtime extensions load in the background in dependency order."""
import queue_main as queued_runtime
from manual_feedback import install as install_manual_feedback
from manual_feedback_static import install as install_manual_feedback_static

core = queued_runtime.core

# Final entrypoints may add cross-cutting observers at dependency-sensitive composition
# points. Hooks are explicit instance transforms, not module-function monkey patches.
_ENGINE_EXTENSION_HOOKS = {
    "after_fast_light": {},
    "after_candidate_shadow_context": {},
}


def set_engine_extension_hook(stage, name, callback):
    if stage not in _ENGINE_EXTENSION_HOOKS:
        raise ValueError(f"Unknown engine extension hook stage: {stage}")
    hooks = _ENGINE_EXTENSION_HOOKS[stage]
    if callback is None:
        hooks.pop(str(name), None)
    else:
        hooks[str(name)] = callback


def _apply_engine_extension_hooks(stage, value):
    for name, callback in tuple(_ENGINE_EXTENSION_HOOKS[stage].items()):
        updated = callback(value)
        if updated is None:
            raise RuntimeError(f"Engine extension hook {stage}:{name} returned None")
        value = updated
    return value


def prepare_runtime_extensions():
    from agent_candidates import install_store_overlay
    install_store_overlay(core.STORE)
    from device_targets import install as install_device_targets
    install_device_targets()
    from manual_context_learning import install as install_manual_context_learning
    install_manual_context_learning(core)


def prepare_engine_extensions():
    from fast_runtime import install as install_fast_runtime
    from paused_shadow_inference import install as install_paused_shadow_inference
    from fast_local_primary import install as install_fast_local_primary
    from manual_feedback import install_runtime_physical_equivalence
    from manual_feedback_lifecycle import install_runtime as install_lifecycle
    from teaching_learning_bridge import install as install_teaching_learning_bridge
    from manual_feedback_rl import JournaledRLTeaching
    from context_tournament import install as install_context_tournament
    from context_tournament_metrics import install_metrics as install_context_tournament_metrics
    from fast_light_objective_runtime import install as install_fast_light_objective
    from context_tournament_hysteresis import install as install_context_tournament_hysteresis
    from context_tournament_promotion import install_promotion as install_context_tournament_promotion
    from context_tournament_primary_protection import install_primary_protection
    from context_tournament_quality import install_sensor_quality
    from context_tournament_policy_candidate import install_policy_candidates
    from context_schema_history import install_schema_history
    from context_tournament_requalification import install_promotion_shadow_requalification
    from context_schema_probation import install_schema_probation
    from teach_rl_rebenchmark import install_teach_rl_rebenchmark
    from control_diagnostics import install_control_diagnostics
    from context_ui_diagnostics import install_context_ui_diagnostics
    from context_tournament_events import install_context_events

    changed = install_fast_runtime(core)
    if changed:
        core.STORE.event(None, "info", "fast_runtime_migration",
                         f"Realtime timing applied to {len(changed)} fast agent(s)", {"agents": changed})
    install_paused_shadow_inference(core)
    install_fast_local_primary(core.STORE, core.ENGINE)
    install_runtime_physical_equivalence(core, core.ENGINE)
    install_lifecycle(core)
    install_teaching_learning_bridge(core)
    core.ENGINE.rl_teaching = JournaledRLTeaching(core.STORE, core.ENGINE)

    tournament = install_context_tournament(core.STORE, core.ENGINE)
    install_context_tournament_metrics(tournament)
    install_fast_light_objective(tournament)
    tournament = _apply_engine_extension_hooks("after_fast_light", tournament)
    install_context_tournament_hysteresis()
    install_context_tournament_promotion(tournament)
    install_primary_protection(tournament)
    install_sensor_quality(tournament)
    install_policy_candidates(tournament)
    install_schema_history(tournament)
    install_promotion_shadow_requalification(tournament)
    install_schema_probation(tournament)
    install_teach_rl_rebenchmark(core.STORE, core.ENGINE, core.ENGINE.rl_teaching)
    install_control_diagnostics(core, tournament)
    install_context_ui_diagnostics(tournament)
    install_context_events(tournament)

    # Candidate generations are installed last. Stage 06 opts into the explicit listener
    # contract below, so AgentCandidateManager does not install its legacy per-instance
    # teach/add_label wrappers. This preserves the same user capabilities without a second
    # monkey-patched feedback path.
    from agent_candidates import install as install_agent_candidates
    from agent_candidate_manual_rebuild import install as install_candidate_manual_rebuild
    from agent_candidate_config_guard import install as install_candidate_config_guard
    from agent_candidate_balance import install as install_candidate_balance
    from agent_candidate_debounce import install as install_candidate_debounce
    from agent_candidate_teach_status import install as install_candidate_teach_status
    from agent_candidate_lifecycle_hardening import install as install_candidate_lifecycle_hardening
    from agent_candidate_conservative_correct import install as install_candidate_conservative_correct
    from agent_candidate_lineage import install as install_candidate_lineage
    from agent_candidate_lineage_retention import install as install_candidate_lineage_retention
    from agent_candidate_lineage_guards import install as install_candidate_lineage_guards
    from agent_candidate_shadow_runtime import install as install_candidate_shadow_runtime
    from agent_candidate_shadow_context import install as install_candidate_shadow_context
    from agent_candidate_atomic_promote import install as install_candidate_atomic_promote
    from agent_candidate_user_promotion import install as install_candidate_user_promotion
    from agent_candidate_blocked_shadow_evidence import install as install_candidate_blocked_shadow_evidence

    core.ENGINE.teaching._candidate_feedback_hook = True
    core.ENGINE.rl_teaching._candidate_feedback_hook = True
    candidates = install_agent_candidates(core, start_worker=False)

    candidates = install_candidate_manual_rebuild(candidates)
    candidates = install_candidate_config_guard(candidates)
    candidates = install_candidate_balance(candidates)
    candidates = install_candidate_debounce(candidates)
    candidates = install_candidate_teach_status(core, candidates)
    candidates = install_candidate_lifecycle_hardening(candidates)
    candidates = install_candidate_conservative_correct(candidates)
    candidates = install_candidate_lineage(candidates)
    candidates = install_candidate_lineage_retention(candidates)
    candidates = install_candidate_lineage_guards(candidates)
    candidates = install_candidate_shadow_runtime(candidates)
    candidates = install_candidate_shadow_context(candidates)
    candidates = _apply_engine_extension_hooks("after_candidate_shadow_context", candidates)
    candidates = install_candidate_atomic_promote(candidates)
    candidates = install_candidate_user_promotion(candidates)
    candidates = install_candidate_blocked_shadow_evidence(candidates)

    # One listener contract owns all Teaching/Teach-RL -> Candidate transitions. Adding a
    # correction keeps the conservative direct-parent Candidate path. Undo is different:
    # it must remove already-baked influence, so it always requests a full historical
    # rebuild child. The Live parent remains immutable until ordinary atomic promotion.
    journal = getattr(core.ENGINE, "manual_feedback_journal", None)

    def _mark_candidate(result, status):
        if journal is None or not isinstance(result, dict) or not result.get("feedback_id"):
            return
        child_gid = None
        if isinstance(status, dict):
            child_gid = status.get("generation_id") or status.get("child_generation_id")
        row = journal.set_status(
            result["feedback_id"], "learning_queued",
            learning_effect={
                "candidate_queued": True,
                "candidate_generation_id": child_gid,
                "candidate_reason": (status or {}).get("reason") if isinstance(status, dict) else None,
            },
        )
        result["feedback"] = row
        result["ui_message"] = journal.ui_summary(row)

    def _teaching_feedback(event, agent, result):
        reason = "manual_feedback_undo_rebuild" if event == "teaching_undone" else "wrong_decision"
        status = candidates.enqueue(agent["id"], reason)
        _mark_candidate(result, status)
        return status

    def _teach_rl_feedback(event, agent, result):
        reason = "manual_feedback_undo_rebuild" if event == "teach_rl_undone" else "teach"
        status = candidates.enqueue(agent["id"], reason)
        _mark_candidate(result, status)
        return status

    core.ENGINE.teaching.candidate_feedback_listener = _teaching_feedback
    core.ENGINE.rl_teaching.candidate_feedback_listener = _teach_rl_feedback

    candidates.start()
    core.STORE.event(
        None, "info", "manual_feedback_ready",
        "Manual correction feedback path ready",
        {"candidate_feedback": "explicit_listener", "undo": "full_rebuild_candidate"},
    )
    core.STORE.event(None, "info", "teach_rl_ready", "Historical Teach RL pipeline ready", None)
    core.STORE.event(
        None, "info", "context_tournament_ready",
        "Sensor Tournament screens broad context then validates the exact deployable policy on future predictive gain",
        {
            "challenger_count": int(core.OPTIONS.get("context_challenger_count", 4)) if hasattr(core, "OPTIONS") else 4,
            "enabled": bool(core.OPTIONS.get("context_tournament_enabled", True)) if hasattr(core, "OPTIONS") else True,
            "min_samples": int(core.OPTIONS.get("context_tournament_min_samples", 40)) if hasattr(core, "OPTIONS") else 40,
            "min_days": float(core.OPTIONS.get("context_tournament_min_days", 3)) if hasattr(core, "OPTIONS") else 3,
            "min_gain": float(core.OPTIONS.get("context_tournament_min_gain", 0.03)) if hasattr(core, "OPTIONS") else 0.03,
            "primary_replacement_gain": float(core.OPTIONS.get("context_primary_replacement_gain", 0.07)) if hasattr(core, "OPTIONS") else 0.07,
            "hysteresis": "challenger_score > baseline_score + required_predictive_gain",
            "sensor_quality": "availability plus observed health and own-action leakage gate",
            "ranking": "historical+semantic screening only; promotion uses paired future predictive_gain",
            "observed_vs_active": "broad observed pool is separate from compact active feature schema",
            "semantic_groups": "value+quality+lags+trend+time_since_edge+selected_interactions",
            "targeted_sensor": "research priority only; never promotion evidence",
            "multiple_testing": "explicit gain penalty based on screened hypothesis count",
            "feature_cost": "explicit gain penalty for schema/interactions and event frequency",
            "redundancy": "contemporaneous duplicate sensor correlation gate",
            "own_action_leakage": "post-command sensor effects cannot justify promotion",
            "policy_candidate_contract": getattr(tournament, "policy_candidate_contract", None),
            "policy_candidate_training": "predict -> paired score -> candidate learn",
            "policy_candidate_deployment": "the exact trained target-schema policy is promoted; no zero-weight column migration",
            "result_name": "predictive_gain",
            "causal_claim": False,
            "promotion_requalification": "control_to_shadow",
            "schema_probation_samples": int(core.OPTIONS.get("context_schema_probation_samples", 50)) if hasattr(core, "OPTIONS") else 50,
            "schema_rollback_margin": 0.03,
            "schema_rollback_min_samples": 30,
            "teach_rl_control_rebenchmark": "prequential_shadow",
            "paused_shadow_inference": "existing_model_only",
            "fast_primary_anchor": "automation_first_then_sensor_tournament",
            "fast_light_objective": "automation_residual_timing",
            "fast_light_tournament_metric": "timing_utility_with_balanced_accuracy_safety",
            "agent_candidates": bool(candidates),
            "candidate_build": "isolated_hidden_surrogate",
            "candidate_manual_rebuild": getattr(candidates, "candidate_manual_rebuild_contract", "legacy"),
            "candidate_correct": getattr(candidates, "candidate_correct_contract", "legacy"),
            "candidate_offline_gate": getattr(candidates, "candidate_offline_gate_contract", "legacy"),
            "candidate_offline_gate_observation": getattr(candidates, "candidate_offline_gate_observation_contract", "legacy"),
            "candidate_custom_promotion": getattr(candidates, "candidate_custom_promotion_contract", "legacy"),
            "candidate_lineage": getattr(candidates, "candidate_lineage_contract", "legacy"),
            "candidate_model_retention": getattr(candidates, "candidate_model_retention", None),
            "candidate_shadow": getattr(candidates, "candidate_shadow_contract", "legacy"),
            "candidate_shadow_context": getattr(candidates, "candidate_shadow_context_contract", "legacy"),
            "candidate_decision_history": getattr(candidates, "candidate_decision_history_contract", "legacy"),
            "candidate_pair_contract": getattr(candidates, "candidate_pair_contract", "legacy"),
            "candidate_feedback_debounce_seconds": getattr(candidates, "candidate_feedback_debounce_seconds", 15.0),
            "candidate_config_contract": getattr(candidates, "candidate_config_contract", "policy_config_must_match_live_at_build_and_promote"),
            "candidate_comparison": "paired_future_direct_parent_vs_child",
            "candidate_promotion": getattr(candidates, "candidate_promotion_contract", "atomic_generation_swap_preserve_live_mode_or_explicit_target"),
            "candidate_control_promotion": getattr(candidates, "candidate_control_promote_contract", "target_lock_preserve_ownership_lease_no_release_reacquire"),
            "candidate_physical_mode": getattr(candidates, "candidate_physical_mode_contract", "candidate_always_shadow_until_committed_promote"),
            "candidate_binary_evidence": "20_future_samples_per_action_standard; user-defined_custom_thresholds_available",
            "control_diagnostics": "schema_revision+schema_age+prequential_samples+feature_tournament_state",
            "context_ui_diagnostics": "active+observed_pool+primary+challengers+evaluation+schema+last_update",
            "context_events": "structured_numeric_no_generated_text",
            "consecutive_wins": int(core.OPTIONS.get("context_tournament_consecutive_wins", 3)) if hasattr(core, "OPTIONS") else 3,
            "evaluation_hours": float(core.OPTIONS.get("context_tournament_evaluation_hours", 24)) if hasattr(core, "OPTIONS") else 24,
            "cooldown_hours": float(core.OPTIONS.get("context_tournament_cooldown_hours", 24)) if hasattr(core, "OPTIONS") else 24,
            "state_table": "context_tournament_state",
            "promotion_table": "context_tournament_promotions",
            "quality_table": "context_tournament_sensor_quality",
            "observed_pool_table": "context_tournament_observed_pool",
            "schema_history_table": "context_schema_history",
            "schema_probation_table": "context_schema_probation",
            "context_event_state_table": "context_tournament_event_state",
            "fast_light_timing_table": "fast_light_timing_metrics",
            "binary_metric": "fast_timing_utility_for_fast_lights; balanced_accuracy_otherwise",
            "binary_safety_metric": "balanced_accuracy",
            "continuous_metric": "normalized_mae",
            "installed": tournament is not None,
        },
    )


core.prepare_runtime_extensions = prepare_runtime_extensions
core.prepare_engine_extensions = prepare_engine_extensions
install_manual_feedback(core, attach_runtime=False)
install_manual_feedback_static(core)


if __name__ == "__main__":
    core.main()
