#!/usr/bin/env python3
"""ETAP-4 same-process hotspot profiler for Hybrid inference.

Compares the previous implementation shape with the optimized implementation on identical
inputs. Absolute timings are descriptive; semantic parity is the hard contract.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Production observation imports initialize Store. Keep the profiler completely isolated
# from the add-on's /data, matching tools/benchmark_hybrid_inference.py.
_SCRATCH = tempfile.TemporaryDirectory(prefix="homemind-hotspot-benchmark-")
os.environ["ADAPTIVE_AI_DATA"] = _SCRATCH.name

from hybrid_inference_benchmark import fixture, state
from observation_contract import (
    _last_edge_time,
    _same_observation,
    observation_value,
    policy_features,
)
from observation_space import observation_as_of
from shared_inference_context import shared_inference_temporal


def percentile(values, q):
    rows = sorted(float(x) for x in values)
    if not rows:
        return None
    pos = (len(rows) - 1) * float(q)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return rows[lo]
    return rows[lo] + (rows[hi] - rows[lo]) * (pos - lo)


def stats(rows):
    values = [float(x) for x in rows]
    return {
        "count": len(values),
        "p50_us": percentile(values, .50),
        "p95_us": percentile(values, .95),
        "mean_us": statistics.fmean(values) if values else None,
    }


def timed(fn, iterations):
    rows = []
    result = None
    for _ in range(max(1, int(iterations))):
        started = time.perf_counter_ns()
        result = fn()
        rows.append((time.perf_counter_ns() - started) / 1000.0)
    return result, stats(rows)


def old_forward_reference(model, dense):
    current = [
        max(-6.0, min(6.0, (float(value) - float(mean)) / float(scale)))
        for value, mean, scale in zip(dense, model.input_mean, model.input_scale)
    ]
    for layer_index, (fan_in, fan_out) in enumerate(
        zip(model.architecture, model.architecture[1:])
    ):
        weights = model.weights[layer_index]
        biases = model.biases[layer_index]
        output = []
        for row in range(int(fan_out)):
            offset = row * int(fan_in)
            value = float(biases[row])
            for column in range(int(fan_in)):
                value += float(weights[offset + column]) * float(current[column])
            output.append(value)
        current = (
            output
            if layer_index == len(model.weights) - 1
            else [value if value > 0.0 else 0.0 for value in output]
        )
    return current


def old_edge_reference(temporal, entity_id, at_ts, current):
    ordered = [
        (float(ts), st)
        for ts, st in temporal.samples_for(entity_id)
        if float(ts) <= float(at_ts) + 1e-9
    ]
    if not ordered:
        return None
    last_valid = None
    first_valid_ts = None
    valid_count = 0
    edge = None
    for ts, st in ordered:
        obs = observation_value(st)
        if not obs["valid"]:
            continue
        valid_count += 1
        if first_valid_ts is None:
            first_valid_ts = ts
        if last_valid is not None:
            same = _same_observation(last_valid[1], st)
            if same is False:
                edge = ts
        last_valid = (ts, st)
    if edge is not None:
        return edge
    if valid_count >= 2:
        return first_valid_ts
    return last_valid[0] if last_valid else None


def fill_history(temporal, states, ts, depth):
    depth = max(4, min(96, int(depth)))
    for eid, current in states.items():
        if eid == "light.target":
            continue
        base = float(current["state"])
        rows = deque(maxlen=96)
        for age in range(depth - 1, -1, -1):
            sample_ts = float(ts) - float(age)
            # A deterministic edge-rich numeric history exercises decoding and edge scans.
            value = base + ((depth - age) % 7) * 0.05
            rows.append((
                sample_ts,
                state(
                    eid, value, sample_ts,
                    device_class="temperature",
                    unit_of_measurement="°C",
                ),
            ))
        temporal.samples[eid] = rows


def run(iterations=400, entities=8, history_depth=96):
    states, temporal, home, live, _candidate, mask, mlp, _cm, ts = fixture(entities)
    fill_history(temporal, states, ts, history_depth)

    shared = shared_inference_temporal(temporal, ts, home_provider=home)
    features, _labels, _meta = policy_features(live, states, shared, at_ts=ts)
    observation = observation_as_of(
        mask, states, shared, ts, live.agent, home_provider=shared.home_context
    )
    dense = mlp._dense_input(observation)

    old_scores, old_forward = timed(
        lambda: old_forward_reference(mlp, dense), iterations
    )
    new_scores, new_forward = timed(lambda: mlp._forward(dense), iterations)
    forward_equal = old_scores == new_scores

    entity_ids = tuple(live.schema.entities)
    current_states = {eid: states[eid] for eid in entity_ids}
    old_edges, old_edge = timed(
        lambda: tuple(
            old_edge_reference(shared, eid, ts, current_states[eid])
            for eid in entity_ids
        ),
        iterations,
    )
    new_edges, new_edge = timed(
        lambda: tuple(
            _last_edge_time(shared, eid, ts, current_states[eid])
            for eid in entity_ids
        ),
        iterations,
    )
    edge_equal = old_edges == new_edges

    _ridge_result, ridge_full = timed(
        lambda: policy_features(
            live,
            states,
            shared_inference_temporal(temporal, ts, home_provider=home),
            at_ts=ts,
        ),
        iterations,
    )
    _obs_result, mlp_observation = timed(
        lambda: observation_as_of(
            mask,
            states,
            shared_inference_temporal(temporal, ts, home_provider=home),
            ts,
            live.agent,
        ),
        iterations,
    )
    _predict_result, mlp_predict = timed(lambda: mlp.predict(observation), iterations)

    return {
        "contract": "hybrid_inference_hotspot_profile_etap4_v1",
        "iterations": int(iterations),
        "entities": len(entity_ids),
        "history_depth": int(history_depth),
        "parity": {
            "mlp_forward_exact": bool(forward_equal),
            "ridge_edge_exact": bool(edge_equal),
        },
        "hotspots": {
            "mlp_forward_old_reference": old_forward,
            "mlp_forward_optimized": new_forward,
            "mlp_forward_p95_ratio_new_over_old": (
                float(new_forward["p95_us"]) / max(float(old_forward["p95_us"]), 1e-9)
            ),
            "ridge_edge_old_reference": old_edge,
            "ridge_edge_optimized": new_edge,
            "ridge_edge_p95_ratio_new_over_old": (
                float(new_edge["p95_us"]) / max(float(old_edge["p95_us"]), 1e-9)
            ),
            "ridge_feature_full": ridge_full,
            "mlp_observation_full": mlp_observation,
            "mlp_predict_full": mlp_predict,
        },
        "timings_are_descriptive": True,
        "pass": bool(forward_equal and edge_equal),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=400)
    parser.add_argument("--entities", type=int, default=8)
    parser.add_argument("--history-depth", type=int, default=96)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    result = run(args.iterations, args.entities, args.history_depth)
    print(json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
    ))
    if not result["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        _SCRATCH.cleanup()
