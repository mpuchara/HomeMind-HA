"""Hybrid Tiny MLP action selection with Ridge safety authority.

Tiny MLP may choose the proposed action only when the offline tournament selected it
for the exact Ridge training generation.  Ridge remains authoritative for action-space
legality, horizon, context support/novelty and action-specific confidence calibration.
Any missing/stale neural artifact, inference error or failed Ridge guard falls back to
the unchanged Ridge decision.
"""
from __future__ import annotations

import math
import time

from inference_hot_path_metrics import observe_elapsed
from policy_tiny_mlp import TinyMLPBackend
from settings import OPTIONS


class HybridPolicyService:
    CONTRACT_VERSION = 1
    BACKEND = "tiny_mlp_action+ridge_guard"

    def __init__(self, engine):
        self.engine = engine
        self.neural = getattr(engine, "tiny_mlp_shadow", None)

    @property
    def enabled(self):
        return bool(OPTIONS.get("hybrid_policy_enabled", True)) and self.neural is not None

    @staticmethod
    def _source_revision(policy):
        return str(
            getattr(policy, "tournament_revision", None)
            or getattr(policy, "model_revision", None)
            or "unknown"
        )

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            out = float(value)
        except (TypeError, ValueError):
            return float(default)
        return out if math.isfinite(out) else float(default)

    def evaluate(
        self,
        agent,
        policy,
        state_map,
        temporal,
        *,
        timestamp,
        ridge_chosen,
        ridge_confidence,
        ridge_arms,
        ridge_horizon,
        ridge_support,
        ridge_novelty,
        metric_prefix="",
    ):
        base = {
            "evaluated": True,
            "applied": False,
            "backend": self.BACKEND,
            "reason": None,
            "agreement": None,
            "ridge_action_index": int(ridge_chosen.get("index", 0)),
            "ridge_action_value": float(ridge_chosen.get("value", 0.0)),
            "ridge_confidence": float(ridge_confidence),
            "ridge_support": float(ridge_support),
            "ridge_novelty": float(ridge_novelty),
            "mlp_action_index": None,
            "mlp_action_value": None,
            "mlp_decision_strength": None,
            "dependencies": [],
        }
        if not self.enabled:
            return {**base, "reason": "hybrid_disabled"}

        record = self.neural.persisted_record(agent["id"])
        if not record:
            return {**base, "reason": "neural_artifact_missing"}
        if record.get("selected_backend") != TinyMLPBackend.BACKEND:
            return {**base, "reason": "ridge_selected_by_tournament"}
        tournament = dict(record.get("tournament") or {})
        if not bool(tournament.get("passed")):
            return {**base, "reason": "neural_tournament_not_passed"}
        model = dict(record.get("model") or {})
        if not bool(model.get("trained")):
            return {**base, "reason": "neural_model_untrained"}

        expected_revision = self._source_revision(policy)
        source_revision = str(record.get("source_policy_revision") or "unknown")
        if source_revision != expected_revision:
            return {
                **base,
                "reason": "neural_source_revision_stale",
                "source_policy_revision": source_revision,
                "expected_source_policy_revision": expected_revision,
            }

        try:
            predict_kwargs = {
                "timestamp": float(timestamp),
                "require_selected": True,
            }
            if metric_prefix:
                predict_kwargs["metric_prefix"] = metric_prefix
            result = self.neural.predict_persisted(
                agent,
                policy,
                state_map,
                temporal,
                **predict_kwargs,
            )
        except Exception as exc:
            return {
                **base,
                "reason": "neural_inference_error",
                "error": f"{type(exc).__name__}: {exc}"[:400],
            }
        if not result:
            return {**base, "reason": "neural_prediction_unavailable"}

        guard_started_ns = time.perf_counter_ns()
        guard_stage = str(metric_prefix or "") + "hybrid_ridge_guard"

        def guard_result(value):
            observe_elapsed(self.engine, guard_stage, guard_started_ns)
            return value

        mlp_chosen = dict(result.get("chosen") or {})
        try:
            mlp_index = int(mlp_chosen["index"])
        except (KeyError, TypeError, ValueError):
            return guard_result({**base, "reason": "neural_action_index_invalid"})
        if mlp_index < 0 or mlp_index >= len(policy.actions):
            return guard_result({**base, "reason": "neural_action_outside_ridge_action_space"})

        selected_arm = next(
            (
                dict(arm)
                for arm in ridge_arms
                if int(arm.get("index", -1)) == mlp_index
            ),
            None,
        )
        if selected_arm is None:
            return guard_result({**base, "reason": "ridge_guard_arm_missing"})

        head = policy.heads.get(int(ridge_horizon))
        if head is None:
            return guard_result({**base, "reason": "ridge_guard_head_missing"})

        structural = float(head.structural_confidence(ridge_arms, mlp_index))
        calibration = dict(head.calibration(mlp_index) or {})
        guard_confidence = min(
            structural,
            self._safe_float(calibration.get("ceiling"), 0.0),
        )
        support = self._safe_float(selected_arm.get("support"), 0.0)
        novelty = self._safe_float(selected_arm.get("novelty"), 1.0)
        mlp_strength = self._safe_float(result.get("confidence"), 0.0)

        required_confidence = max(
            self._safe_float(
                OPTIONS.get("hybrid_policy_min_ridge_confidence", 0.60), 0.60
            ),
            self._safe_float(agent.get("confidence_threshold"), 0.0),
        )
        required_support = max(
            self._safe_float(
                OPTIONS.get("hybrid_policy_min_ridge_support", 0.20), 0.20
            ),
            self._safe_float(OPTIONS.get("min_historical_support", 0.20), 0.20),
        )
        max_novelty = min(
            self._safe_float(
                OPTIONS.get("hybrid_policy_max_ridge_novelty", 0.85), 0.85
            ),
            self._safe_float(OPTIONS.get("max_context_novelty", 0.85), 0.85),
        )
        min_mlp_strength = self._safe_float(
            OPTIONS.get("hybrid_policy_min_mlp_decision_strength", 0.55), 0.55
        )

        common = {
            **base,
            "mlp_action_index": mlp_index,
            "mlp_action_value": float(policy.actions[mlp_index]),
            "mlp_decision_strength": mlp_strength,
            "agreement": mlp_index == int(ridge_chosen.get("index", -1)),
            "source_policy_revision": source_revision,
            "mlp_model_revision": str(
                getattr(result.get("backend"), "model_revision", "")
                or model.get("model_revision")
                or ""
            ),
            "mlp_mask_id": str(
                getattr(result.get("backend"), "mask_id", "")
                or (record.get("mask") or {}).get("mask_id")
                or ""
            ),
            "ridge_guard_confidence": guard_confidence,
            "ridge_guard_structural_confidence": structural,
            "ridge_guard_validation_accuracy": self._safe_float(
                calibration.get("accuracy"), 0.0
            ),
            "ridge_guard_validation_lower_bound": self._safe_float(
                calibration.get("ceiling"), 0.0
            ),
            "ridge_guard_validation_samples": int(
                calibration.get("samples") or 0
            ),
            "ridge_guard_support": support,
            "ridge_guard_novelty": novelty,
            "required_ridge_confidence": required_confidence,
            "required_ridge_support": required_support,
            "maximum_ridge_novelty": max_novelty,
            "minimum_mlp_decision_strength": min_mlp_strength,
            "dependencies": list((record.get("mask") or {}).get("selected_entities") or ()),
        }

        failures = []
        if guard_confidence + 1e-12 < required_confidence:
            failures.append("ridge_confidence")
        if support + 1e-12 < required_support:
            failures.append("ridge_support")
        if novelty - 1e-12 > max_novelty:
            failures.append("ridge_novelty")
        if mlp_strength + 1e-12 < min_mlp_strength:
            failures.append("mlp_decision_strength")
        if failures:
            return guard_result({
                **common,
                "reason": "ridge_guard_rejected:" + ",".join(failures),
            })

        chosen = dict(ridge_chosen)
        chosen.update(selected_arm)
        chosen.update(
            {
                "index": mlp_index,
                "value": float(policy.actions[mlp_index]),
                "structural_confidence": structural,
                "validation_accuracy": self._safe_float(
                    calibration.get("accuracy"), 0.0
                ),
                "validation_lower_bound": self._safe_float(
                    calibration.get("ceiling"), 0.0
                ),
                "validation_samples": int(calibration.get("samples") or 0),
                "hybrid_action_source": TinyMLPBackend.BACKEND,
                "mlp_decision_strength": mlp_strength,
            }
        )
        return guard_result({
            **common,
            "applied": True,
            "reason": (
                "mlp_ridge_agreement"
                if common["agreement"]
                else "mlp_action_accepted_by_ridge_guard"
            ),
            "chosen": chosen,
            "confidence": guard_confidence,
            "support": support,
            "novelty": novelty,
            "horizon": int(ridge_horizon),
            "decision_source": "hybrid_tiny_mlp_ridge_guard",
        })

    def diagnostics(self):
        return {
            "contract_version": self.CONTRACT_VERSION,
            "enabled": self.enabled,
            "backend": self.BACKEND,
            "action_selector": TinyMLPBackend.BACKEND,
            "safety_authority": "diagonal_linucb",
            "fallback": "unchanged_ridge_prediction",
            "confidence_source": "ridge_action_specific_calibration",
            "support_source": "ridge_action_specific_support",
            "novelty_source": "ridge_context_novelty",
            "physical_authority": "ridge_guarded_only",
        }


def install(core):
    engine = core.ENGINE
    if engine is None:
        return None
    existing = getattr(engine, "hybrid_policy", None)
    if existing is not None and getattr(engine, "_hybrid_policy_installed", False):
        return existing
    service = HybridPolicyService(engine)
    engine.hybrid_policy = service
    engine._hybrid_policy_installed = True
    core.STORE.event(
        None,
        "info",
        "hybrid_policy_ready",
        "Tiny MLP action selector with Ridge confidence/support/novelty guard enabled",
        service.diagnostics(),
    )
    return service
