"""Stage-7 conservative Offline-RL update for TinyMLP Candidate policies.

This is intentionally a small, CPU-bounded offline policy-improvement step for HomeMind's
small discrete action spaces. It consumes only already-observed trusted Stage-6 reward
experiences and explicit Manual Correct anchors.

Important statistical limitation:
Stage 6 persisted the executed action and trusted reward, but not the full behavior-policy
propensity of the Ridge controller that produced every action. Therefore the evaluation
below reports a *parent-relative logged-action reward proxy*, not unbiased IPS/OPE.

Safety properties:
- no environment interaction and no exploration;
- immutable parent cloned before training;
- bounded epochs/samples/updates;
- reward clipping and confidence/reliability weighting;
- KL regularization to the parent policy;
- hard parent-parameter-distance projection;
- unsupported actions may not gain material probability or become new argmax choices;
- explicit Manual Correct anchors use a stronger supervised gradient than reward;
- untouched chronological holdout is used only for the offline gate.
"""
from __future__ import annotations

from array import array
import math
import random
import time
import uuid

from policy_tiny_mlp_correct import clone_backend, parameter_distance


def _finite(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _bounded(rows, limit):
    rows = list(rows or ())
    limit = max(1, int(limit))
    if len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    step = (len(rows) - 1) / float(limit - 1)
    return [rows[int(round(i * step))] for i in range(limit)]


def _forward_activations(backend, normalized):
    activations = [list(normalized)]
    current = list(normalized)
    last_layer = len(backend.weights) - 1
    for layer_index, (fan_in, fan_out) in enumerate(
        zip(backend.architecture, backend.architecture[1:])
    ):
        weights = backend.weights[layer_index]
        biases = backend.biases[layer_index]
        output = []
        for row in range(int(fan_out)):
            offset = row * int(fan_in)
            value = float(biases[row])
            for column in range(int(fan_in)):
                value += float(weights[offset + column]) * float(current[column])
            output.append(value)
        current = output if layer_index == last_layer else [
            value if value > 0.0 else 0.0 for value in output
        ]
        activations.append(current)
    return activations


def _probabilities(backend, observation):
    normalized = backend.normalized_input(observation)
    logits = backend._forward(normalized, already_normalized=True)
    return backend.softmax(logits)


def _argmax(probabilities):
    if not probabilities:
        return -1
    return min(
        range(len(probabilities)),
        key=lambda idx: (-float(probabilities[idx]), int(idx)),
    )


def _weighted_stats(rows, reward_clip):
    weights = []
    rewards = []
    for row in rows:
        reward = max(-reward_clip, min(reward_clip, _finite(row.get("reward"))))
        weight = max(0.0, _finite(row.get("weight"), 1.0))
        if weight <= 0.0:
            continue
        weights.append(weight)
        rewards.append(reward)
    total = sum(weights)
    if not rewards or total <= 0.0:
        return 0.0, 1.0
    mean = sum(w * r for w, r in zip(weights, rewards)) / total
    variance = sum(w * (r - mean) ** 2 for w, r in zip(weights, rewards)) / total
    return float(mean), max(0.10, math.sqrt(max(0.0, variance)))


def _support_counts(rows, action_count):
    counts = [0] * int(action_count)
    for row in rows:
        try:
            idx = int(row.get("action_idx"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(counts):
            counts[idx] += 1
    return counts


def _project_to_parent(child, parent, maximum_relative_l2):
    maximum = max(0.0, float(maximum_relative_l2))
    distance = parameter_distance(parent, child)
    relative = float(distance.get("relative_l2") or 0.0)
    if relative <= maximum or relative <= 1e-12:
        return distance, False
    scale = maximum / relative
    for layer_index, layer in enumerate(child.weights):
        parent_layer = parent.weights[layer_index]
        for idx in range(len(layer)):
            p = float(parent_layer[idx])
            layer[idx] = p + (float(layer[idx]) - p) * scale
    for layer_index, biases in enumerate(child.biases):
        parent_biases = parent.biases[layer_index]
        for idx in range(len(biases)):
            p = float(parent_biases[idx])
            biases[idx] = p + (float(biases[idx]) - p) * scale
    return parameter_distance(parent, child), True


def _manual_metrics(model, rows):
    rows = list(rows or ())
    if not rows:
        return {"samples": 0, "correct": 0, "score": None, "details": []}
    correct = 0
    details = []
    for row in rows:
        target = int(row.get("action_idx", -1))
        if not 0 <= target < len(model.actions):
            continue
        probs = _probabilities(model, row["observation"])
        chosen = _argmax(probs)
        ok = chosen == target
        correct += int(ok)
        details.append({
            "label_id": row.get("label_id"),
            "target": target,
            "predicted": chosen,
            "target_probability": float(probs[target]),
            "correct": bool(ok),
        })
    samples = len(details)
    return {
        "samples": samples,
        "correct": correct,
        "score": (float(correct) / samples if samples else None),
        "details": details,
    }


def _nearest_context_rate(parent, reference_rows, eval_rows, threshold):
    refs = []
    for row in _bounded(reference_rows, 128):
        try:
            refs.append(parent.normalized_input(row["observation"]))
        except Exception:
            continue
    if not refs:
        return {"samples": 0, "unseen": 0, "rate": None, "threshold": float(threshold)}
    unseen = samples = 0
    for row in eval_rows:
        try:
            vector = parent.normalized_input(row["observation"])
        except Exception:
            continue
        samples += 1
        best = math.inf
        for ref in refs:
            distance = math.sqrt(
                sum((float(a) - float(b)) ** 2 for a, b in zip(vector, ref))
                / max(1, len(vector))
            )
            if distance < best:
                best = distance
        unseen += int(best > float(threshold))
    return {
        "samples": samples,
        "unseen": unseen,
        "rate": (float(unseen) / samples if samples else None),
        "threshold": float(threshold),
    }


def _action_reward_proxy(train_rows, eval_rows, action_count):
    sums = [0.0] * int(action_count)
    weights = [0.0] * int(action_count)
    for row in train_rows:
        idx = int(row.get("action_idx", -1))
        if not 0 <= idx < action_count:
            continue
        weight = max(0.0, _finite(row.get("weight"), 1.0))
        sums[idx] += weight * _finite(row.get("reward"))
        weights[idx] += weight
    means = [
        (sums[idx] / weights[idx] if weights[idx] > 0.0 else None)
        for idx in range(action_count)
    ]
    errors = []
    missing = 0
    for row in eval_rows:
        idx = int(row.get("action_idx", -1))
        if not 0 <= idx < action_count or means[idx] is None:
            missing += 1
            continue
        errors.append(float(means[idx]) - _finite(row.get("reward")))
    return {
        "kind": "per_action_logged_reward_mean_proxy",
        "behavior_propensity_known": False,
        "per_action_reward_mean": {
            str(idx): (None if value is None else float(value))
            for idx, value in enumerate(means)
        },
        "samples": len(errors),
        "missing": int(missing),
        "mae": (
            sum(abs(value) for value in errors) / len(errors)
            if errors else None
        ),
        "rmse": (
            math.sqrt(sum(value * value for value in errors) / len(errors))
            if errors else None
        ),
    }


def offline_policy_metrics(
    parent,
    child,
    rows,
    *,
    train_reference=(),
    supported_actions=(),
    reward_clip=1.0,
    ratio_clip=2.0,
    context_threshold=1.5,
):
    rows = list(rows or ())
    supported = {int(idx) for idx in supported_actions}
    unsupported = set(range(len(parent.actions))) - supported
    weighted_reward = weighted_child_reward = 0.0
    total_weight = child_weight = ratio_sq = 0.0
    agreements = 0
    tv_sum = 0.0
    max_tv = 0.0
    unsupported_lift = 0.0
    unsupported_argmax = 0
    logged_probability_regressions = 0
    baseline, _spread = _weighted_stats(rows, reward_clip)

    for row in rows:
        idx = int(row.get("action_idx", -1))
        if not 0 <= idx < len(parent.actions):
            continue
        weight = max(0.0, _finite(row.get("weight"), 1.0))
        if weight <= 0.0:
            continue
        reward = max(-reward_clip, min(reward_clip, _finite(row.get("reward"))))
        parent_probs = _probabilities(parent, row["observation"])
        child_probs = _probabilities(child, row["observation"])
        parent_action = _argmax(parent_probs)
        child_action = _argmax(child_probs)
        agreements += int(parent_action == child_action)
        tv = 0.5 * sum(
            abs(float(a) - float(b))
            for a, b in zip(parent_probs, child_probs)
        )
        tv_sum += tv
        max_tv = max(max_tv, tv)
        if unsupported:
            unsupported_lift = max(
                unsupported_lift,
                max(
                    float(child_probs[action]) - float(parent_probs[action])
                    for action in unsupported
                ),
            )
            if (
                child_action in unsupported
                and child_action != parent_action
            ):
                unsupported_argmax += 1

        parent_logged = max(1e-6, float(parent_probs[idx]))
        ratio = max(
            1.0 / max(1.0, float(ratio_clip)),
            min(float(ratio_clip), float(child_probs[idx]) / parent_logged),
        )
        weighted_reward += weight * reward
        total_weight += weight
        weighted_child_reward += weight * ratio * reward
        child_weight += weight * ratio
        ratio_sq += (weight * ratio) ** 2

        # A high-reward logged action should not lose material probability; a low-reward
        # action should not gain it. This is a regression signal, not counterfactual truth.
        delta = float(child_probs[idx]) - float(parent_probs[idx])
        if reward > baseline + 1e-9 and delta < -0.02:
            logged_probability_regressions += 1
        elif reward < baseline - 1e-9 and delta > 0.02:
            logged_probability_regressions += 1

    samples = sum(
        1 for row in rows
        if 0 <= int(row.get("action_idx", -1)) < len(parent.actions)
        and max(0.0, _finite(row.get("weight"), 1.0)) > 0.0
    )
    parent_reward = weighted_reward / total_weight if total_weight else None
    child_reward = (
        weighted_child_reward / child_weight if child_weight else None
    )
    improvement = (
        child_reward - parent_reward
        if child_reward is not None and parent_reward is not None else None
    )
    ess = (
        (child_weight * child_weight) / ratio_sq
        if child_weight > 0.0 and ratio_sq > 0.0 else 0.0
    )
    return {
        "contract": "offline_logged_action_reward_proxy_v1",
        "behavior_propensity_known": False,
        "samples": int(samples),
        "average_trusted_reward": parent_reward,
        "parent_reward_proxy": parent_reward,
        "candidate_reward_proxy": child_reward,
        "reward_improvement_estimate": improvement,
        "effective_sample_size_proxy": float(ess),
        "parent_action_agreement": (
            float(agreements) / samples if samples else None
        ),
        "action_drift_mean_tv": (
            float(tv_sum) / samples if samples else None
        ),
        "action_drift_max_tv": float(max_tv),
        "unsupported_probability_lift_max": max(0.0, float(unsupported_lift)),
        "unsupported_new_argmax_count": int(unsupported_argmax),
        "logged_probability_regression_count": int(logged_probability_regressions),
        "logged_probability_regression_fraction": (
            float(logged_probability_regressions) / samples if samples else None
        ),
        "unseen_context": _nearest_context_rate(
            parent, train_reference, rows, context_threshold
        ),
        "q_proxy_calibration": _action_reward_proxy(
            train_reference, rows, len(parent.actions)
        ),
    }


def _training_score(parent, child, rows, supported_actions, reward_clip):
    metrics = offline_policy_metrics(
        parent,
        child,
        rows,
        train_reference=rows,
        supported_actions=supported_actions,
        reward_clip=reward_clip,
    )
    reward = metrics.get("candidate_reward_proxy")
    drift = metrics.get("action_drift_mean_tv")
    if reward is None:
        return -math.inf, metrics
    return float(reward) - 0.25 * float(drift or 0.0), metrics


def train_conservative_offline_rl(
    parent,
    samples,
    *,
    manual_samples=(),
    max_samples=512,
    max_epochs=6,
    batch_size=16,
    learning_rate=0.0015,
    reward_clip=1.0,
    advantage_clip=1.5,
    kl_beta=2.0,
    parent_l2=0.002,
    manual_weight=4.0,
    gradient_clip=0.5,
    max_parent_relative_l2=0.08,
    min_action_support=4,
    early_stop_patience=2,
    early_stop_min_delta=1e-4,
    checkpoint=None,
):
    """Clone and conservatively improve a TinyMLP from trusted offline rewards only."""
    rows = []
    for row in _bounded(samples, max_samples):
        idx = int(row.get("action_idx", -1))
        weight = max(0.0, _finite(row.get("weight"), 1.0))
        if (
            0 <= idx < len(parent.actions)
            and weight > 0.0
            and row.get("observation")
        ):
            rows.append({
                **dict(row),
                "action_idx": idx,
                "reward": max(
                    -float(reward_clip),
                    min(float(reward_clip), _finite(row.get("reward"))),
                ),
                "weight": weight,
            })
    manual = [
        dict(row) for row in manual_samples or ()
        if 0 <= int(row.get("action_idx", -1)) < len(parent.actions)
        and row.get("observation")
    ]
    if not rows:
        return clone_backend(parent), {
            "trained": False,
            "reason": "no_trusted_offline_reward_samples",
            "samples": 0,
            "manual_samples": len(manual),
        }

    child = clone_backend(parent)
    support_counts = _support_counts(rows, len(parent.actions))
    supported = {
        idx for idx, count in enumerate(support_counts)
        if count >= max(1, int(min_action_support))
    }
    reward_mean, reward_std = _weighted_stats(rows, float(reward_clip))
    prepared = []
    for row in rows:
        advantage = (
            (_finite(row["reward"]) - reward_mean)
            / max(0.10, reward_std)
        )
        advantage = max(
            -float(advantage_clip),
            min(float(advantage_clip), advantage),
        )
        prepared.append({**row, "advantage": float(advantage)})

    max_epochs = max(1, min(24, int(max_epochs)))
    batch_size = max(1, min(64, int(batch_size)))
    lr = max(1e-6, min(0.05, float(learning_rate)))
    kl_beta = max(0.0, min(20.0, float(kl_beta)))
    parent_l2 = max(0.0, min(1.0, float(parent_l2)))
    manual_weight = max(1.0, min(20.0, float(manual_weight)))
    clip = max(1e-4, min(10.0, float(gradient_clip)))
    patience = max(1, min(10, int(early_stop_patience)))
    min_delta = max(0.0, float(early_stop_min_delta))

    rng = random.Random(
        int(parent.init_seed)
        ^ int(parent.training_samples)
        ^ len(prepared)
        ^ 0x7A17
    )
    order = list(range(len(prepared)))
    best_score = -math.inf
    best_epoch = 0
    best_weights = None
    best_biases = None
    stale = 0
    updates = 0
    clip_events = 0
    projection_events = 0
    epoch_scores = []
    started = time.perf_counter()

    def accumulate(sample, grad_w, grad_b, *, manual_row=False):
        normalized = child.normalized_input(sample["observation"])
        activations = _forward_activations(child, normalized)
        child_probs = child.softmax(activations[-1])
        if manual_row:
            target = int(sample["action_idx"])
            scale = manual_weight * max(
                0.0, _finite(sample.get("weight"), 1.0)
            )
            delta = [float(value) for value in child_probs]
            delta[target] -= 1.0
            delta = [value * scale for value in delta]
            effective_weight = scale
        else:
            target = int(sample["action_idx"])
            parent_probs = _probabilities(parent, sample["observation"])
            weight = max(0.0, _finite(sample.get("weight"), 1.0))
            advantage = float(sample.get("advantage") or 0.0)
            delta = [
                weight * (
                    advantage * (
                        float(child_probs[idx]) - (1.0 if idx == target else 0.0)
                    )
                    + kl_beta * (
                        float(child_probs[idx]) - float(parent_probs[idx])
                    )
                )
                for idx in range(len(child_probs))
            ]
            effective_weight = weight * (abs(advantage) + max(0.25, kl_beta))

        for layer_index in range(len(child.weights) - 1, -1, -1):
            fan_in = int(child.architecture[layer_index])
            fan_out = int(child.architecture[layer_index + 1])
            previous = activations[layer_index]
            weights = child.weights[layer_index]
            for row_index in range(fan_out):
                grad_b[layer_index][row_index] += delta[row_index]
                offset = row_index * fan_in
                d = delta[row_index]
                for column in range(fan_in):
                    grad_w[layer_index][offset + column] += d * previous[column]
            if layer_index > 0:
                propagated = [0.0] * fan_in
                for column in range(fan_in):
                    value = 0.0
                    for row_index in range(fan_out):
                        value += (
                            float(weights[row_index * fan_in + column])
                            * delta[row_index]
                        )
                    propagated[column] = (
                        value
                        if activations[layer_index][column] > 0.0
                        else 0.0
                    )
                delta = propagated
        return max(1e-9, effective_weight)

    for epoch in range(1, max_epochs + 1):
        rng.shuffle(order)
        for batch_start in range(0, len(order), batch_size):
            indices = order[batch_start:batch_start + batch_size]
            grad_w = [
                array("d", [0.0] * len(layer))
                for layer in child.weights
            ]
            grad_b = [
                array("d", [0.0] * len(layer))
                for layer in child.biases
            ]
            total_weight = 0.0
            for sample_index in indices:
                total_weight += accumulate(
                    prepared[sample_index], grad_w, grad_b
                )
            # Human labels are intentionally stronger and are replayed every update.
            for row in manual:
                total_weight += accumulate(
                    row, grad_w, grad_b, manual_row=True
                )
            if total_weight <= 0.0:
                continue

            inv = 1.0 / total_weight
            norm_sq = 0.0
            for layer_index, layer in enumerate(child.weights):
                parent_layer = parent.weights[layer_index]
                for idx in range(len(layer)):
                    grad = grad_w[layer_index][idx] * inv
                    grad += parent_l2 * (
                        float(layer[idx]) - float(parent_layer[idx])
                    )
                    grad_w[layer_index][idx] = grad
                    norm_sq += grad * grad
                parent_biases = parent.biases[layer_index]
                for idx in range(len(child.biases[layer_index])):
                    grad = grad_b[layer_index][idx] * inv
                    grad += parent_l2 * (
                        float(child.biases[layer_index][idx])
                        - float(parent_biases[idx])
                    )
                    grad_b[layer_index][idx] = grad
                    norm_sq += grad * grad

            norm = math.sqrt(norm_sq)
            clip_scale = min(1.0, clip / max(1e-12, norm))
            clip_events += int(clip_scale < 0.999999)
            for layer_index, layer in enumerate(child.weights):
                for idx in range(len(layer)):
                    layer[idx] = float(layer[idx]) - (
                        lr * grad_w[layer_index][idx] * clip_scale
                    )
                biases = child.biases[layer_index]
                for idx in range(len(biases)):
                    biases[idx] = float(biases[idx]) - (
                        lr * grad_b[layer_index][idx] * clip_scale
                    )
            _distance, projected = _project_to_parent(
                child, parent, max_parent_relative_l2
            )
            projection_events += int(projected)
            updates += 1
            if callable(checkpoint):
                checkpoint("tiny_mlp_offline_rl_batch")

        score, _metrics = _training_score(
            parent, child, prepared, supported, float(reward_clip)
        )
        epoch_scores.append(float(score))
        if score - best_score > min_delta:
            best_score = float(score)
            best_epoch = epoch
            best_weights = [
                array("f", layer) for layer in child.weights
            ]
            best_biases = [
                array("f", layer) for layer in child.biases
            ]
            stale = 0
        else:
            stale += 1
        if callable(checkpoint):
            checkpoint("tiny_mlp_offline_rl_epoch", force=True)
        if stale >= patience:
            break

    if best_weights is not None:
        child.weights = best_weights
        child.biases = best_biases
    child.trained = True
    child.training_samples = int(parent.training_samples) + len(prepared)
    child.model_revision = str(uuid.uuid4())
    elapsed = max(0.0, time.perf_counter() - started)
    distance = parameter_distance(parent, child)
    parent_manual = _manual_metrics(parent, manual)
    child_manual = _manual_metrics(child, manual)
    meta = {
        "contract": "tiny_mlp_conservative_offline_policy_improvement_v1",
        "trained": True,
        "algorithm": "reward_weighted_policy_gradient_with_parent_kl",
        "behavior_propensity_known": False,
        "samples": len(prepared),
        "manual_samples": len(manual),
        "reward_clip": float(reward_clip),
        "reward_mean": float(reward_mean),
        "reward_std": float(reward_std),
        "advantage_clip": float(advantage_clip),
        "action_support_counts": {
            str(idx): int(value)
            for idx, value in enumerate(support_counts)
        },
        "supported_actions": sorted(int(idx) for idx in supported),
        "min_action_support": int(min_action_support),
        "epochs": len(epoch_scores),
        "best_epoch": int(best_epoch),
        "best_score": (
            None if not math.isfinite(best_score) else float(best_score)
        ),
        "epoch_scores": epoch_scores,
        "batch_updates": int(updates),
        "learning_rate": float(lr),
        "kl_beta": float(kl_beta),
        "parent_l2": float(parent_l2),
        "manual_weight": float(manual_weight),
        "gradient_clip": float(clip),
        "gradient_clip_events": int(clip_events),
        "parent_projection_events": int(projection_events),
        "max_parent_relative_l2": float(max_parent_relative_l2),
        "parent_distance": distance,
        "manual_fit_parent": parent_manual,
        "manual_fit_candidate": child_manual,
        "elapsed_seconds": round(elapsed, 4),
        "online_exploration": False,
        "online_reward_updates": False,
        "physical_authority": False,
    }
    child.training_meta = {
        **dict(child.training_meta or {}),
        "offline_rl": meta,
    }
    return child, meta


def offline_rl_gate(
    parent,
    child,
    *,
    train_rows,
    holdout_rows,
    manual_samples=(),
    min_total_samples=24,
    min_holdout_samples=8,
    min_supported_actions=2,
    min_effective_sample_size=4.0,
    min_reward_gain=0.0,
    min_parent_agreement=0.80,
    max_mean_tv=0.10,
    max_max_tv=0.25,
    max_parent_relative_l2=0.08,
    max_unsupported_probability_lift=0.02,
    max_regression_fraction=0.10,
    max_unseen_context_rate=0.75,
    reward_clip=1.0,
    context_threshold=1.5,
):
    train_rows = list(train_rows or ())
    holdout_rows = list(holdout_rows or ())
    manual = list(manual_samples or ())
    support_counts = _support_counts(train_rows, len(parent.actions))
    supported = {
        idx for idx, count in enumerate(support_counts)
        if count > 0
    }
    train_metrics = offline_policy_metrics(
        parent,
        child,
        train_rows,
        train_reference=train_rows,
        supported_actions=supported,
        reward_clip=reward_clip,
        context_threshold=context_threshold,
    )
    holdout_metrics = offline_policy_metrics(
        parent,
        child,
        holdout_rows,
        train_reference=train_rows,
        supported_actions=supported,
        reward_clip=reward_clip,
        context_threshold=context_threshold,
    )
    parent_manual = _manual_metrics(parent, manual)
    child_manual = _manual_metrics(child, manual)
    distance = parameter_distance(parent, child)

    reasons = []
    total = len(train_rows) + len(holdout_rows)
    if total < int(min_total_samples):
        reasons.append("insufficient_trusted_reward_samples")
    if len(holdout_rows) < int(min_holdout_samples):
        reasons.append("insufficient_offline_holdout")
    supported_count = sum(1 for value in support_counts if value > 0)
    if supported_count < min(int(min_supported_actions), len(parent.actions)):
        reasons.append("insufficient_action_support")

    if holdout_rows:
        gain = holdout_metrics.get("reward_improvement_estimate")
        if gain is None or float(gain) < float(min_reward_gain):
            reasons.append("reward_improvement_gate_failed")
        if (
            float(holdout_metrics.get("effective_sample_size_proxy") or 0.0)
            < float(min_effective_sample_size)
        ):
            reasons.append("effective_sample_size_too_low")
        agreement = holdout_metrics.get("parent_action_agreement")
        if agreement is None or float(agreement) < float(min_parent_agreement):
            reasons.append("parent_action_agreement_too_low")
        mean_tv = holdout_metrics.get("action_drift_mean_tv")
        if mean_tv is None or float(mean_tv) > float(max_mean_tv):
            reasons.append("mean_action_drift_too_high")
        if float(holdout_metrics.get("action_drift_max_tv") or 0.0) > float(max_max_tv):
            reasons.append("max_action_drift_too_high")
        if (
            float(holdout_metrics.get("unsupported_probability_lift_max") or 0.0)
            > float(max_unsupported_probability_lift)
        ):
            reasons.append("unsupported_action_probability_lift")
        if int(holdout_metrics.get("unsupported_new_argmax_count") or 0) > 0:
            reasons.append("unsupported_action_became_argmax")
        regression_fraction = holdout_metrics.get(
            "logged_probability_regression_fraction"
        )
        if (
            regression_fraction is not None
            and float(regression_fraction) > float(max_regression_fraction)
        ):
            reasons.append("logged_action_regression_too_high")
        unseen_rate = (
            (holdout_metrics.get("unseen_context") or {}).get("rate")
        )
        if (
            unseen_rate is not None
            and float(unseen_rate) > float(max_unseen_context_rate)
        ):
            reasons.append("unseen_context_rate_too_high")

    if float(distance.get("relative_l2") or 0.0) > float(max_parent_relative_l2):
        reasons.append("parent_distance_limit_exceeded")
    if manual:
        if (
            child_manual.get("score") is None
            or float(child_manual["score"]) + 1e-12
            < float(parent_manual.get("score") or 0.0)
        ):
            reasons.append("manual_correct_regressed")
        if child_manual.get("score") is not None and float(child_manual["score"]) < 1.0:
            reasons.append("manual_correct_anchor_not_preserved")

    insufficient = any(
        reason.startswith("insufficient_")
        or reason == "effective_sample_size_too_low"
        for reason in reasons
    )
    status = (
        "passed" if not reasons
        else ("insufficient_evidence" if insufficient else "offline_blocked")
    )
    return {
        "contract": "tiny_mlp_offline_rl_gate_v1",
        "status": status,
        "passed": not reasons,
        "reasons": reasons,
        "samples_total": int(total),
        "train_samples": len(train_rows),
        "holdout_samples": len(holdout_rows),
        "action_support_counts": {
            str(idx): int(value)
            for idx, value in enumerate(support_counts)
        },
        "supported_actions": sorted(int(idx) for idx in supported),
        "train": train_metrics,
        "holdout": holdout_metrics,
        "manual_fit_parent": parent_manual,
        "manual_fit_candidate": child_manual,
        "parent_distance": distance,
        "limits": {
            "min_total_samples": int(min_total_samples),
            "min_holdout_samples": int(min_holdout_samples),
            "min_supported_actions": int(min_supported_actions),
            "min_effective_sample_size": float(min_effective_sample_size),
            "min_reward_gain": float(min_reward_gain),
            "min_parent_agreement": float(min_parent_agreement),
            "max_mean_tv": float(max_mean_tv),
            "max_max_tv": float(max_max_tv),
            "max_parent_relative_l2": float(max_parent_relative_l2),
            "max_unsupported_probability_lift": float(max_unsupported_probability_lift),
            "max_regression_fraction": float(max_regression_fraction),
            "max_unseen_context_rate": float(max_unseen_context_rate),
            "reward_clip": float(reward_clip),
            "context_threshold": float(context_threshold),
        },
        "comparison": {
            "parent": {
                "model_revision": str(parent.model_revision),
                "reward_proxy": holdout_metrics.get("parent_reward_proxy"),
            },
            "supervised_parent": {
                "model_revision": str(parent.model_revision),
                "same_as_parent": True,
                "manual_fit": parent_manual,
            },
            "offline_rl_candidate": {
                "model_revision": str(child.model_revision),
                "reward_proxy": holdout_metrics.get("candidate_reward_proxy"),
                "reward_improvement_estimate": holdout_metrics.get(
                    "reward_improvement_estimate"
                ),
                "parent_action_agreement": holdout_metrics.get(
                    "parent_action_agreement"
                ),
                "action_drift_mean_tv": holdout_metrics.get(
                    "action_drift_mean_tv"
                ),
                "unseen_context_rate": (
                    holdout_metrics.get("unseen_context") or {}
                ).get("rate"),
                "q_proxy_calibration": holdout_metrics.get(
                    "q_proxy_calibration"
                ),
                "regression_count": holdout_metrics.get(
                    "logged_probability_regression_count"
                ),
            },
        },
        "behavior_propensity_known": False,
        "evaluation_claim": (
            "parent-relative logged-action reward proxy; not unbiased counterfactual OPE"
        ),
        "online_exploration": False,
        "physical_authority": False,
        "automatic_live_switch": False,
    }
