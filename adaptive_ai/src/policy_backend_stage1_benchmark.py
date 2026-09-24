"""Stage-1 Pi-oriented policy benchmark harness.

The harness is diagnostics only.  It measures backend computation/persistence overhead and
can project the added policy-inference component onto an externally measured event->intent
baseline.  It never creates ActionIntent and never dispatches Home Assistant services.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time

from policy_backend import serialize_backend_model


def _percentile(values, percentile):
    rows = sorted(float(x) for x in values)
    if not rows:
        return 0.0
    pos = (len(rows) - 1) * float(percentile)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return rows[lo]
    return rows[lo] * (hi - pos) + rows[hi] * (pos - lo)


def _deep_size(value, seen=None):
    seen = set() if seen is None else seen
    ident = id(value)
    if ident in seen:
        return 0
    seen.add(ident)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_deep_size(k, seen) + _deep_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(_deep_size(v, seen) for v in value)
    return size


class SyntheticTinyMLP:
    """Benchmark-only 64->32->16->N MLP; never used by runtime decisions."""

    BACKEND = "tiny_mlp"
    VERSION = 0

    def __init__(self, dims=64, hidden=(32, 16), outputs=2, model=None):
        self.dims = int(dims)
        self.hidden = tuple(int(x) for x in hidden)
        self.outputs = int(outputs)
        shapes = [self.dims, *self.hidden, self.outputs]
        if model:
            self.weights = model["weights"]
            self.biases = model["biases"]
        else:
            self.weights = []
            self.biases = []
            for layer, (left, right) in enumerate(zip(shapes, shapes[1:])):
                self.weights.append([
                    [((i * 17 + j * 31 + layer * 13) % 101 - 50) / 5000.0 for i in range(left)]
                    for j in range(right)
                ])
                self.biases.append([0.0] * right)

    @staticmethod
    def _relu(values):
        return [value if value > 0.0 else 0.0 for value in values]

    def _dense(self, features):
        return [float(features.get(i, 0.0)) for i in range(self.dims)]

    def predict(self, features):
        x = self._dense(features)
        for index, (weights, bias) in enumerate(zip(self.weights, self.biases)):
            x = [
                sum(weight * value for weight, value in zip(row, x)) + offset
                for row, offset in zip(weights, bias)
            ]
            if index < len(self.weights) - 1:
                x = self._relu(x)
        best = max(range(len(x)), key=lambda i: (x[i], -i))
        return {"index": int(best), "score": float(x[best])}

    def update(self, horizon, action_idx, features, reward, sample_ts=None):
        # Synthetic bounded update exists only so Stage-1 can measure update overhead.
        idx = max(0, min(self.outputs - 1, int(action_idx)))
        self.biases[-1][idx] += 0.001 * max(-1.0, min(1.0, float(reward)))

    def serialize(self):
        raw = {
            "version": self.VERSION,
            "dims": self.dims,
            "hidden": list(self.hidden),
            "outputs": self.outputs,
            "weights": self.weights,
            "biases": self.biases,
            "schema": {"version": 0, "entities": []},
        }
        return serialize_backend_model(
            raw, policy_backend=self.BACKEND, backend_version=self.VERSION,
            schema_id="benchmark-synthetic-v0", mask_id="benchmark-synthetic-mask",
        )

    @classmethod
    def deserialize(cls, raw):
        return cls(
            dims=raw["dims"], hidden=raw["hidden"], outputs=raw["outputs"], model=raw,
        )


def benchmark_backend(name, factory, features, *, iterations=250, updates=32,
                      event_to_intent_baseline_us=None):
    model = factory()
    inference_us = []
    for _ in range(max(1, int(iterations))):
        started = time.perf_counter_ns()
        model.predict(features)
        inference_us.append((time.perf_counter_ns() - started) / 1000.0)

    update_us = []
    for index in range(max(1, int(updates))):
        started = time.perf_counter_ns()
        model.update(1, index % 2, features, 1.0 if index % 2 else -1.0)
        update_us.append((time.perf_counter_ns() - started) / 1000.0)

    started = time.perf_counter_ns()
    raw = model.serialize()
    serialize_us = (time.perf_counter_ns() - started) / 1000.0
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    started = time.perf_counter_ns()
    restored = type(model).deserialize(raw)
    deserialize_us = (time.perf_counter_ns() - started) / 1000.0
    restored.predict(features)

    p95 = _percentile(inference_us, 0.95)
    result = {
        "backend": str(name),
        "iterations": len(inference_us),
        "inference_us": {
            "p50": _percentile(inference_us, 0.50),
            "p95": p95,
            "p99": _percentile(inference_us, 0.99),
            "mean": statistics.fmean(inference_us),
        },
        "synthetic_update_us": {
            "p50": _percentile(update_us, 0.50),
            "p95": _percentile(update_us, 0.95),
        },
        "serialize_us": serialize_us,
        "deserialize_us": deserialize_us,
        "serialized_bytes": len(encoded),
        "model_memory_bytes": _deep_size(raw),
        "projected_model_memory_bytes": {
            "20_agents": _deep_size(raw) * 20,
            "50_agents": _deep_size(raw) * 50,
        },
        "event_to_intent": {
            "policy_inference_p95_us": p95,
            "baseline_us": (
                None if event_to_intent_baseline_us is None
                else float(event_to_intent_baseline_us)
            ),
            "projected_with_policy_us": (
                None if event_to_intent_baseline_us is None
                else float(event_to_intent_baseline_us) + p95
            ),
            "scope": "projection_only_feature_extraction_transport_and_executor_excluded",
        },
        "dispatch_capability": False,
    }
    return result


def synthetic_tiny_mlp_benchmark(*, iterations=250, event_to_intent_baseline_us=None):
    features = {i: ((i % 11) - 5) / 5.0 for i in range(64)}
    return benchmark_backend(
        "tiny_mlp_synthetic_benchmark_only",
        lambda: SyntheticTinyMLP(dims=64, hidden=(32, 16), outputs=2),
        features,
        iterations=iterations,
        event_to_intent_baseline_us=event_to_intent_baseline_us,
    )
