"""Explicit lighting-preference model and decision composition for Stage 07.

The preference model is deliberately small and inspectable.  It consumes only durable,
explicit ManualFeedbackJournal facts; it does not learn occupancy, replay automation
persistence, weak acceptance, or EpisodeEvaluator outcomes as if they were user
preferences.  Historical policy remains a bootstrap/fallback and therefore cannot
outvote an explicit preference merely by having more old samples.

This module never creates ActionIntent and never calls Executor/Home Assistant.  The
Engine owns decision composition and Executor remains the only physical dispatch path.
"""
from __future__ import annotations

import json
import math

from settings import OPTIONS
import teaching as teaching_module


PREFERENCE_CONTRACT_VERSION = 1
PREFERENCE_MODEL_VERSION = 1
ACTION_LABEL_WEIGHT = 1.0
ACTION_RATING_WEIGHT = 0.5
NEAREST_CONTEXT_MARGIN = 0.015
ACTIVE_FEEDBACK_STATUSES = {"recorded", "applied", "learning_queued", "rebuild_queued"}
GENERALIZED_SCOPES = {"similar_context", "persistent_preference"}


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nearest_action(actions, value):
    value = _finite(value)
    if value is None or not actions:
        return None
    return min(range(len(actions)), key=lambda idx: abs(float(actions[idx]) - value))


class LightingPreferenceModel:
    """Nearest-context supervised preference scorer for light power.

    One journal feedback_id is one independent item of preference evidence.  A fact may
    contain both a positive action label (``correct_action``) and a negative action rating
    (``rejected_action``); those are two optimization terms but still one independent
    calibration/evidence item.  Re-evaluating the same fact never increments evidence.
    """

    CONTRACT_VERSION = PREFERENCE_CONTRACT_VERSION
    MODEL_VERSION = PREFERENCE_MODEL_VERSION

    def __init__(self, store):
        self.store = store

    @staticmethod
    def supports(agent):
        return (
            str((agent or {}).get("target_entity") or "").split(".", 1)[0] == "light"
            and str((agent or {}).get("target_property") or "") == "power"
        )

    def _rows(self, agent_id):
        """Return durable active facts for a live/root agent without reinterpretation."""
        with self.store.conn() as c:
            table = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_feedback_journal'"
            ).fetchone()
            if not table:
                return []
            rows = c.execute(
                """SELECT * FROM manual_feedback_journal
                   WHERE (agent_id=? OR root_agent_id=?)
                     AND undone_ts IS NULL
                   ORDER BY created_ts,feedback_id""",
                (str(agent_id), str(agent_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _parse_signature(row):
        try:
            value = json.loads(row.get("context_signature_json") or "{}")
        except Exception:
            return None
        return value if isinstance(value, dict) and value else None

    @staticmethod
    def _scope_allowed(row, episode_id=None):
        scope = str(row.get("scope") or "similar_context")
        if scope == "one_time":
            return False
        if scope == "episode":
            return bool(
                episode_id
                and row.get("episode_id")
                and str(row.get("episode_id")) == str(episode_id)
            )
        return scope in GENERALIZED_SCOPES

    def evaluate(self, agent, actions, context_signature, *, episode_id=None):
        """Score preferred actions from explicit facts only.

        A negative-only ``wrong`` rating cannot manufacture a positive label for another
        action.  Therefore an action is selectable only if it has positive label support.
        """
        actions = [float(value) for value in (actions or [])]
        base = {
            "contract_version": self.CONTRACT_VERSION,
            "model_version": self.MODEL_VERSION,
            "domain": "light_power",
            "source": "preference_model",
            "applied": False,
            "action_index": None,
            "action_value": None,
            "independent_evidence_count": 0,
            "calibration_evidence_count": 0,
            "optimization_terms": 0,
            "evidence_ids": [],
            "scores": [],
            "weights": {
                "explicit_action_label": ACTION_LABEL_WEIGHT,
                "explicit_action_rating": ACTION_RATING_WEIGHT,
                "historical_demonstration": 0.0,
                "episode_outcome": 0.0,
                "absence_of_feedback": 0.0,
            },
            "bootstrap_role": "fallback_only",
        }
        if not self.supports(agent):
            return {**base, "reason": "unsupported_domain"}
        if not actions:
            return {**base, "reason": "no_actions"}
        if not isinstance(context_signature, dict) or not context_signature:
            return {**base, "reason": "context_unavailable"}

        matches = []
        for row in self._rows(agent.get("id")):
            if str(row.get("application_status") or "") not in ACTIVE_FEEDBACK_STATUSES:
                continue
            if not self._scope_allowed(row, episode_id=episode_id):
                continue
            previous = self._parse_signature(row)
            if not previous:
                continue
            rms = teaching_module.distance(context_signature, previous)
            if rms is None:
                continue
            matches.append((float(rms), row))

        if not matches:
            return {**base, "reason": "no_matching_explicit_preference"}

        best = min(item[0] for item in matches)
        nearest = [item for item in matches if item[0] <= best + NEAREST_CONTEXT_MARGIN]
        scores = [0.0 for _ in actions]
        positive = [0.0 for _ in actions]
        negative = [0.0 for _ in actions]
        evidence_ids = set()
        optimization_terms = 0

        for _, row in nearest:
            contributed = False
            correct_idx = _nearest_action(actions, row.get("correct_action"))
            if correct_idx is not None:
                scores[correct_idx] += ACTION_LABEL_WEIGHT
                positive[correct_idx] += ACTION_LABEL_WEIGHT
                optimization_terms += 1
                contributed = True
            rejected_idx = _nearest_action(actions, row.get("rejected_action"))
            if rejected_idx is not None:
                scores[rejected_idx] -= ACTION_RATING_WEIGHT
                negative[rejected_idx] += ACTION_RATING_WEIGHT
                optimization_terms += 1
                contributed = True
            if contributed and row.get("feedback_id"):
                evidence_ids.add(str(row["feedback_id"]))

        detail = [
            {
                "index": idx,
                "value": actions[idx],
                "score": scores[idx],
                "positive_label_weight": positive[idx],
                "negative_rating_weight": negative[idx],
            }
            for idx in range(len(actions))
        ]
        independent = len(evidence_ids)
        result = {
            **base,
            "independent_evidence_count": independent,
            "calibration_evidence_count": independent,
            "optimization_terms": optimization_terms,
            "evidence_ids": sorted(evidence_ids),
            "scores": detail,
            "best_context_distance": best,
        }
        eligible = [idx for idx in range(len(actions)) if positive[idx] > 0.0]
        if not eligible:
            return {**result, "reason": "negative_rating_without_positive_action_label"}
        ranked = sorted(eligible, key=lambda idx: (-scores[idx], -positive[idx], idx))
        best_idx = ranked[0]
        if scores[best_idx] <= 0.0:
            return {**result, "reason": "explicit_evidence_does_not_support_an_action"}
        if len(ranked) > 1 and abs(scores[ranked[0]] - scores[ranked[1]]) <= 1e-12:
            return {**result, "reason": "explicit_preferences_tied"}
        return {
            **result,
            "applied": True,
            "action_index": best_idx,
            "action_value": actions[best_idx],
            "reason": "explicit_preference_label",
        }

    def predict(self, agent, policy, states, temporal, timestamp, *, episode_id=None):
        if not self.supports(agent):
            return self.evaluate(agent, getattr(policy, "actions", ()), {}, episode_id=episode_id)
        current = teaching_module.signature(policy, states, temporal, timestamp)
        return self.evaluate(
            agent, getattr(policy, "actions", ()), current or {}, episode_id=episode_id
        )

    def instruction_state(self, teaching_id, timestamp, *, episode_id=None):
        """Resolve scope of a Teaching label linked to the Stage-06 journal.

        Legacy unlinked labels keep their old behaviour.  New similar-context/one-time
        labels are direct instructions only during the existing intent TTL; afterwards
        similar-context evidence belongs to the preference model and one-time evidence is
        not generalized.  Episode labels remain direct only in their episode (or during
        that same immediate TTL when no active episode id is available).
        """
        if not teaching_id:
            return {"active": False, "scope": None, "source": "none"}
        with self.store.conn() as c:
            tables = {
                row[0] for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                    "('manual_feedback_effects','manual_feedback_journal')"
                ).fetchall()
            }
            if tables != {"manual_feedback_effects", "manual_feedback_journal"}:
                return {"active": True, "scope": "legacy", "source": "legacy_unlinked"}
            row = c.execute(
                """SELECT j.* FROM manual_feedback_effects e
                   JOIN manual_feedback_journal j ON j.feedback_id=e.feedback_id
                   WHERE e.ref_type='teaching_label' AND e.ref_id=?
                   ORDER BY e.updated_ts DESC LIMIT 1""",
                (str(teaching_id),),
            ).fetchone()
        if not row:
            return {"active": True, "scope": "legacy", "source": "legacy_unlinked"}
        row = dict(row)
        status = str(row.get("application_status") or "")
        if row.get("undone_ts") is not None or status not in ACTIVE_FEEDBACK_STATUSES:
            return {"active": False, "scope": row.get("scope"), "source": "journal_inactive"}
        scope = str(row.get("scope") or "similar_context")
        selected = float(row.get("selected_ts") or row.get("created_ts") or 0.0)
        immediate_until = selected + max(0.0, float(OPTIONS.get("intent_ttl_seconds", 2)))
        if scope == "persistent_preference":
            active = True
        elif scope == "episode":
            active = bool(
                episode_id
                and row.get("episode_id")
                and str(episode_id) == str(row.get("episode_id"))
            ) or float(timestamp) <= immediate_until
        else:
            # one_time and similar_context are immediate instruction facts only.  The
            # latter continues as statistical preference evidence after the TTL.
            active = float(timestamp) <= immediate_until
        return {
            "active": bool(active),
            "scope": scope,
            "source": "journal_instruction",
            "feedback_id": row.get("feedback_id"),
            "selected_ts": selected,
            "immediate_until": immediate_until,
        }


class PreferenceDecisionComposer:
    """Compose instruction -> explicit preference -> bootstrap -> experiment.

    Deterministic/device constraints are still enforced afterwards by Executor.  The
    composer does not dispatch, mutate policy weights, or turn Shadow into Control.
    """

    CONTRACT_VERSION = 1

    def __init__(self, engine, preference_model):
        self.engine = engine
        self.preference_model = preference_model

    @staticmethod
    def _select_policy_arm(policy, chosen, arms, horizon, action_index, confidence):
        selected = next((arm for arm in arms if int(arm.get("index", -1)) == int(action_index)), None)
        if selected is None:
            return chosen, confidence, chosen.get("support", 0.0), chosen.get("novelty", 1.0)
        updated = dict(chosen)
        updated.update(selected)
        head = policy.heads[int(horizon)]
        structural = head.structural_confidence(arms, int(action_index))
        calibration = head.calibration(int(action_index))
        safe_confidence = min(float(structural), float(calibration["ceiling"]), float(confidence))
        updated["structural_confidence"] = structural
        updated["validation_accuracy"] = calibration["accuracy"]
        updated["validation_lower_bound"] = calibration["ceiling"]
        updated["validation_samples"] = calibration["samples"]
        return updated, safe_confidence, float(selected.get("support", 0.0)), float(selected.get("novelty", 1.0))

    def compose(self, *, agent, policy, state_map, temporal, timestamp, features, labels,
                chosen, confidence, arms, horizon, support, novelty, runtime, registry):
        baseline_value = chosen["value"]
        episode_id = runtime.get("active_episode_id") or runtime.get("episode_id")
        teaching = self.engine.teaching.match(agent, policy, state_map, temporal, timestamp)
        instruction = None
        if teaching:
            instruction = self.preference_model.instruction_state(
                teaching.get("id"), timestamp, episode_id=episode_id
            )
            if not instruction.get("active"):
                teaching = None

        preference = None
        trial = None
        source = "historical_policy_bootstrap"
        if teaching:
            idx = _nearest_action(policy.actions, teaching.get("desired"))
            if idx is not None:
                chosen, confidence, support, novelty = self._select_policy_arm(
                    policy, chosen, arms, horizon, idx, confidence
                )
                chosen = dict(chosen, value=float(teaching["desired"]), index=idx)
                source = "scoped_instruction:" + str((instruction or {}).get("scope") or "legacy")
        else:
            preference = self.preference_model.predict(
                agent, policy, state_map, temporal, timestamp, episode_id=episode_id
            )
            if preference.get("applied"):
                idx = int(preference["action_index"])
                chosen, confidence, support, novelty = self._select_policy_arm(
                    policy, chosen, arms, horizon, idx, confidence
                )
                chosen = dict(chosen, value=float(preference["action_value"]), index=idx)
                source = "preference_model"
            else:
                trial = self.engine.experiments.propose(
                    agent, policy, state_map, registry, features, labels, chosen,
                    confidence, arms, horizon, runtime,
                )
                if trial:
                    chosen = dict(chosen, value=trial["value"], index=trial["index"])
                    support, novelty = trial["support"], trial["novelty"]
                    source = "experiment"

        return {
            "chosen": chosen,
            "confidence": confidence,
            "arms": arms,
            "horizon": horizon,
            "support": support,
            "novelty": novelty,
            "baseline_value": baseline_value,
            "teaching": teaching,
            "instruction": instruction,
            "preference": preference,
            "trial": trial,
            "source": source,
        }
