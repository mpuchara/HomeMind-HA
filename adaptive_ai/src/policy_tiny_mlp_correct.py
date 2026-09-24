"""Stage-5 conservative incremental Manual Correct for trained Tiny MLP policies.

This module contains backend-only math.  It never reads Home Assistant, never creates
ActionIntent and never mutates the source model.  The caller supplies:
- exact as-of correction observations,
- replay rows used only for retaining old behaviour,
- an untouched regression holdout,
- nearby observations used for parent/child agreement checks.

The source Tiny MLP is cloned first.  A failed gate therefore leaves the parent byte-for-
byte unchanged and the Candidate lifecycle can reject/rollback the child safely.
"""
from __future__ import annotations

import math

from policy_tiny_mlp import TinyMLPBackend
from policy_tiny_mlp_training import evaluate_supervised, train_supervised


def clone_backend(parent):
    raw = parent.serialize()
    return TinyMLPBackend.deserialize(
        raw,
        expected_schema_id=parent.schema_id,
        expected_mask_id=parent.mask_id,
        expected_feature_ids=parent.feature_ids,
        expected_actions=parent.actions,
        expected_horizons=parent.horizons,
    )


def nearest_action(actions, value):
    actions = tuple(float(x) for x in actions)
    if not actions:
        raise ValueError("Tiny MLP Correct requires an action space")
    return min(range(len(actions)), key=lambda i: abs(actions[i] - float(value)))


def _prediction_index(model, observation):
    return int(model.predict(observation)[0]["index"])


def _is_correct(agent, actions, predicted_idx, actual_idx):
    if len(actions) <= 2 or str(agent.get("target_property") or "") == "power":
        return int(predicted_idx) == int(actual_idx)
    deadband = max(
        float(agent.get("deadband") or 0.0),
        (float(agent.get("max_value") or 0.0) - float(agent.get("min_value") or 0.0)) * 0.03,
    )
    return abs(float(actions[int(predicted_idx)]) - float(actions[int(actual_idx)])) <= deadband


def correction_fit(model, agent, rows):
    rows = list(rows or ())
    if not rows:
        return {"samples": 0, "correct": 0, "score": None, "details": []}
    details = []
    correct = 0
    for row in rows:
        actual = int(row["action_idx"])
        predicted = _prediction_index(model, row["observation"])
        ok = _is_correct(agent, model.actions, predicted, actual)
        correct += int(ok)
        details.append({
            "label_id": row.get("label_id"),
            "sample_ts": float(row.get("timestamp") or 0.0),
            "desired_index": actual,
            "predicted_index": predicted,
            "correct": bool(ok),
        })
    return {
        "samples": len(rows),
        "correct": int(correct),
        "score": float(correct) / len(rows),
        "details": details,
    }


def parameter_distance(parent, child):
    if parent.architecture != child.architecture:
        raise ValueError("Tiny MLP Correct parent/child architecture mismatch")
    diff_sq = parent_sq = 0.0
    count = 0
    for left, right in zip(parent.weights, child.weights):
        for a, b in zip(left, right):
            av = float(a)
            dv = float(b) - av
            parent_sq += av * av
            diff_sq += dv * dv
            count += 1
    for left, right in zip(parent.biases, child.biases):
        for a, b in zip(left, right):
            av = float(a)
            dv = float(b) - av
            parent_sq += av * av
            diff_sq += dv * dv
            count += 1
    rms = math.sqrt(diff_sq / max(1, count))
    parent_rms = math.sqrt(parent_sq / max(1, count))
    relative = math.sqrt(diff_sq / max(1e-12, parent_sq))
    return {
        "parameters": int(count),
        "rms_delta": float(rms),
        "parent_rms": float(parent_rms),
        "relative_l2": float(relative),
    }


def pair_regression_metrics(parent, child, agent, rows):
    rows = list(rows or ())
    parent_eval = evaluate_supervised(parent, agent, rows)
    child_eval = evaluate_supervised(child, agent, rows)
    agreement = 0
    regressions = 0
    improvements = 0
    for row in rows:
        actual = int(row["action_idx"])
        p = _prediction_index(parent, row["observation"])
        c = _prediction_index(child, row["observation"])
        agreement += int(p == c)
        p_ok = _is_correct(agent, parent.actions, p, actual)
        c_ok = _is_correct(agent, child.actions, c, actual)
        regressions += int(p_ok and not c_ok)
        improvements += int((not p_ok) and c_ok)
    return {
        "samples": len(rows),
        "parent": parent_eval,
        "child": child_eval,
        "parent_child_agreement": (
            float(agreement) / len(rows) if rows else None
        ),
        "regression_count": int(regressions),
        "improvement_count": int(improvements),
        "regression_fraction": (
            float(regressions) / len(rows) if rows else None
        ),
        "score_delta": (
            float(child_eval.get("score") or 0.0) - float(parent_eval.get("score") or 0.0)
            if rows and parent_eval.get("score") is not None and child_eval.get("score") is not None
            else None
        ),
    }


def nearby_agreement(parent, child, observations):
    observations = list(observations or ())
    if not observations:
        return {"samples": 0, "agreement": None}
    same = 0
    for observation in observations:
        same += int(
            _prediction_index(parent, observation)
            == _prediction_index(child, observation)
        )
    return {
        "samples": len(observations),
        "agreement": float(same) / len(observations),
    }


def mixed_training_rows(corrections, replay, *, correction_fraction=0.25, max_samples=256):
    """Build a bounded 20-30% correction / 70-80% replay mixture.

    Duplicating an explicit human label only changes optimization weight.  The caller
    keeps the original correction sample/provenance separately, so duplication cannot
    create fake Correct events.
    """
    corrections = list(corrections or ())
    replay = list(replay or ())
    if not corrections:
        return [], {
            "correction_source_samples": 0,
            "correction_slots": 0,
            "replay_samples": 0,
            "effective_correction_fraction": 0.0,
        }
    fraction = max(0.20, min(0.30, float(correction_fraction)))
    limit = max(len(corrections), int(max_samples))
    # Reserve enough room for every explicit label at least once.
    max_replay = max(0, limit - len(corrections))
    replay = replay[-max_replay:] if max_replay else []

    if replay:
        desired_correction_slots = max(
            len(corrections),
            int(round(len(replay) * fraction / max(1e-9, 1.0 - fraction))),
        )
    else:
        desired_correction_slots = len(corrections)
    correction_slots = min(
        max(len(corrections), desired_correction_slots),
        max(len(corrections), limit - len(replay)),
    )

    weighted = []
    for index in range(correction_slots):
        source = dict(corrections[index % len(corrections)])
        source["source"] = "manual_correct"
        source["weight"] = float(source.get("weight") or 1.0)
        weighted.append(source)
    for row in replay:
        item = dict(row)
        item["source"] = str(item.get("source") or "historical_replay")
        item["weight"] = float(item.get("weight") or 1.0)
        weighted.append(item)

    total = len(weighted)
    return weighted, {
        "correction_source_samples": len(corrections),
        "correction_slots": correction_slots,
        "replay_samples": len(replay),
        "effective_correction_fraction": (
            float(correction_slots) / total if total else 0.0
        ),
        "total_optimizer_rows": total,
    }


def incremental_correct_finetune(
    parent,
    *,
    agent,
    correction_samples,
    replay_samples,
    holdout_samples,
    nearby_observations=(),
    correction_fraction=0.25,
    max_samples=256,
    max_epochs=6,
    batch_size=16,
    learning_rate=0.003,
    l2=0.0001,
    gradient_clip=0.5,
    early_stop_patience=2,
    early_stop_min_delta=0.0005,
    minimum_holdout_samples=12,
    max_accuracy_regression=0.03,
    min_parent_agreement=0.85,
    min_nearby_agreement=0.75,
    max_regression_fraction=0.10,
    max_parent_relative_l2=0.20,
    min_correction_fit=0.95,
    checkpoint=None,
):
    corrections = list(correction_samples or ())
    replay = list(replay_samples or ())
    holdout = list(holdout_samples or ())
    if not corrections:
        raise ValueError("Tiny MLP Correct has no usable explicit correction samples")

    child = clone_backend(parent)
    before = correction_fit(parent, agent, corrections)
    training_rows, mixture = mixed_training_rows(
        corrections,
        replay,
        correction_fraction=correction_fraction,
        max_samples=max_samples,
    )
    trainer = train_supervised(
        child,
        training_rows,
        max_samples=max_samples,
        max_epochs=max_epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        l2=l2,
        gradient_clip=gradient_clip,
        early_stop_patience=early_stop_patience,
        early_stop_min_delta=early_stop_min_delta,
        checkpoint=checkpoint,
    )
    after = correction_fit(child, agent, corrections)
    regression = pair_regression_metrics(parent, child, agent, holdout)
    nearby = nearby_agreement(parent, child, nearby_observations)
    distance = parameter_distance(parent, child)

    reasons = []
    if not trainer.get("trained"):
        reasons.append("fine_tune_did_not_train")
    if after.get("score") is None or float(after["score"]) + 1e-12 < float(min_correction_fit):
        reasons.append("corrected_points_not_fitted")
    if (
        before.get("score") is not None
        and after.get("score") is not None
        and float(after["score"]) + 1e-12 < float(before["score"])
    ):
        reasons.append("corrected_points_regressed")

    enough_holdout = int(regression.get("samples") or 0) >= int(minimum_holdout_samples)
    if not enough_holdout:
        reasons.append("insufficient_unaffected_holdout")
    else:
        parent_eval = regression.get("parent") or {}
        child_eval = regression.get("child") or {}
        if bool(parent_eval.get("balanced")) and (
            not parent_eval.get("class_coverage") or not child_eval.get("class_coverage")
        ):
            reasons.append("unaffected_holdout_class_coverage_insufficient")
        delta = regression.get("score_delta")
        if delta is None or float(delta) < -float(max_accuracy_regression):
            reasons.append("unaffected_holdout_regression")
        agreement = regression.get("parent_child_agreement")
        if agreement is None or float(agreement) < float(min_parent_agreement):
            reasons.append("parent_child_agreement_too_low")
        fraction = regression.get("regression_fraction")
        if fraction is not None and float(fraction) > float(max_regression_fraction):
            reasons.append("regression_count_too_high")

    if int(nearby.get("samples") or 0) > 0:
        if float(nearby.get("agreement") or 0.0) < float(min_nearby_agreement):
            reasons.append("nearby_context_drift")

    if float(distance.get("relative_l2") or 0.0) > float(max_parent_relative_l2):
        reasons.append("parent_distance_limit_exceeded")

    passed = not reasons
    status = "passed" if passed else (
        "insufficient_evidence"
        if any(reason.startswith("insufficient_") or "class_coverage" in reason for reason in reasons)
        else "blocked"
    )
    report = {
        "contract": "tiny_mlp_manual_correct_incremental_v1",
        "mode": "incremental_supervised_finetune",
        "passed": bool(passed),
        "status": status,
        "reasons": reasons,
        "correction_fit_before": before,
        "correction_fit_after": after,
        "mixture": mixture,
        "trainer": trainer,
        "unaffected_holdout": regression,
        "nearby_context": nearby,
        "parent_distance": distance,
        "limits": {
            "minimum_holdout_samples": int(minimum_holdout_samples),
            "max_accuracy_regression": float(max_accuracy_regression),
            "min_parent_agreement": float(min_parent_agreement),
            "min_nearby_agreement": float(min_nearby_agreement),
            "max_regression_fraction": float(max_regression_fraction),
            "max_parent_relative_l2": float(max_parent_relative_l2),
            "min_correction_fit": float(min_correction_fit),
        },
        "parent_model_revision": str(parent.model_revision),
        "candidate_model_revision": str(child.model_revision),
        "parent_model_checksum": parent.serialize().get("model_checksum"),
        # Final child checksum is intentionally added by the lifecycle after this
        # report is embedded in training_meta. Serializing it here would checksum a
        # pre-report payload and expose a stale identity in diagnostics.
        "rollback_semantics": "parent_immutable_child_rejected_on_failed_gate",
        "online_reward_updates": False,
        "physical_authority": False,
    }
    child.training_meta = {
        **dict(child.training_meta or {}),
        "manual_correct": report,
    }
    return child, report
