"""HTTP-first entrypoint; runtime extensions load in the background in dependency order."""
import queue_main as queued_runtime
from manual_feedback import install as install_manual_feedback
from manual_feedback_static import install as install_manual_feedback_static

core = queued_runtime.core


def prepare_runtime_extensions():
    # The database is assigned by main before this hook. Candidate training surrogates
    # must be hidden from normal runtime enumeration before engine/history are imported.
    from agent_candidates import install_store_overlay
    install_store_overlay(core.STORE)
    # Domain adapters must still precede imports of engine/history/executor, which capture
    # context functions.
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
    from teaching_rl import RLTeaching
    from context_tournament import install as install_context_tournament
    from context_tournament_metrics import install_metrics as install_context_tournament_metrics
    from fast_light_objective_runtime import install as install_fast_light_objective
    from context_tournament_hysteresis import install as install_context_tournament_hysteresis
    from context_tournament_promotion import install_promotion as install_context_tournament_promotion
    from context_tournament_primary_protection import install_primary_protection
    from context_tournament_quality import install_sensor_quality
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
                         f"Realtime timing applied to {len(changed)} fast agent(s)",
                         {"agents": changed})
    # Runtime mode and offline-training state are separate concerns. A paused trained
    # model may infer in Shadow, but this never changes Control qualification.
    install_paused_shadow_inference(core)
    # Fast replay uses primary_occupancy_sensor as a semantic anchor, not merely a
    # diagnostic. Normalize it before Tournament/history consumers see the policy so a
    # remote correlation cannot outrank the current target automation's occupancy input.
    install_fast_local_primary(core.STORE, core.ENGINE)
    install_runtime_physical_equivalence(core, core.ENGINE)
    install_lifecycle(core)
    install_teaching_learning_bridge(core)
    # Historical Teach is a separate offline-RL path.  The existing Teaching bridge is
    # intentionally kept intact because Wrong decision already depends on that behaviour.
    core.ENGINE.rl_teaching = RLTeaching(core.STORE, core.ENGINE)
    tournament = install_context_tournament(core.STORE, core.ENGINE)
    install_context_tournament_metrics(tournament)
    # Fast lights optimize residual timing against the still-running HA automation.
    # Install before promotion so its paired timing evidence is what promotion windows
    # consume; balanced accuracy remains a safety check rather than the ranking objective.
    install_fast_light_objective(tournament)
    # Hysteresis patches only the promotion score boundary; install it before wiring the
    # promotion state machine so every cumulative/window comparison uses strict margin.
    install_context_tournament_hysteresis()
    install_context_tournament_promotion(tournament)
    # Primary feature protection runs after promotion wiring but patches the schema-slot
    # chooser used by that state machine. It cannot create control actions of its own.
    install_primary_protection(tournament)
    # Quality wraps the final replacement chooser and records active/challenger reliability
    # before promotion is evaluated on each event.
    install_sensor_quality(tournament)
    # Schema history sees the exact before/after state of a successful promotion after all
    # hysteresis, primary-protection and quality gates have passed.
    install_schema_history(tournament)
    # Requalification is outermost: only after a real promotion row exists can the affected
    # Control agent be persisted back to Shadow and its previous Control handoff released.
    install_promotion_shadow_requalification(tournament)
    # Probation sits outside promotion/history/requalification. It snapshots the old policy,
    # compares both schemas on future outcomes and can restore only that agent's old model.
    install_schema_probation(tournament)
    # Teach rebenchmark is the outermost process-agent observer at this stage. It invalidates
    # the rebuild benchmark only after supervised Teach fine tuning, then scores future
    # Shadow outcomes before inner online learning sees them. It never enables Control.
    install_teach_rl_rebenchmark(core.STORE, core.ENGINE, core.ENGINE.rl_teaching)
    # Diagnostics are deliberately read-only wrappers around runtime payloads. Executor
    # keeps its direct qualification import and never learns how sensors were picked.
    install_control_diagnostics(core, tournament)
    install_context_ui_diagnostics(tournament)
    # Structured event reporting only deduplicates/logs numerical evidence and normalizes
    # legacy Tournament event names; it cannot affect decisions.
    install_context_events(tournament)
    # Candidate generations are installed last. Their process wrapper observes the final
    # effective Live prediction, runs Candidate inference without ActionIntent/Executor,
    # and scores both policies on the same future target transitions.
    from agent_candidates import install as install_agent_candidates
    candidates = install_agent_candidates(core)
    core.STORE.event(None, "info", "manual_feedback_ready", "Manual correction feedback path ready", None)
    core.STORE.event(None, "info", "teach_rl_ready", "Historical Teach RL pipeline ready", None)
    core.STORE.event(None, "info", "context_tournament_ready",
                     "Sensor Tournament shadow evaluates incremental predictive value and auto-promotes proven sensors",
                     {"challenger_count": int(core.OPTIONS.get("context_challenger_count", 4)) if hasattr(core, "OPTIONS") else 4,
                      "enabled": bool(core.OPTIONS.get("context_tournament_enabled", True)) if hasattr(core, "OPTIONS") else True,
                      "min_samples": int(core.OPTIONS.get("context_tournament_min_samples", 40)) if hasattr(core, "OPTIONS") else 40,
                      "min_days": float(core.OPTIONS.get("context_tournament_min_days", 3)) if hasattr(core, "OPTIONS") else 3,
                      "min_gain": float(core.OPTIONS.get("context_tournament_min_gain", 0.03)) if hasattr(core, "OPTIONS") else 0.03,
                      "primary_replacement_gain": float(core.OPTIONS.get("context_primary_replacement_gain", 0.07)) if hasattr(core, "OPTIONS") else 0.07,
                      "hysteresis": "challenger_score > baseline_score + required_gain",
                      "sensor_quality": "availability",
                      "ranking": "predictive_gain * sensor_quality",
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
                      "candidate_comparison": "paired_future_live_vs_candidate",
                      "candidate_promotion": "manual_to_shadow",
                      "control_diagnostics": "schema_revision+schema_age+prequential_samples+feature_tournament_state",
                      "context_ui_diagnostics": "active+primary+challengers+evaluation+schema+last_update",
                      "context_events": "structured_numeric_no_generated_text",
                      "consecutive_wins": int(core.OPTIONS.get("context_tournament_consecutive_wins", 3)) if hasattr(core, "OPTIONS") else 3,
                      "evaluation_hours": float(core.OPTIONS.get("context_tournament_evaluation_hours", 24)) if hasattr(core, "OPTIONS") else 24,
                      "cooldown_hours": float(core.OPTIONS.get("context_tournament_cooldown_hours", 24)) if hasattr(core, "OPTIONS") else 24,
                      "state_table": "context_tournament_state",
                      "promotion_table": "context_tournament_promotions",
                      "quality_table": "context_tournament_sensor_quality",
                      "schema_history_table": "context_schema_history",
                      "schema_probation_table": "context_schema_probation",
                      "context_event_state_table": "context_tournament_event_state",
                      "fast_light_timing_table": "fast_light_timing_metrics",
                      "binary_metric": "fast_timing_utility_for_fast_lights; balanced_accuracy_otherwise",
                      "binary_safety_metric": "balanced_accuracy",
                      "continuous_metric": "normalized_mae",
                      "installed": tournament is not None})


core.prepare_runtime_extensions = prepare_runtime_extensions
core.prepare_engine_extensions = prepare_engine_extensions
# HTTP routes are cheap and need no database; observers are attached by the hook above.
install_manual_feedback(core, attach_runtime=False)
install_manual_feedback_static(core)


if __name__ == "__main__":
    core.main()
