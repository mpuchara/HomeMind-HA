#!/usr/bin/env python3
"""Stage-2 observation-space scale and realtime isolation benchmark."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import policy as policy_module
from context import TemporalHistory
from observation_space import (
    ENTITY_DESCRIPTORS,
    GLOBAL_FEATURES,
    HOME_FEATURES,
    select_observation_mask,
)
from policy import MultiHorizonPolicy


def state(eid, value="off", **attrs):
    return {"entity_id": eid, "state": str(value), "attributes": attrs}


def agent():
    return {
        "id": "stage2-bench",
        "name": "Stage 2 benchmark",
        "target_entity": "light.target",
        "target_property": "power",
        "min_value": 0,
        "max_value": 1,
        "deadband": .5,
        "input_entities": ["*"],
        "enabled": True,
        "confidence_threshold": .78,
        "action_interval": 30,
        "micro_exploration": False,
        "exploration_step": 1,
        "mode": "shadow",
        "training_state": "qualified",
    }


def percentile(values, q):
    values = sorted(float(v) for v in values)
    if not values:
        return None
    pos = (len(values) - 1) * float(q)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def fixture(entity_count):
    states = {"light.target": state("light.target", "off")}
    registry = {
        "light.target": {"area_id": "room_0", "device_id": "target-device"}
    }
    for idx in range(entity_count):
        eid = f"binary_sensor.context_{idx:04d}"
        states[eid] = state(
            eid, "on" if idx % 7 == 0 else "off",
            device_class="motion" if idx % 2 else "occupancy",
            friendly_name=f"Context {idx}",
        )
        registry[eid] = {
            "area_id": f"room_{idx % 12}",
            "device_id": f"sensor-{idx}",
        }
    hints = [f"binary_sensor.context_{idx:04d}" for idx in range(min(8, entity_count))]
    relevance = {
        f"binary_sensor.context_{idx:04d}": max(0.0, 1.0 - idx / 100.0)
        for idx in range(min(100, entity_count))
    }
    return states, registry, hints, relevance


def hot_sample(policy, states, temporal, iterations):
    durations = []
    semantic = None
    for _ in range(iterations):
        started = time.perf_counter_ns()
        features, labels, meta = policy.features(states, temporal, at_ts=1_700_500_100.0)
        decision = policy.predict(features)
        durations.append((time.perf_counter_ns() - started) / 1000.0)
        signature = (
            tuple(sorted(features.items())),
            tuple((k, tuple(v)) for k, v in sorted(labels.items())),
            tuple(round(float(x), 12) if isinstance(x, (int, float)) else x for x in decision[0].values()),
            round(float(decision[1]), 12),
        )
        if semantic is None:
            semantic = signature
        elif semantic != signature:
            raise AssertionError("active Ridge inference became nondeterministic")
    return {
        "p50_us": percentile(durations, .50),
        "p95_us": percentile(durations, .95),
        "p99_us": percentile(durations, .99),
        "semantic": semantic,
    }


def run(entity_count=775, iterations=400):
    states, registry, hints, relevance = fixture(entity_count)
    a = agent()
    p = MultiHorizonPolicy(a, states, registry, hints, relevance_scores=relevance)
    temporal = TemporalHistory(maxlen=96)
    ts = 1_700_500_000.0
    for eid, st in states.items():
        if eid != a["target_entity"]:
            temporal.add(eid, ts, st)

    selector_calls = {"count": 0}
    original = policy_module.select_observation_mask

    def counted(*args, **kwargs):
        selector_calls["count"] += 1
        return original(*args, **kwargs)

    policy_module.select_observation_mask = counted
    try:
        before = hot_sample(p, states, temporal, iterations)
    finally:
        policy_module.select_observation_mask = original

    materialize_ms = []
    first_mask = None
    first_diag = None
    for _ in range(5):
        started = time.perf_counter_ns()
        mask, diag = select_observation_mask(
            a, states, registry, hints, relevance_scores=relevance
        )
        materialize_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
        first_mask = first_mask or mask
        first_diag = first_diag or diag
        if mask.mask_id != first_mask.mask_id:
            raise AssertionError("selector is not deterministic")

    p.observation_mask = first_mask
    p.observation_diagnostics = dict(first_diag, status="ready")
    after = hot_sample(p, states, temporal, iterations)

    expected_global = (
        entity_count * len(ENTITY_DESCRIPTORS)
        + len(GLOBAL_FEATURES)
        + len(HOME_FEATURES)
    )
    p95_ratio = float(after["p95_us"]) / max(float(before["p95_us"]), 1e-9)
    result = {
        "contract": "observation_space_stage2_v1",
        "entity_count": entity_count,
        "descriptors_per_entity": len(ENTITY_DESCRIPTORS),
        "expected_global_feature_count": expected_global,
        "global_feature_count": first_diag["global_feature_count"],
        "selected_feature_count": first_diag["selected_feature_count"],
        "schema_id": first_diag["schema_id"],
        "mask_id": first_diag["mask_id"],
        "selector_materialization_ms": {
            "p50": percentile(materialize_ms, .50),
            "p95": percentile(materialize_ms, .95),
            "max": max(materialize_ms),
        },
        "hot_path": {
            "selector_calls": selector_calls["count"],
            "before_materialization": {
                k: v for k, v in before.items() if k != "semantic"
            },
            "after_materialization": {
                k: v for k, v in after.items() if k != "semantic"
            },
            "p95_ratio": p95_ratio,
            "investigate_if_ratio_above": 1.10,
            "semantic_parity": before["semantic"] == after["semantic"],
        },
        "pass": bool(
            first_diag["global_feature_count"] == expected_global
            and 32 <= first_diag["selected_feature_count"] <= 128
            and selector_calls["count"] == 0
            and before["semantic"] == after["semantic"]
        ),
    }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entities", type=int, default=775)
    parser.add_argument("--iterations", type=int, default=400)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = run(args.entities, args.iterations)
    print(json.dumps(
        result, ensure_ascii=False, sort_keys=True,
        indent=None if args.compact else 2,
    ))
    if not result["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
