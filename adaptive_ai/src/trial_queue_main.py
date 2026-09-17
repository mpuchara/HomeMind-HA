"""Final runtime composition for durable trial knowledge and calibrated confidence semantics.

Runtime path:
    run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py

Stage 11 binds durable TrialRecord knowledge to generation-aware Explore. Stage 13 then
installs the semantic/calibration contract after all Candidate comparison layers exist and
before workers start. Neither layer adds a physical-control path; Executor remains the only
Home Assistant service dispatcher.
"""
import preference_queue_main as runtime
from confidence_contract import install as install_confidence_contract
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
            "action_boundary": "diagnostics_and_promotion_gate_only_no_dispatch",
        },
    )


core.prepare_engine_extensions = prepare_engine_extensions


if __name__ == "__main__":
    core.main()
