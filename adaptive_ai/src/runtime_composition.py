"""Final staged composition root for the shipped Adaptive AI runtime.

This is intentionally an incremental root.  The proven fast/preference stack is invoked as
one base composition step; Stage 11/13/14/15 services and the first Stage-16 contracts are
then attached explicitly.  Stage 17 adds bounded/cursor-based performance services after
their source contracts exist.  Unmigrated legacy overlays stay behind the explicit router
as compatibility fallbacks and can be removed feature-by-feature in later PRs.

The module itself stays deliberately lightweight: stage implementations are imported only
from ``prepare_engine_extensions()`` after main.py has already bound the HTTP server.
"""
from __future__ import annotations

from dataclasses import dataclass
import time


CONTRACT_VERSION = 2
ENTRYPOINT_CHAIN = (
    "run.sh",
    "trial_queue_main.py",
    "preference_queue_main.py",
    "fast_queue_main.py",
    "queue_main.py",
    "main.py",
)


class SystemClock:
    def time(self):
        return time.time()


@dataclass(frozen=True)
class RuntimeDependencies:
    clock: object
    repository: object
    transport: object


class RuntimeCompositionRoot:
    def __init__(self, runtime, *, clock=None):
        self.runtime = runtime
        self.core = runtime.core
        self.clock = clock or SystemClock()
        self.base_prepare_engine_extensions = self.core.prepare_engine_extensions
        self._prepared_engine_ids = set()
        self.dependencies = None
        self.contracts = None

    def _contract_snapshot(self, manager, router):
        engine = self.core.ENGINE
        low_power = getattr(self.core, "LOW_POWER_RUNTIME", None)
        return {
            "version": CONTRACT_VERSION,
            "entrypoint_chain": list(ENTRYPOINT_CHAIN),
            "context": {"owner": "engine.context", "contract": "shared_context_service"},
            "policy": {"owner": "engine.policy+engine.decision_composer", "contract": "policy_decision_before_actionintent"},
            "feedback": {"owner": "engine.manual_feedback_journal", "contract": "durable_feedback_fact_then_candidate_listener", "http": "explicit_named_routes"},
            "episode_evaluation": {"owner": "engine.episode_evaluator", "contract": "observer_only_shared_episode_ids"},
            "candidates": {"owner": "engine.agent_candidates", "contract": "isolated_shadow_generation_manager"},
            "workflow_actions": {
                "contract": getattr(manager, "agent_workflow_contract", None),
                "durable_requests": getattr(manager, "workflow_request_contract", None),
                "explore": getattr(manager, "agent_explore_contract", None),
            },
            "promotion_gates": {
                "owner": "manager.promotion_validation_service",
                "contract": getattr(manager, "promotion_validation_contract", None),
                "source_of_truth": "promotion_validations[]",
            },
            "confidence_calibration": {
                "owner": "manager.confidence_contract+engine.confidence_calibration",
                "contract": getattr(manager, "confidence_contract", None),
                "final_gate": "fixed_future_independent_on_off_paired_non_regression",
                "automation_replay": "screening_only_not_final_calibration_evidence",
            },
            "controlled_adaptation": {
                "owner": "manager.adaptation_service",
                "contract": getattr(manager, "cold_start_drift_contract", None),
                "cold_start": "fallback_or_shadow_no_gate_relaxation",
                "drift": "isolated_candidate_no_live_reset",
                "regression_memory": "zero_weight_cached_offline_replay_guard",
                "recovery_metrics": ["episodes_to_recover", "seconds_to_recover"],
                "promotion": "Stage13_v2_future_holdout_remains_authoritative",
            },
            "device_resources": {
                "owner": "engine.executor.device_agents",
                "contract": engine.executor.device_agents.contract(),
                "identity": "explicit_mapping_then_HA_device_id_then_exact_entity",
                "control_ownership": "durable_precommit_shared_resource_claim",
                "dispatch_guard": "atomic_lease_manual_hold_and_cross_agent_dwell_recheck",
                "perception_configuration": "single_durable_perception_service_owner",
            },
            "execution": {"owner": "engine.executor", "contract": "ActionIntent_to_Executor_only_physical_dispatch"},
            "policy_backend_shadow": {
                "owner": "engine.policy_backend_shadow",
                "contract": "opt_in_observer_only_full_ridge_no_dispatch",
            },
            "performance": {
                "owner": "manager.performance_f22",
                "contract": getattr(manager, "performance_f22_contract", None),
                "semantics": "bounded computation only; raw evidence remains authoritative",
            },
            "low_power": low_power() if callable(low_power) else None,
            "transport": router.descriptor(),
            "dependencies": {
                "clock": type(self.clock).__name__,
                "repository": type(self.core.STORE).__name__ if self.core.STORE is not None else None,
                "transport": type(router).__name__,
            },
            "remaining_legacy_overlays": [
                "queue_main Handler compatibility chain",
                "Candidate process_agent observation wrapper",
                "unmigrated feature GET/static routes",
            ],
        }

    def prepare_engine_extensions(self):
        engine = self.core.ENGINE
        if engine is None:
            return
        key = id(engine)
        if key in self._prepared_engine_ids:
            return

        # Imports remain off the pre-HTTP path; final composition happens in the
        # background runtime-init thread after Ingress is already listening.
        from agent_workflow_actions import install as install_agent_workflow_actions
        from agent_explore import install as install_agent_explore
        from cold_start_drift import install as install_cold_start_drift
        from confidence_contract import install as install_confidence_contract
        from confidence_runtime import install_runtime_semantics
        from device_agents import install_runtime as install_device_agent_runtime
        from performance_f22 import install as install_performance_f22
        from performance_f22_order_guard import install as install_performance_f22_order_guard
        from promotion_validation import install as install_promotion_validation
        from policy_backend_shadow import install_policy_backend_shadow
        from rpi_low_power_runtime import install as install_rpi_low_power_runtime
        from workflow_request_queue import install as install_workflow_request_queue
        from runtime_http import install_dispatch, register_feedback_routes, register_promotion_routes
        from trial_knowledge import install as install_trial_knowledge

        # Existing fast + preference + episode composition is the characterized base.
        self.base_prepare_engine_extensions()
        # Stage 12 remains opt-in and observer-only. Installing the service does not
        # change the production backend; disabled mode performs no inference or learning.
        policy_shadow = install_policy_backend_shadow(engine, self.core.STORE)
        manager = getattr(engine, "agent_candidates", None)
        if manager is None:
            return

        # These are user-facing product capabilities. Install them explicitly in the
        # shipped root rather than depending on a legacy overlay side effect. Both
        # installers are idempotent, so upgrades never stack duplicate HTTP handlers.
        manager = install_agent_workflow_actions(manager)
        manager = install_workflow_request_queue(manager)
        manager = install_agent_explore(manager)

        # RPi resource control changes scheduling only, never learning semantics.
        manager = install_rpi_low_power_runtime(self.core, manager)
        engine.agent_candidates = manager

        # Trial knowledge intentionally wraps generation-aware Explore.
        manager = install_trial_knowledge(manager)
        manager = install_confidence_contract(manager)
        install_runtime_semantics(engine, manager.confidence_probability_journal)
        manager = install_cold_start_drift(manager)
        install_device_agent_runtime(engine)

        manager = install_promotion_validation(manager, clock=self.clock, repository=self.core.STORE)
        manager = install_performance_f22(manager, core=self.core)
        manager = install_performance_f22_order_guard(manager)
        engine.agent_candidates = manager

        router = install_dispatch(self.core)
        register_feedback_routes(router, self.core)
        register_promotion_routes(router, self.core, manager)

        self.dependencies = RuntimeDependencies(clock=self.clock, repository=self.core.STORE, transport=router)
        self.contracts = self._contract_snapshot(manager, router)
        self.core.RUNTIME_DEPENDENCIES = self.dependencies
        self.core.RUNTIME_COMPOSITION_CONTRACT = self.contracts

        device_service = getattr(getattr(engine, "executor", None), "device_agents", None)
        self.core.STORE.event(
            None, "info", "policy_backend_shadow_ready",
            "Full-ridge policy challenger is available as a non-controlling opt-in Shadow",
            {
                "enabled": bool(getattr(policy_shadow, "enabled", False)),
                "mode": "shadow",
                "dispatch_capability": False,
                "default_backend_changed": False,
                "backend": "full_ridge_linucb",
                "decision_observation": "same_live_features_same_allowed_action_set",
                "reward_observation": "executed_action_only",
                "trial_records": "logged_propensity_preserved_exactly_once",
            },
        )
        self.core.STORE.event(
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
        self.core.STORE.event(
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
        self.core.STORE.event(
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
        self.core.STORE.event(
            None, "info", "device_agent_contract_ready",
            "Registry-backed DeviceAgent capabilities and shared-resource arbitration are active",
            {
                "contract": device_service.contract() if device_service is not None else None,
                "install_order": "executor_contract_then_runtime_diagnostics_before_workers",
                "action_boundary": "resource_arbiter_never_dispatches_executor_only",
            },
        )
        self.core.STORE.event(
            None, "info", "performance_f22_ready",
            "Bounded history, diagnostics and training-cost controls are active",
            {
                "contract": getattr(manager, "performance_f22_contract", None),
                "install_order": "after_candidate_confidence_drift_device_contracts_before_workers",
                "raw_evidence_retained": True,
                "action_boundary": "performance_only_no_dispatch",
            },
        )
        self.core.STORE.event(
            None, "info", "runtime_composition_ready",
            "Final runtime composition root and explicit promotion/feedback contracts are active",
            self.contracts,
        )
        self._prepared_engine_ids.add(key)

    def descriptor(self):
        return self.contracts or {
            "version": CONTRACT_VERSION,
            "entrypoint_chain": list(ENTRYPOINT_CHAIN),
            "state": "bound_not_prepared",
        }


def bind_final_composition(runtime, *, clock=None):
    """Bind one final prepare hook; repeated binding never stacks another wrapper."""
    core = runtime.core
    existing = getattr(core, "RUNTIME_COMPOSITION_ROOT", None)
    if existing is not None:
        return existing
    root = RuntimeCompositionRoot(runtime, clock=clock)
    core.RUNTIME_COMPOSITION_ROOT = root
    core.prepare_engine_extensions = root.prepare_engine_extensions
    return root
