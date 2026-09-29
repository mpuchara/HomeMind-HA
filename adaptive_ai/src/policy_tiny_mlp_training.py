"""Offline supervised trainer and neutral Ridge-vs-MLP tournament for Stage 4.

The trainer consumes only historical imitation labels reconstructed by HistoryManager.
It never accepts live rewards, never dispatches an action and never changes Candidate
promotion rules. The existing chronological holdout remains untouched until tournament
scoring; the same labelled holdout rows are scored by Ridge and MLP.

Pure-Python SGD is intentionally bounded for Raspberry Pi deployments: deterministic
sample cap, small mini-batches, bounded epochs, early stopping, L2 regularization and
global gradient clipping.
"""
from __future__ import annotations

from array import array
import json
import math
import random
import time
import uuid

from policy_tiny_mlp import TinyMLPBackend


_NUMPY = None
_NUMPY_CHECKED = False


def _numpy_module():
    """Load NumPy lazily so realtime startup never pays the import/RSS cost.

    Production images include NumPy for historical/Correct training. Source-only test
    environments may omit it; in that case the exact scalar trainer remains available.
    """
    global _NUMPY, _NUMPY_CHECKED
    if not _NUMPY_CHECKED:
        try:
            import numpy as np
        except ImportError:
            np = None
        _NUMPY = np
        _NUMPY_CHECKED = True
    return _NUMPY


def _finite(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def bounded_samples(samples, max_samples):
    rows = list(samples or ())
    limit = max(1, int(max_samples))
    if len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    step = (len(rows) - 1) / float(limit - 1)
    return [rows[int(round(index * step))] for index in range(limit)]


def _observation_values(backend, sample):
    observation = sample.get("observation") or {}
    ids = tuple(str(x) for x in observation.get("feature_ids") or ())
    if ids != backend.feature_ids:
        raise ValueError("NEEDS_RETRAIN: supervised tiny MLP sample feature order mismatch")
    values = [float(x) for x in observation.get("values") or ()]
    if len(values) != backend.input_size or not all(math.isfinite(x) for x in values):
        raise ValueError("invalid supervised tiny MLP observation")
    return values


def _set_initial_normalization(backend, samples):
    if backend.trained or backend.training_samples > 0:
        return
    rows = [_observation_values(backend, sample) for sample in samples]
    if not rows:
        return
    n = float(len(rows))
    mean = [sum(row[i] for row in rows) / n for i in range(backend.input_size)]
    variance = [
        sum((row[i] - mean[i]) ** 2 for row in rows) / n
        for i in range(backend.input_size)
    ]
    scale = [max(1e-3, math.sqrt(value)) for value in variance]
    backend.input_mean = array("f", mean)
    backend.input_scale = array("f", scale)


def _class_weights(samples, action_count):
    counts = [0] * int(action_count)
    for row in samples:
        idx = int(row.get("action_idx"))
        if 0 <= idx < len(counts):
            counts[idx] += 1
    present = sum(1 for value in counts if value > 0)
    total = sum(counts)
    weights = [1.0] * len(counts)
    if total > 0 and present > 1:
        for idx, count in enumerate(counts):
            if count > 0:
                weights[idx] = min(4.0, max(0.25, total / float(present * count)))
    return counts, weights


def _numpy_softmax(np, logits):
    peak = np.max(logits, axis=1, keepdims=True)
    exp = np.exp(np.clip(logits - peak, -60.0, 60.0))
    total = np.sum(exp, axis=1, keepdims=True)
    total = np.where((total > 0.0) & np.isfinite(total), total, 1.0)
    return exp / total


def _train_supervised_numpy(
    backend,
    rows,
    *,
    class_counts,
    class_weight,
    max_samples,
    max_epochs,
    batch_size,
    learning_rate,
    l2,
    gradient_clip,
    early_stop_patience,
    early_stop_min_delta,
    checkpoint,
    started,
):
    """Vectorized equivalent of the scalar mini-batch trainer.

    The complete bounded observation matrix is normalized once and retained in RAM for
    all epochs. Dense forward/backward math then runs in NumPy instead of Python MAC
    loops. Parameters are rounded back to float32 after every optimizer batch, matching
    the persistent TinyMLP tensor contract. The isolated worker owns this memory and
    releases it when the historical chunk exits.
    """
    np = _numpy_module()
    if np is None:
        return None

    row_count = len(rows)
    input_size = int(backend.input_size)
    matrix = np.empty((row_count, input_size), dtype=np.float64)
    targets = np.empty(row_count, dtype=np.int64)
    source_weights = np.empty(row_count, dtype=np.float64)
    for index, row in enumerate(rows):
        matrix[index, :] = _observation_values(backend, row)
        targets[index] = int(row["action_idx"])
        source_weights[index] = max(0.0, _finite(row.get("weight"), 1.0))

    means = np.asarray(backend.input_mean, dtype=np.float64)
    scales = np.asarray(backend.input_scale, dtype=np.float64)
    matrix -= means
    matrix /= scales
    np.clip(matrix, -6.0, 6.0, out=matrix)
    class_weight_vector = np.asarray(class_weight, dtype=np.float64)
    effective_weights = source_weights * class_weight_vector[targets]

    architecture = tuple(int(x) for x in backend.architecture)
    weights = [
        np.asarray(layer, dtype=np.float64).reshape(
            architecture[index + 1], architecture[index]
        ).copy()
        for index, layer in enumerate(backend.weights)
    ]
    biases = [
        np.asarray(layer, dtype=np.float64).copy()
        for layer in backend.biases
    ]

    rng = random.Random(
        int(backend.init_seed) ^ int(backend.training_samples) ^ row_count
    )
    order = list(range(row_count))
    best_loss = math.inf
    best_weights = None
    best_biases = None
    best_epoch = 0
    stale_epochs = 0
    clip_events = 0
    batch_updates = 0
    epoch_losses = []

    for epoch in range(1, max_epochs + 1):
        rng.shuffle(order)
        for batch_start in range(0, row_count, batch_size):
            indices = order[batch_start:batch_start + batch_size]
            xb = matrix[indices]
            yb = targets[indices]
            wb = effective_weights[indices]
            batch_weight = float(np.sum(wb))
            if batch_weight <= 0.0:
                continue

            activations = [xb]
            current = xb
            for layer_index, (weight, bias) in enumerate(zip(weights, biases)):
                current = current @ weight.T + bias
                if layer_index < len(weights) - 1:
                    current = np.maximum(current, 0.0)
                activations.append(current)

            probabilities = _numpy_softmax(np, activations[-1])
            delta = probabilities.copy()
            delta[np.arange(len(indices)), yb] -= 1.0
            delta *= wb[:, None]

            grad_w = [None] * len(weights)
            grad_b = [None] * len(biases)
            for layer_index in range(len(weights) - 1, -1, -1):
                previous = activations[layer_index]
                grad_w[layer_index] = (delta.T @ previous) / batch_weight
                grad_w[layer_index] += l2 * weights[layer_index]
                grad_b[layer_index] = np.sum(delta, axis=0) / batch_weight
                if layer_index > 0:
                    delta = delta @ weights[layer_index]
                    delta *= (previous > 0.0)

            norm_sq = 0.0
            for grad in grad_w:
                norm_sq += float(np.sum(grad * grad))
            for grad in grad_b:
                norm_sq += float(np.sum(grad * grad))
            grad_norm = math.sqrt(norm_sq)
            clip_scale = min(1.0, gradient_clip / max(1e-12, grad_norm))
            if clip_scale < 0.999999:
                clip_events += 1

            for layer_index in range(len(weights)):
                weights[layer_index] -= (
                    learning_rate * grad_w[layer_index] * clip_scale
                )
                biases[layer_index] -= (
                    learning_rate * grad_b[layer_index] * clip_scale
                )
                # TinyMLP parameters are float32 at every scalar update. Keep the same
                # quantization boundary instead of accumulating hidden float64 state.
                weights[layer_index] = weights[layer_index].astype(
                    np.float32
                ).astype(np.float64)
                biases[layer_index] = biases[layer_index].astype(
                    np.float32
                ).astype(np.float64)

            batch_updates += 1
            if callable(checkpoint):
                checkpoint("tiny_mlp_supervised_batch")

        current = matrix
        for layer_index, (weight, bias) in enumerate(zip(weights, biases)):
            current = current @ weight.T + bias
            if layer_index < len(weights) - 1:
                current = np.maximum(current, 0.0)
        probabilities = _numpy_softmax(np, current)
        selected = probabilities[np.arange(row_count), targets]
        weighted_loss = -np.log(np.maximum(1e-9, selected)) * effective_weights
        epoch_loss = float(np.sum(weighted_loss)) / max(
            1e-9, float(np.sum(effective_weights))
        )
        epoch_losses.append(float(epoch_loss))
        if best_loss - epoch_loss > early_stop_min_delta:
            best_loss = float(epoch_loss)
            best_epoch = epoch
            best_weights = [weight.copy() for weight in weights]
            best_biases = [bias.copy() for bias in biases]
            stale_epochs = 0
        else:
            stale_epochs += 1
        if callable(checkpoint):
            checkpoint("tiny_mlp_supervised_epoch", force=True)
        if stale_epochs >= early_stop_patience:
            break

    if best_weights is not None:
        weights = best_weights
        biases = best_biases
    backend.weights = [
        array("f", weight.reshape(-1).astype(np.float32).tolist())
        for weight in weights
    ]
    backend.biases = [
        array("f", bias.astype(np.float32).tolist())
        for bias in biases
    ]
    backend.trained = True
    backend.training_samples = int(backend.training_samples) + row_count
    backend.model_revision = str(uuid.uuid4())
    elapsed = max(0.0, time.perf_counter() - started)
    matrix_bytes = int(
        matrix.nbytes + targets.nbytes + source_weights.nbytes
        + effective_weights.nbytes
        + sum(weight.nbytes for weight in weights)
        + sum(bias.nbytes for bias in biases)
    )
    meta = {
        "trainer": "offline_supervised_cross_entropy_v2",
        "execution_backend": "numpy_vectorized",
        "samples": row_count,
        "training_samples_total": int(backend.training_samples),
        "class_counts": {str(i): int(v) for i, v in enumerate(class_counts)},
        "class_weight": {str(i): float(v) for i, v in enumerate(class_weight)},
        "epochs": len(epoch_losses),
        "best_epoch": int(best_epoch),
        "best_loss": None if not math.isfinite(best_loss) else float(best_loss),
        "last_loss": float(epoch_losses[-1]) if epoch_losses else None,
        "learning_rate": learning_rate,
        "batch_size": batch_size,
        "l2": l2,
        "gradient_clip": gradient_clip,
        "gradient_clip_events": int(clip_events),
        "batch_updates": int(batch_updates),
        "early_stop_patience": early_stop_patience,
        "early_stop_min_delta": early_stop_min_delta,
        "max_samples": int(max_samples),
        "prepared_matrix_bytes": matrix_bytes,
        "prepared_matrix_dtype": "float64",
        "elapsed_seconds": round(elapsed, 4),
        "online_reward_updates": False,
    }
    backend.training_meta = {**dict(backend.training_meta or {}), "supervised": meta}
    return {"trained": True, **meta}


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
        if layer_index == last_layer:
            current = output
        else:
            current = [value if value > 0.0 else 0.0 for value in output]
        activations.append(current)
    return activations


def _loss_only(backend, samples, class_weight):
    total = weighted = 0.0
    for row in samples:
        target = int(row["action_idx"])
        normalized = backend.normalized_input(row["observation"])
        logits = backend._forward(normalized, already_normalized=True)
        probabilities = backend.softmax(logits)
        weight = max(0.0, _finite(row.get("weight"), 1.0)) * class_weight[target]
        total += -math.log(max(1e-9, probabilities[target])) * weight
        weighted += weight
    return total / max(1e-9, weighted)


def train_supervised(
    backend,
    samples,
    *,
    max_samples=4096,
    max_epochs=12,
    batch_size=16,
    learning_rate=0.012,
    l2=1e-4,
    gradient_clip=1.0,
    early_stop_patience=3,
    early_stop_min_delta=1e-3,
    checkpoint=None,
):
    """Mutate the backend with bounded offline supervised imitation learning."""
    rows = bounded_samples(samples, max_samples)
    rows = [
        row for row in rows
        if 0 <= int(row.get("action_idx", -1)) < len(backend.actions)
        and _finite(row.get("weight"), 1.0) > 0.0
    ]
    if not rows:
        return {
            "trained": False,
            "reason": "no_positive_supervised_samples",
            "samples": 0,
            "epochs": 0,
        }

    _set_initial_normalization(backend, rows)
    class_counts, class_weight = _class_weights(rows, len(backend.actions))
    max_epochs = max(1, min(64, int(max_epochs)))
    batch_size = max(1, min(128, int(batch_size)))
    lr = max(1e-6, min(1.0, float(learning_rate)))
    l2 = max(0.0, min(0.1, float(l2)))
    clip = max(1e-4, min(100.0, float(gradient_clip)))
    patience = max(1, min(20, int(early_stop_patience)))
    min_delta = max(0.0, float(early_stop_min_delta))

    started = time.perf_counter()
    vectorized = _train_supervised_numpy(
        backend,
        rows,
        class_counts=class_counts,
        class_weight=class_weight,
        max_samples=max_samples,
        max_epochs=max_epochs,
        batch_size=batch_size,
        learning_rate=lr,
        l2=l2,
        gradient_clip=clip,
        early_stop_patience=patience,
        early_stop_min_delta=min_delta,
        checkpoint=checkpoint,
        started=started,
    )
    if vectorized is not None:
        return vectorized

    rng = random.Random(int(backend.init_seed) ^ int(backend.training_samples) ^ len(rows))
    order = list(range(len(rows)))
    best_loss = math.inf
    best_weights = None
    best_biases = None
    best_epoch = 0
    stale_epochs = 0
    clip_events = 0
    batch_updates = 0
    epoch_losses = []

    for epoch in range(1, max_epochs + 1):
        rng.shuffle(order)
        for batch_start in range(0, len(order), batch_size):
            indices = order[batch_start:batch_start + batch_size]
            grad_w = [array("d", [0.0] * len(layer)) for layer in backend.weights]
            grad_b = [array("d", [0.0] * len(layer)) for layer in backend.biases]
            batch_weight = 0.0

            for sample_index in indices:
                sample = rows[sample_index]
                target = int(sample["action_idx"])
                sample_weight = (
                    max(0.0, _finite(sample.get("weight"), 1.0))
                    * float(class_weight[target])
                )
                if sample_weight <= 0.0:
                    continue
                normalized = backend.normalized_input(sample["observation"])
                activations = _forward_activations(backend, normalized)
                probabilities = backend.softmax(activations[-1])
                delta = [float(value) for value in probabilities]
                delta[target] -= 1.0
                delta = [value * sample_weight for value in delta]
                batch_weight += sample_weight

                for layer_index in range(len(backend.weights) - 1, -1, -1):
                    fan_in = int(backend.architecture[layer_index])
                    fan_out = int(backend.architecture[layer_index + 1])
                    previous = activations[layer_index]
                    weights = backend.weights[layer_index]
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
                                value if activations[layer_index][column] > 0.0 else 0.0
                            )
                        delta = propagated

            if batch_weight <= 0.0:
                continue
            inv = 1.0 / batch_weight
            norm_sq = 0.0
            for layer_index, layer in enumerate(backend.weights):
                for idx in range(len(layer)):
                    grad = grad_w[layer_index][idx] * inv + l2 * float(layer[idx])
                    grad_w[layer_index][idx] = grad
                    norm_sq += grad * grad
                for idx in range(len(backend.biases[layer_index])):
                    grad = grad_b[layer_index][idx] * inv
                    grad_b[layer_index][idx] = grad
                    norm_sq += grad * grad
            grad_norm = math.sqrt(norm_sq)
            clip_scale = min(1.0, clip / max(1e-12, grad_norm))
            if clip_scale < 0.999999:
                clip_events += 1

            for layer_index, layer in enumerate(backend.weights):
                for idx in range(len(layer)):
                    layer[idx] = float(layer[idx]) - lr * grad_w[layer_index][idx] * clip_scale
                biases = backend.biases[layer_index]
                for idx in range(len(biases)):
                    biases[idx] = float(biases[idx]) - lr * grad_b[layer_index][idx] * clip_scale
            batch_updates += 1
            if callable(checkpoint):
                checkpoint("tiny_mlp_supervised_batch")

        epoch_loss = _loss_only(backend, rows, class_weight)
        epoch_losses.append(float(epoch_loss))
        if best_loss - epoch_loss > min_delta:
            best_loss = float(epoch_loss)
            best_epoch = epoch
            best_weights = [array("f", layer) for layer in backend.weights]
            best_biases = [array("f", layer) for layer in backend.biases]
            stale_epochs = 0
        else:
            stale_epochs += 1
        if callable(checkpoint):
            checkpoint("tiny_mlp_supervised_epoch", force=True)
        if stale_epochs >= patience:
            break

    if best_weights is not None:
        backend.weights = best_weights
        backend.biases = best_biases
    backend.trained = True
    backend.training_samples = int(backend.training_samples) + len(rows)
    backend.model_revision = str(uuid.uuid4())
    elapsed = max(0.0, time.perf_counter() - started)
    meta = {
        "trainer": "offline_supervised_cross_entropy_v2",
        "execution_backend": "python_scalar_fallback",
        "samples": len(rows),
        "training_samples_total": int(backend.training_samples),
        "class_counts": {str(i): int(v) for i, v in enumerate(class_counts)},
        "class_weight": {str(i): float(v) for i, v in enumerate(class_weight)},
        "epochs": len(epoch_losses),
        "best_epoch": int(best_epoch),
        "best_loss": None if not math.isfinite(best_loss) else float(best_loss),
        "last_loss": float(epoch_losses[-1]) if epoch_losses else None,
        "learning_rate": lr,
        "batch_size": batch_size,
        "l2": l2,
        "gradient_clip": clip,
        "gradient_clip_events": int(clip_events),
        "batch_updates": int(batch_updates),
        "early_stop_patience": patience,
        "early_stop_min_delta": min_delta,
        "max_samples": int(max_samples),
        "elapsed_seconds": round(elapsed, 4),
        "online_reward_updates": False,
    }
    backend.training_meta = {**dict(backend.training_meta or {}), "supervised": meta}
    return {"trained": True, **meta}


def _score_from_counts(agent, actions, stats):
    samples = int(stats.get("samples") or 0)
    per_action = stats.get("per_action") or {}
    per_action_accuracy = {
        str(key): float(value.get("correct") or 0) / max(1, int(value.get("samples") or 0))
        for key, value in per_action.items()
        if int(value.get("samples") or 0) > 0
    }
    binary = len(actions) <= 2 or str(agent.get("target_property")) == "power"
    if binary and per_action_accuracy:
        score = sum(per_action_accuracy.values()) / len(per_action_accuracy)
    else:
        score = float(stats.get("correct") or 0) / samples if samples else 0.0
    class_coverage = (len(per_action_accuracy) >= 2) if binary else bool(per_action_accuracy)
    return float(score), bool(class_coverage), per_action_accuracy, bool(binary)


def exact_paired_mlp_win_p_value(mlp_only_correct, ridge_only_correct):
    """One-sided exact McNemar/sign-test tail on paired correctness disagreements.

    Under the null, either model is equally likely to own each discordant holdout row.
    The returned probability is P(X >= mlp_only_correct | X~Binomial(n, 0.5)).
    No asymptotic approximation is used; holdouts are bounded to a few thousand rows.
    """
    mlp_only = max(0, int(mlp_only_correct or 0))
    ridge_only = max(0, int(ridge_only_correct or 0))
    discordant = mlp_only + ridge_only
    if discordant <= 0:
        return 1.0
    if mlp_only <= 0:
        return 1.0
    if mlp_only > discordant:
        return 0.0
    denominator = 1 << discordant
    # Sum the shorter exact tail and use integer arithmetic until final conversion.
    if mlp_only > discordant // 2:
        numerator = sum(
            math.comb(discordant, value)
            for value in range(mlp_only, discordant + 1)
        )
    else:
        lower = sum(
            math.comb(discordant, value)
            for value in range(0, mlp_only)
        )
        numerator = denominator - lower
    return min(1.0, max(0.0, float(numerator / denominator)))


def _numpy_prediction_indices(backend, rows):
    """Return dense batch predictions or None when NumPy is unavailable."""
    np = _numpy_module()
    if np is None or not rows:
        return None
    matrix = np.empty((len(rows), int(backend.input_size)), dtype=np.float64)
    for index, row in enumerate(rows):
        matrix[index, :] = _observation_values(backend, row)
    matrix -= np.asarray(backend.input_mean, dtype=np.float64)
    matrix /= np.asarray(backend.input_scale, dtype=np.float64)
    np.clip(matrix, -6.0, 6.0, out=matrix)
    current = matrix
    architecture = tuple(int(x) for x in backend.architecture)
    for layer_index, (raw_weight, raw_bias) in enumerate(
        zip(backend.weights, backend.biases)
    ):
        weight = np.asarray(raw_weight, dtype=np.float64).reshape(
            architecture[layer_index + 1], architecture[layer_index]
        )
        bias = np.asarray(raw_bias, dtype=np.float64)
        current = current @ weight.T + bias
        if layer_index < len(backend.weights) - 1:
            current = np.maximum(current, 0.0)
    # Softmax is monotonic per row, so argmax(logits) is exactly the same action
    # selection as argmax(softmax(logits)) while avoiding another allocation.
    return np.argmax(current, axis=1).astype(np.int64).tolist()


def evaluate_supervised(backend, agent, samples):
    rows = list(samples or ())
    predicted_indices = _numpy_prediction_indices(backend, rows)
    stats = {"samples": 0, "correct": 0, "per_action": {}}
    paired = {
        "contract": "paired_holdout_correctness_v1",
        "samples": 0,
        "both_correct": 0,
        "mlp_only_correct": 0,
        "ridge_only_correct": 0,
        "both_wrong": 0,
    }
    tolerance = max(
        float(agent.get("deadband") or 0.0),
        (float(agent.get("max_value") or 0.0) - float(agent.get("min_value") or 0.0)) * 0.03,
    )
    for row_index, row in enumerate(rows):
        target = int(row.get("action_idx", -1))
        if not (0 <= target < len(backend.actions)):
            continue
        if predicted_indices is None:
            chosen = backend.predict(row["observation"])[0]
            predicted = int(chosen["index"])
        else:
            predicted = int(predicted_indices[row_index])
        if len(backend.actions) <= 2 or str(agent.get("target_property")) == "power":
            correct = predicted == target
        else:
            correct = abs(float(backend.actions[predicted]) - float(backend.actions[target])) <= tolerance
        stats["samples"] += 1
        stats["correct"] += int(correct)
        slot = stats["per_action"].setdefault(str(target), {"samples": 0, "correct": 0})
        slot["samples"] += 1
        slot["correct"] += int(correct)
        if "ridge_correct" in row:
            ridge_correct = bool(row.get("ridge_correct"))
            paired["samples"] += 1
            if correct and ridge_correct:
                paired["both_correct"] += 1
            elif correct and not ridge_correct:
                paired["mlp_only_correct"] += 1
            elif ridge_correct:
                paired["ridge_only_correct"] += 1
            else:
                paired["both_wrong"] += 1
    score, coverage, per_action_accuracy, binary = _score_from_counts(
        agent, backend.actions, stats
    )
    paired["discordant"] = (
        int(paired["mlp_only_correct"]) + int(paired["ridge_only_correct"])
    )
    paired["complete"] = int(paired["samples"]) == int(stats["samples"])
    paired["mlp_win_rate_on_discordant"] = (
        float(paired["mlp_only_correct"]) / paired["discordant"]
        if paired["discordant"] else None
    )
    return {
        **stats,
        "score": score,
        "class_coverage": coverage,
        "per_action_accuracy": per_action_accuracy,
        "balanced": binary,
        "paired_comparison": paired,
    }


def tournament_result(
    *,
    agent,
    actions,
    ridge_stats,
    mlp_metrics,
    threshold,
    minimum_samples,
    minimum_gain=0.03,
    significance_alpha=0.05,
    minimum_discordant_pairs=8,
    parameter_count=None,
    serialized_bytes=None,
    max_parameters=50000,
    max_serialized_bytes=524288,
):
    ridge_score, ridge_coverage, ridge_per_action, binary = _score_from_counts(
        agent, actions, ridge_stats or {}
    )
    mlp_score = float(mlp_metrics.get("score") or 0.0)
    mlp_coverage = bool(mlp_metrics.get("class_coverage"))
    ridge_samples = int((ridge_stats or {}).get("samples") or 0)
    mlp_samples = int(mlp_metrics.get("samples") or 0)
    paired = dict(mlp_metrics.get("paired_comparison") or {})
    paired_samples = int(paired.get("samples") or 0)
    sample_count_match = ridge_samples == mlp_samples
    paired_complete = bool(
        paired.get("complete")
        and paired_samples == mlp_samples
        and paired_samples == ridge_samples
    )
    same_rows = bool(sample_count_match and paired_complete)
    enough = mlp_samples >= int(minimum_samples) and mlp_coverage
    threshold_passed = bool(enough and mlp_score > float(threshold))
    resource_passed = (
        (parameter_count is None or int(parameter_count) <= int(max_parameters))
        and (serialized_bytes is None or int(serialized_bytes) <= int(max_serialized_bytes))
    )
    gain = mlp_score - ridge_score
    min_gain = max(0.0, min(1.0, float(minimum_gain)))
    gain_passed = bool(gain + 1e-12 >= min_gain)
    alpha = max(1e-6, min(0.50, float(significance_alpha)))
    min_discordant = max(1, min(4096, int(minimum_discordant_pairs)))
    mlp_only = int(paired.get("mlp_only_correct") or 0)
    ridge_only = int(paired.get("ridge_only_correct") or 0)
    discordant = mlp_only + ridge_only
    discordant_pairs_passed = bool(
        paired_complete and discordant >= min_discordant
    )
    p_value = (
        exact_paired_mlp_win_p_value(mlp_only, ridge_only)
        if paired_complete else None
    )
    significance_passed = bool(
        discordant_pairs_passed
        and mlp_only > ridge_only
        and p_value is not None
        and p_value <= alpha
    )
    wins = bool(
        same_rows
        and ridge_coverage
        and threshold_passed
        and resource_passed
        and gain_passed
        and significance_passed
    )
    selected = TinyMLPBackend.BACKEND if wins else "diagonal_linucb"
    if not sample_count_match:
        reason = "holdout_sample_mismatch"
    elif not paired_complete:
        reason = "paired_holdout_evidence_missing_or_incomplete"
    elif not ridge_coverage:
        reason = "ridge_holdout_class_coverage_insufficient"
    elif not enough:
        reason = "mlp_holdout_evidence_insufficient"
    elif not threshold_passed:
        reason = "mlp_below_existing_candidate_threshold"
    elif not resource_passed:
        reason = "mlp_resource_gate_failed"
    elif not gain_passed:
        reason = "mlp_gain_below_practical_minimum"
    elif not discordant_pairs_passed:
        reason = "mlp_holdout_discordant_pairs_insufficient"
    elif not significance_passed:
        reason = "mlp_paired_improvement_not_significant"
    else:
        reason = "mlp_meaningfully_and_significantly_better_on_identical_holdout"
    return {
        "contract": "ridge_vs_tiny_mlp_paired_holdout_v3",
        "selection_bias": "same_rows_same_metric_practical_gain_min_discordant_plus_exact_paired_significance_required",
        "automatic_physical_switch": False,
        "samples": mlp_samples,
        "same_holdout_rows": same_rows,
        "balanced": binary,
        "threshold": float(threshold),
        "minimum_samples": int(minimum_samples),
        "minimum_gain": float(min_gain),
        "gain_passed": gain_passed,
        "significance_test": "exact_one_sided_mcnemar_binomial",
        "significance_alpha": float(alpha),
        "minimum_discordant_pairs": int(min_discordant),
        "discordant_pairs": int(discordant),
        "discordant_pairs_passed": discordant_pairs_passed,
        "significance_p_value": p_value,
        "significance_passed": significance_passed,
        "paired_comparison": paired,
        "ridge_score": float(ridge_score),
        "ridge_class_coverage": ridge_coverage,
        "ridge_per_action_accuracy": ridge_per_action,
        "mlp_score": float(mlp_score),
        "mlp_class_coverage": mlp_coverage,
        "mlp_per_action_accuracy": dict(mlp_metrics.get("per_action_accuracy") or {}),
        "gain": float(gain),
        "threshold_passed": threshold_passed,
        "resource_gate_passed": resource_passed,
        "parameter_count": None if parameter_count is None else int(parameter_count),
        "serialized_bytes": None if serialized_bytes is None else int(serialized_bytes),
        "selected_backend": selected,
        "passed": wins,
        "reason": reason,
    }


def build_training_artifact(*, agent, policy, mask, backend, trainer, tournament):
    backend.training_meta = {
        **dict(backend.training_meta or {}),
        "tournament": dict(tournament or {}),
    }
    raw = backend.serialize()
    return {
        "format": "homemind-tiny-mlp-training-artifact",
        "version": 1,
        "agent_id": str(agent["id"]),
        "source_policy_revision": str(
            getattr(policy, "tournament_revision", None)
            or getattr(policy, "model_revision", None)
            or "unknown"
        ),
        "mask": mask.export(),
        "model": raw,
        "trainer": dict(trainer or {}),
        "tournament": dict(tournament or {}),
        "selected_backend": str(
            (tournament or {}).get("selected_backend") or "diagonal_linucb"
        ),
        "created_ts": time.time(),
    }
