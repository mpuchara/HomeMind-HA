"""Final runtime composition for durable trials, calibrated confidence and controlled adaptation.

Runtime path:
    run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py

Stage 11 binds durable TrialRecord knowledge to generation-aware Explore. Stage 13 installs
the semantic/calibration contract after all Candidate comparison layers exist. Stage 14
then adds cold-start evidence reporting and drift monitoring. These layers are installed
before workers start and do not add a physical-control path; Executor remains the only
Home Assistant service dispatcher.
"""
import preference_queue_main as runtime
from cold_start_drift import install as install_cold_start_drift
from confidence_contract import install as install_confidence_contract
from confidence_runtime import install_runtime_semantics
from trial_knowledge import install as install_trial_knowledge

core = runtime.core
_original_prepare_engine_extensions = core.prepare_engine_extensions


def prepare_engine_extensions():
    _original_prepare_engine_extensions()
    manager = getattr(core.ENGINE, "agent_candidates", None) if core.ENGINE is not None else None
    if manager is None:
        return
    manager = install_trial_knowledge(manager)
    manager = install_confidence_contract(manager)
    install_runtime_semantics(core.ENGINE, manager.confidence_probability_journal)
    manager = install_cold_start_drift(manager)
    core.ENGINE.agent_candidates = manager
    core.STORE.event(
        None, "info", "trial_knowledge_ready",
        "Versioned TrialRecord knowledge is bound to generation-aware Free Explore",
        {
            "contract": getattr(manager, "trial_knowledge_contract", None),
            "hypothesis_catalog": getattr(manager, "trial_hypothesis_catalog", None),
            "off_policy": getattr(manager, "trial_off_policy_contract", None),
            "rollback": getattr(manager, "trial_rollback_contract", None),
            "install_order": "after_agent_explore_before_workers",
            "action_boundary": "existing_experiments_to_actionintent_to_executor_only",
        },
    )
    core.STORE.event(
        None, "info", "confidence_contract_ready",
        "Confidence semantics and independent future evaluation are active",
        {
            "contract": getattr(manager, "confidence_contract", None),
            "install_order": "after_trial_knowledge_before_workers",
            "live_runtime_semantics": True,
            "probability_calibration_service": "engine.confidence_calibration",
            "action_boundary": "diagnostics_and_promotion_gate_only_no_dispatch",
        },
    )
    core.STORE.event(
        None, "info", "cold_start_drift_ready",
        "Cold-start evidence reporting and controlled drift adaptation are active",
        {
            "contract": getattr(manager, "cold_start_drift_contract", None),
            "install_order": "after_confidence_contract_before_workers",
            "candidate_isolation": True,
            "promotion": "existing_stage13_fixed_future_gate",
            "action_boundary": "monitoring_and_candidate_orchestration_only_no_dispatch",
        },
    )


core.prepare_engine_extensions = prepare_engine_extensions


if __name__ == "__main__":
    core.main()
