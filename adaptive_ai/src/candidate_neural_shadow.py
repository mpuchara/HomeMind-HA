"""Stage-4 Candidate Shadow backend selector for supervised Tiny MLP.

A neural backend may replace the Candidate's *observed Shadow prediction* only after:
1) the established Candidate offline gate has passed, and
2) the neutral Ridge-vs-MLP tournament selected tiny_mlp on identical holdout rows.

This module never changes the Root Live model, never creates ActionIntent and never
invokes Executor/Home Assistant.  If a selected neural backend fails at inference, the
Candidate observation is intentionally left as a gap rather than silently mixing Ridge
evidence into the neural A/B stream.
"""
from __future__ import annotations

import json

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

        result = service.predict_persisted(
            agent,
            policy,
            state_map,
            manager.engine.temporal_history,
            timestamp=float(event_ts),
            require_selected=True,
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
        return {
            "reason": "neural_stage4_shadow_only",
            "message": "Tiny MLP Stage 4 Candidate is Shadow-only and cannot be promoted yet",
            "custom_override": "never",
        }

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
            TinyMLPBackend.BACKEND if neural_active else "diagonal_linucb"
        )
        result["candidate_neural_shadow_active"] = neural_active
        result["candidate_neural_physical_authority"] = False
        result["candidate_neural_promotable"] = False if neural_active else None
        if neural_active:
            result["promotable"] = False
            vetoes = list(result.get("promotion_vetoes") or [])
            veto = promotion_veto(record)
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
            if eligible:
                raise ValueError(
                    promotion_veto(record)["message"]
                    + "; neural promotion to Live/Control is not enabled"
                )

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
        "offline_gate_plus_identical_holdout_tournament_selects_shadow_backend_only_no_live_promotion"
    )
    return manager
