"""Frozen chronological holdout helpers for Agent Training vNext Stage 4.

The live Ridge policy keeps its established prequential test-then-learn behavior.
FrozenRidgeSnapshot copies only the prediction coefficients at the train/validation
boundary and never mutates them, so future validation rows cannot leak back into the
frozen score.
"""
import math


def _finite(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def empty_holdout_counts(prior=None):
    prior = prior or {}
    return {
        "samples": int(prior.get("samples") or 0),
        "correct": int(prior.get("correct") or 0),
        "per_action": {
            str(key): {
                "samples": int(value.get("samples") or 0),
                "correct": int(value.get("correct") or 0),
            }
            for key, value in (prior.get("per_action") or {}).items()
        },
    }


def record_holdout_result(stats, actual_idx, correct):
    stats["samples"] = int(stats.get("samples") or 0) + 1
    stats["correct"] = int(stats.get("correct") or 0) + int(bool(correct))
    slot = stats.setdefault("per_action", {}).setdefault(
        str(int(actual_idx)), {"samples": 0, "correct": 0}
    )
    slot["samples"] = int(slot.get("samples") or 0) + 1
    slot["correct"] = int(slot.get("correct") or 0) + int(bool(correct))


def prediction_is_correct(agent, actions, predicted_idx, actual_idx):
    predicted_idx = int(predicted_idx)
    actual_idx = int(actual_idx)
    if len(actions) <= 2 or str(agent.get("target_property")) == "power":
        return predicted_idx == actual_idx
    tolerance = max(
        float(agent.get("deadband") or 0.0),
        (
            float(agent.get("max_value") or 0.0)
            - float(agent.get("min_value") or 0.0)
        ) * 0.03,
    )
    return (
        abs(float(actions[predicted_idx]) - float(actions[actual_idx]))
        <= tolerance
    )


def holdout_summary(agent, actions, stats, minimum_samples=12):
    counts = empty_holdout_counts(stats)
    samples = int(counts["samples"])
    correct = int(counts["correct"])
    per_action_accuracy = {
        str(key): float(value["correct"]) / max(1, int(value["samples"]))
        for key, value in counts["per_action"].items()
        if int(value.get("samples") or 0) > 0
    }
    binary = len(actions) <= 2 or str(agent.get("target_property")) == "power"
    overall_accuracy = (float(correct) / samples) if samples else None
    macro_accuracy = (
        sum(per_action_accuracy.values()) / len(per_action_accuracy)
        if per_action_accuracy else None
    )
    class_coverage = (
        len(per_action_accuracy) >= 2 if binary else bool(per_action_accuracy)
    )
    balanced_accuracy = macro_accuracy if class_coverage else None
    raw_score = balanced_accuracy if binary else overall_accuracy
    enough_samples = samples >= max(1, int(minimum_samples))
    sufficient = bool(enough_samples and class_coverage and raw_score is not None)
    return {
        "contract": "frozen_or_prequential_holdout_v1",
        "samples": samples,
        "correct": correct,
        "overall_accuracy": overall_accuracy,
        "balanced_accuracy": balanced_accuracy,
        "per_action_accuracy": per_action_accuracy,
        "class_coverage": bool(class_coverage),
        "balanced": bool(binary),
        "minimum_samples": max(1, int(minimum_samples)),
        "enough_samples": bool(enough_samples),
        "status": "ok" if sufficient else "insufficient_evidence",
        # Do not expose a gate-ready score when sample/class evidence is incomplete.
        "score": float(raw_score) if sufficient else None,
        "counts": counts,
    }


class FrozenRidgeSnapshot:
    """Immutable mean-policy snapshot copied at the validation boundary."""

    def __init__(self, policy):
        self.actions = tuple(float(value) for value in policy.actions)
        self.dims = int(policy.dims)
        self.heads = {}
        for horizon, head in policy.heads.items():
            exported = head.export()
            self.heads[int(horizon)] = {
                "a": tuple(
                    tuple(_finite(value, 1.0) for value in row)
                    for row in exported.get("a") or ()
                ),
                "b": tuple(
                    tuple(_finite(value, 0.0) for value in row)
                    for row in exported.get("b") or ()
                ),
                "classifier": None,
                "rejection_b": tuple(tuple(row) for row in exported.get("rejection_b") or ()),
            }
            raw_classifier = exported.get("binary_state_classifier")
            if raw_classifier and raw_classifier.get("ready"):
                from binary_state_classifier import BinaryStateClassifier
                self.heads[int(horizon)]["classifier"] = BinaryStateClassifier(self.dims, raw_classifier)

    def predict(self, horizon, features):
        state = self.heads[int(horizon)]
        a_rows = state["a"]
        b_rows = state["b"]
        best_idx = 0
        best_mean = None
        classifier = state.get("classifier")
        state_score = classifier.score(features) if classifier else None
        for action_idx in range(len(self.actions)):
            a = a_rows[action_idx]
            b = b_rows[action_idx]
            mean = 0.0
            for raw_idx, raw_value in features.items():
                idx = int(raw_idx)
                if idx < 0 or idx >= self.dims:
                    continue
                value = _finite(raw_value)
                mean += (
                    _finite(b[idx])
                    / max(_finite(a[idx], 1.0), 1e-9)
                ) * value
            if state_score is not None:
                rejection = state["rejection_b"][action_idx]
                penalty = min(0.0, sum(rejection[int(i)] * _finite(v) / max(a[int(i)], 1e-9)
                                      for i, v in features.items() if 0 <= int(i) < self.dims))
                mean = (state_score if action_idx == 1 else -state_score) + penalty
            if best_mean is None or mean > best_mean:
                best_idx = action_idx
                best_mean = mean
        return int(best_idx)
