"""Candidate Shadow routing for the Tiny MLP + Ridge hybrid policy.

A supervised Tiny MLP may propose the Candidate action only after the established
offline gate and neutral Ridge-vs-MLP tournament pass. Candidate A/B then uses the same
Ridge confidence/support/novelty guard as the promoted Live hybrid. If that guard rejects
the neural proposal, the ordinary Candidate Ridge path remains the observed fallback.

This module never creates ActionIntent and never invokes Executor/Home Assistant.
Offline-RL neural candidates remain Shadow-only until their separate authority contract
is promoted in a later stage.
"""
from __future__ import annotations

import json
import time

from inference_hot_path_metrics import increment_counter, observe_elapsed
from policy_tiny_mlp import TinyMLPBackend


def _json(value, default=None):
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {} if default is None else default
    try:
        return json.loads(value)
    except Exception:
        return {} if default is None else default


def install(manager):
    if getattr(manager, "_candidate_neural_shadow_installed", False):
        return manager

    service = getattr(manager.engine, "tiny_mlp_shadow", None)
    if service is None:
        return manager

    def candidate_row(candidate_id):
        finder = getattr(manager, "_row_by_candidate", None)
        if callable(finder):
            return finder(str(candidate_id))
        with manager.store.conn() as c:
            row = c.execute(
                "SELECT * FROM agent_candidates WHERE candidate_id=?",
                (str(candidate_id),),
            ).fetchone()
        return dict(row) if row else None

    def neural_eligible(candidate_id):
        record = service.persisted_record(candidate_id)
        if not record:
            return False, None, None
        if record.get("selected_backend") != TinyMLPBackend.BACKEND:
            return False, record, None
        model = dict(record.get("model") or {})
        if not bool(model.get("trained")):
            return False, record, None
        row = candidate_row(candidate_id)
        gate = _json((row or {}).get("offline_gate_json"), {})
        if not bool(gate.get("passed")):
            return False, record, gate
        tournament = dict(record.get("tournament") or {})
        if not bool(tournament.get("passed")):
            return False, record, gate
        return True, record, gate

    def predict(generation, state_map, event_ts):
        candidate_id = generation.get("agent_id")
        if not candidate_id:
            return None
        eligible, record, _gate = neural_eligible(candidate_id)
        if not eligible:
            return None

        agent = manager.store.get_agent_config(str(candidate_id))
        if not agent:
            raise RuntimeError("selected neural Candidate agent is unavailable")
        policy = manager.engine.models.get(str(candidate_id))
        if policy is None:
            if manager.store.get_model(str(candidate_id)) is None:
                raise RuntimeError("selected neural Candidate Ridge baseline is unavailable")
            policy = manager.engine.policy(agent)

        temporal_provider = getattr(manager, "candidate_shadow_temporal", None)
        temporal = (
            temporal_provider()
            if callable(temporal_provider) else manager.engine.temporal_history
        )
        home_provider_getter = getattr(manager, "candidate_shadow_home_provider", None)
        home_provider = (
            home_provider_getter() if callable(home_provider_getter) else None
        )

        hybrid = getattr(manager.engine, "hybrid_policy", None)
        if hybrid is not None:
            stage_started_ns = time.perf_counter_ns()
            features, _, _ = policy.features(
                state_map, temporal, at_ts=float(event_ts)
            )
            observe_elapsed(manager.engine, "candidate_feature_construction", stage_started_ns)
            stage_started_ns = time.perf_counter_ns()
            ridge = policy.predict(features)
            observe_elapsed(manager.engine, "candidate_ridge_predict", stage_started_ns)
            (
                ridge_chosen,
                ridge_confidence,
                ridge_arms,
                ridge_horizon,
                ridge_support,
                ridge_novelty,
            ) = ridge
            hybrid_started_ns = time.perf_counter_ns()
            hybrid_kwargs = {
                "timestamp": float(event_ts),
                "ridge_chosen": ridge_chosen,
                "ridge_confidence": ridge_confidence,
                "ridge_arms": ridge_arms,
                "ridge_horizon": ridge_horizon,
                "ridge_support": ridge_support,
                "ridge_novelty": ridge_novelty,
                "metric_prefix": "candidate_",
            }
            if home_provider is not None:
                hybrid_kwargs["home_provider"] = home_provider
            selected = hybrid.evaluate(
                agent,
                policy,
                state_map,
                temporal,
                **hybrid_kwargs,
            )
            observe_elapsed(manager.engine, "candidate_hybrid_policy_total", hybrid_started_ns)
            if not bool((selected or {}).get("applied")):
                increment_counter(manager.engine, "candidate_hybrid_fallbacks")
                # Returning None intentionally delegates to the established Candidate
                # Ridge path, matching the post-promotion Live fallback semantics.
                return None
            chosen = dict(selected["chosen"])
            return {
                "generation_id": generation["generation_id"],
                "agent_id": str(candidate_id),
                "desired": float(chosen["value"]),
                "confidence": float(selected["confidence"]),
                "model_revision": (
                    "hybrid:"
                    + str(getattr(policy, "model_revision", "") or "")
                    + ":"
                    + str(selected.get("mlp_model_revision") or "")
                ),
                "schema_revision": (
                    "hybrid:"
                    + str((getattr(policy, "schema", None) or {}).export().get("version")
                          if getattr(policy, "schema", None) is not None else "")
                    + ":"
                    + str(selected.get("mlp_mask_id") or "")
                ),
                "policy_backend": "tiny_mlp_action+ridge_guard",
                "confidence_kind": "ridge_action_specific_calibration",
            }

        # Compatibility for non-final entrypoints that have not installed the hybrid
        # service yet: retain the previous neural Shadow observation behavior.
        predict_kwargs = {
            "timestamp": float(event_ts),
            "require_selected": True,
            "metric_prefix": "candidate_",
        }
        if home_provider is not None:
            predict_kwargs["home_provider"] = home_provider
        result = service.predict_persisted(
            agent,
            policy,
            state_map,
            temporal,
            **predict_kwargs,
        )
        if result is None:
            raise RuntimeError("selected neural Candidate model is unavailable")
        backend = result["backend"]
        chosen = result["chosen"]
        confidence = result["confidence"]
        return {
            "generation_id": generation["generation_id"],
            "agent_id": str(candidate_id),
            "desired": float(chosen["value"]),
            "confidence": float(confidence),
            "model_revision": str(backend.model_revision),
            "schema_revision": "tiny_mlp:" + str(backend.mask_id),
            "policy_backend": TinyMLPBackend.BACKEND,
            "confidence_kind": chosen.get("confidence_kind"),
        }

    original_status = manager.status
    original_list_status = manager.list_status
    original_promote = manager.promote
    original_promote_custom = getattr(manager, "promote_custom", None)

    def promotion_veto(record):
        tournament = dict((record or {}).get("tournament") or {})
        if str(tournament.get("contract") or "").startswith("offline_rl_"):
            return {
                "reason": "offline_rl_stage7_shadow_only",
                "message": "Offline RL Stage 7 Candidate is Shadow-only and cannot be promoted yet",
                "custom_override": "never",
            }
        return None

    def enrich(result):
        if not isinstance(result, dict):
            return result
        candidate_id = result.get("candidate_id")
        if not candidate_id:
            return result
        record = service.persisted_record(candidate_id)
        if not record:
            return result
        row = candidate_row(candidate_id)
        gate = _json((row or {}).get("offline_gate_json"), {})
        tournament = dict(record.get("tournament") or {})
        neural_active = bool(
            record.get("selected_backend") == TinyMLPBackend.BACKEND
            and tournament.get("passed")
            and gate.get("passed")
            and (record.get("model") or {}).get("trained")
        )
        result["policy_backend_tournament"] = tournament
        result["candidate_policy_backend"] = (
            "tiny_mlp_action+ridge_guard" if neural_active else "diagonal_linucb"
        )
        result["candidate_neural_shadow_active"] = neural_active
        result["candidate_neural_physical_authority"] = False
        result["candidate_hybrid_ridge_guard_ready"] = bool(neural_active)
        veto = promotion_veto(record) if neural_active else None
        result["candidate_neural_promotable"] = (
            bool(result.get("promotable")) if neural_active and veto is None
            else False if neural_active else None
        )
        if veto is not None:
            result["promotable"] = False
            vetoes = list(result.get("promotion_vetoes") or [])
            if not any(
                str(item.get("reason") or "") == str(veto["reason"])
                for item in vetoes if isinstance(item, dict)
            ):
                vetoes.append(veto)
            result["promotion_vetoes"] = vetoes
        return result

    def status(parent_id):
        return enrich(original_status(parent_id))

    def list_status(*args, **kwargs):
        return [enrich(dict(row)) for row in original_list_status(*args, **kwargs)]

    def _assert_not_neural_selected(parent_id):
        row = candidate_row(parent_id)
        if row is None:
            # parent_id is commonly the root/parent rather than the candidate id.
            try:
                status_row = original_status(parent_id) or {}
            except Exception:
                status_row = {}
            candidate_id = status_row.get("candidate_id")
        else:
            candidate_id = row.get("candidate_id")
        if not candidate_id:
            try:
                status_row = original_status(parent_id) or {}
                candidate_id = status_row.get("candidate_id")
            except Exception:
                candidate_id = None
        if candidate_id:
            eligible, record, _gate = neural_eligible(candidate_id)
            veto = promotion_veto(record) if eligible else None
            if veto is not None:
                raise ValueError(veto["message"])

    def promote(parent_id, *args, **kwargs):
        _assert_not_neural_selected(parent_id)
        return original_promote(parent_id, *args, **kwargs)

    def promote_custom(parent_id, *args, **kwargs):
        _assert_not_neural_selected(parent_id)
        if original_promote_custom is None:
            raise ValueError("Custom Candidate promotion is unavailable")
        return original_promote_custom(parent_id, *args, **kwargs)

    manager.candidate_backend_predictor = predict
    manager.status = status
    manager.list_status = list_status
    manager.promote = promote
    if original_promote_custom is not None:
        manager.promote_custom = promote_custom
    manager._candidate_neural_shadow_installed = True
    manager.candidate_neural_shadow_contract = (
        "offline_gate_plus_identical_holdout_tournament_selects_mlp_action_with_ridge_guard_and_ridge_fallback"
    )
    return manager
