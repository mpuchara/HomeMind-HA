"""Packaged Stage-3 benchmark for the real inference-only TinyMLPBackend.

This synthetic probe never connects to Home Assistant and never creates ActionIntent.
Run it on target hardware with:
    python /app/tiny_mlp_benchmark.py --iterations 200
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

from policy_tiny_mlp import TinyMLPBackend


def percentile(values, q):
    rows = sorted(float(x) for x in values)
    if not rows:
        return 0.0
    pos = (len(rows) - 1) * float(q)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return rows[lo]
    return rows[lo] + (rows[hi] - rows[lo]) * (pos - lo)


def rss_bytes():
    try:
        statm = Path("/proc/self/statm").read_text().split()
        return int(statm[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        try:
            import resource
            value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            return value * (1 if sys.platform == "darwin" else 1024)
        except Exception:
            return None


def make_template(feature_count=96, outputs=2):
    feature_ids = tuple(f"feature:{idx}" for idx in range(int(feature_count)))
    actions = tuple(float(idx) for idx in range(int(outputs)))
    backend = TinyMLPBackend(
        actions=actions,
        horizons=(1,),
        feature_ids=feature_ids,
        schema_id="benchmark-observation-schema-v1",
        mask_id="benchmark-mask-v1",
        hidden=(32, 16),
        init_seed=1482,
    )
    observation = {
        "feature_ids": list(feature_ids),
        "values": [((idx % 17) - 8) / 8.0 for idx in range(len(feature_ids))],
    }
    return backend, observation


def benchmark(iterations=200, feature_count=96, outputs=2):
    template, observation = make_template(feature_count, outputs)
    raw = template.serialize()

    started = time.perf_counter_ns()
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
    serialize_us = (time.perf_counter_ns() - started) / 1000.0

    started = time.perf_counter_ns()
    restarted = TinyMLPBackend.deserialize(
        json.loads(encoded),
        expected_schema_id=template.schema_id,
        expected_mask_id=template.mask_id,
        expected_feature_ids=template.feature_ids,
        expected_actions=template.actions,
        expected_horizons=template.horizons,
    )
    deserialize_us = (time.perf_counter_ns() - started) / 1000.0
    restart_same = (
        restarted.serialize()["model_checksum"] == raw["model_checksum"]
        and restarted.predict(observation)[0]["index"]
        == template.predict(observation)[0]["index"]
    )

    inference = []
    for _ in range(max(1, int(iterations))):
        started = time.perf_counter_ns()
        template.predict(observation)
        inference.append((time.perf_counter_ns() - started) / 1000.0)

    scenarios = {}
    retained = []
    baseline_rss = rss_bytes()
    for count in (1, 20, 50):
        started = time.perf_counter_ns()
        models = [
            TinyMLPBackend.deserialize(
                raw,
                expected_schema_id=template.schema_id,
                expected_mask_id=template.mask_id,
                expected_feature_ids=template.feature_ids,
                expected_actions=template.actions,
                expected_horizons=template.horizons,
            )
            for _ in range(count)
        ]
        load_us = (time.perf_counter_ns() - started) / 1000.0
        repeated = []
        rounds = max(2, min(10, int(iterations) // 20 or 2))
        for _ in range(rounds):
            for model in models:
                started = time.perf_counter_ns()
                model.predict(observation)
                repeated.append((time.perf_counter_ns() - started) / 1000.0)
        current_rss = rss_bytes()
        scenarios[str(count)] = {
            "loaded_models": count,
            "load_us": load_us,
            "repeated_inferences": len(repeated),
            "inference_us": {
                "p50": percentile(repeated, .50),
                "p95": percentile(repeated, .95),
                "p99": percentile(repeated, .99),
            },
            "rss_bytes": current_rss,
            "rss_delta_from_baseline_bytes": (
                None if current_rss is None or baseline_rss is None
                else max(0, int(current_rss) - int(baseline_rss))
            ),
        }
        retained = models
    assert retained

    inference_summary = {
        "p50": percentile(inference, .50),
        "p95": percentile(inference, .95),
        "p99": percentile(inference, .99),
        "mean": statistics.fmean(inference),
    }
    return {
        "backend": TinyMLPBackend.BACKEND,
        "backend_version": TinyMLPBackend.VERSION,
        "dtype": TinyMLPBackend.DTYPE,
        "architecture": list(template.architecture),
        "parameter_count": template.parameter_count,
        "feature_count": template.input_size,
        "output_count": template.output_size,
        "iterations": len(inference),
        "inference_us": inference_summary,
        "serialize_us": serialize_us,
        "deserialize_us": deserialize_us,
        "serialized_bytes": len(encoded.encode("utf-8")),
        "restart_persistence_identical": bool(restart_same),
        "models": scenarios,
        "event_to_intent": {
            "authoritative_ridge_path_changed": False,
            "telemetry_point_includes_tiny_mlp": False,
            "reason": "tiny MLP observer runs only after authoritative process_agent returns",
            "shadow_handler_tail_p95_us": inference_summary["p95"],
        },
        "training_enabled": False,
        "dispatch_capability": False,
        "physical_authority": False,
        "environment": {
            "python": sys.version,
            "platform": sys.platform,
            "rss_source": "/proc/self/statm current RSS with resource fallback",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--features", type=int, default=96)
    parser.add_argument("--outputs", type=int, default=2)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    result = benchmark(args.iterations, args.features, args.outputs)
    print(json.dumps(
        result,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
    ))


if __name__ == "__main__":
    main()
