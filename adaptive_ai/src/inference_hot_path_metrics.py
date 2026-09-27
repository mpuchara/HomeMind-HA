"""RAM-first bounded instrumentation for the realtime inference hot path.

The collector performs no SQLite I/O, logging or model serialization. It keeps only a
small rolling deque per known stage. Updates rely on CPython's atomic deque append so
instrumentation does not introduce another shared application lock into the HA event path.
"""
from __future__ import annotations

from collections import deque
import math
import time

STAGES = (
    "live_inference_total",
    "ridge_feature_construction",
    "ridge_predict",
    "hybrid_policy_total",
    "mlp_observation_construction",
    "mlp_forward",
    "hybrid_ridge_guard",
    "decision_composer",
    "candidate_before_live",
    "candidate_shadow_total",
    "candidate_feature_construction",
    "candidate_ridge_predict",
    "candidate_hybrid_policy_total",
    "candidate_mlp_observation_construction",
    "candidate_mlp_forward",
    "candidate_hybrid_ridge_guard",
    "candidate_wrapper_total",
    "shadow_mlp_observation_construction",
    "shadow_mlp_forward",
    "wrapped_process_agent_total",
)

def _percentile(values, q):
    rows = sorted(float(x) for x in values)
    if not rows:
        return None
    pos = (len(rows) - 1) * float(q)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return rows[lo]
    return rows[lo] + (rows[hi] - rows[lo]) * (pos - lo)

class _RollingStage:
    __slots__ = ("values", "count_total", "last", "maximum", "ewma")
    def __init__(self, window):
        self.values = deque(maxlen=int(window))
        self.count_total = 0
        self.last = None
        self.maximum = None
        self.ewma = None
    def observe(self, elapsed_ms):
        value = max(0.0, float(elapsed_ms))
        self.values.append(value)
        self.count_total += 1
        self.last = value
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.ewma = value if self.ewma is None else (0.18 * value + 0.82 * self.ewma)
    def snapshot(self):
        rows = tuple(self.values)
        return {
            "count": int(self.count_total), "window_count": len(rows),
            "p50_ms": _percentile(rows, .50), "p95_ms": _percentile(rows, .95),
            "p99_ms": _percentile(rows, .99), "max_ms": self.maximum,
            "last_ms": self.last, "ewma_ms": self.ewma,
        }

class InferenceHotPathMetrics:
    CONTRACT_VERSION = 1
    def __init__(self, window=256):
        self.window = max(32, min(2048, int(window)))
        self._stages = {name: _RollingStage(self.window) for name in STAGES}
        self._counters = {
            "mlp_model_deserialize": 0,
            "candidate_hybrid_fallbacks": 0,
            "shadow_mlp_fallback_observes": 0,
        }
    def observe(self, stage, elapsed_ms):
        metric = self._stages.get(str(stage))
        if metric is not None:
            metric.observe(elapsed_ms)
    def observe_ns(self, stage, started_ns):
        self.observe(stage, (time.perf_counter_ns() - int(started_ns)) / 1_000_000.0)
    def increment(self, name, value=1):
        key = str(name)
        if key in self._counters:
            self._counters[key] += int(value)
    def snapshot(self):
        return {
            "contract_version": self.CONTRACT_VERSION,
            "storage": "ram_only_bounded_no_sqlite",
            "window": self.window,
            "concurrency_note": "rolling deque samples are lock-free diagnostics on CPython",
            "stages": {name: metric.snapshot() for name, metric in self._stages.items() if metric.count_total},
            "counters": dict(self._counters),
        }

def observe_elapsed(engine, stage, started_ns):
    metrics = getattr(engine, "inference_hot_path_metrics", None)
    if metrics is not None:
        metrics.observe_ns(stage, started_ns)

def increment_counter(engine, name, value=1):
    metrics = getattr(engine, "inference_hot_path_metrics", None)
    if metrics is not None:
        metrics.increment(name, value)
