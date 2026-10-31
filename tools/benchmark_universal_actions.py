#!/usr/bin/env python3
"""Synthetic U04 dynamic action-scorer benchmark (never a release gate)."""
from pathlib import Path
import argparse
import json
import statistics
import sys
import time

SRC = Path(__file__).resolve().parents[1] / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from universal_actions import from_device_agent_mask
from universal_action_scorer import UniversalActionScorer
from universal_neural_encoder import IncrementalNeuralHome


def percentile(values, p):
    return sorted(values)[min(len(values)-1, int((len(values)-1)*p/100.0))] if values else 0.0


def benchmark(device_count, candidates_per_device, passes):
    scorer = UniversalActionScorer()
    home = IncrementalNeuralHome()
    action_sets = []
    for idx in range(device_count):
        eid = f"select.profile_{idx}"
        options = [f"profile_{i}" for i in range(candidates_per_device)]
        agent = {"id": f"a{idx}", "target_entity": eid,
                 "target_property": "option_index",
                 "min_value": 0.0, "max_value": float(candidates_per_device-1)}
        descriptor = {
            "logical_device_id": f"d{idx}", "device_property": "option_index",
            "physical_target_property": "option_index",
            "area_id": f"room_{idx % 16}",
            "resource_keys": [f"device:d{idx}"],
            "properties": [{"entity_id": eid, "property": "option_index"}],
            "autonomy_allowed": True,
        }
        state = {"entity_id": eid, "state": options[0], "attributes": {"options": options}}
        mask = {"actions": [
            {"requested": value, "value": value, "legal": True, "reason": None}
            for value in range(candidates_per_device)
        ], "fallback": "policy_choice_within_mask"}
        action_sets.append(from_device_agent_mask(
            agent, descriptor, mask, state, mask_timestamp=1.0,
        ))
    durations = []
    for _ in range(passes):
        start = time.perf_counter_ns()
        for action_set in action_sets:
            result = scorer.evaluate(action_set, home)
            if result["evaluated"] != candidates_per_device or result["control_eligible"]:
                raise AssertionError("observer contract changed unexpectedly")
        durations.append((time.perf_counter_ns() - start) / 1_000_000.0)
    return {
        "devices": device_count, "candidates_per_device": candidates_per_device,
        "passes": passes, "parameter_count": scorer.parameter_count,
        "full_scan_ms_p50": round(statistics.median(durations), 4),
        "full_scan_ms_p95": round(percentile(durations, 95), 4),
        "note": "deliberate all-device synthetic scoring, not event-routed Pi4 latency",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", default="2,20,100")
    parser.add_argument("--actions", default="2,16")
    parser.add_argument("--passes", type=int, default=5)
    args = parser.parse_args()
    devices = [int(x) for x in args.devices.split(",")]
    actions = [int(x) for x in args.actions.split(",")]
    if not devices or not actions or min(devices) <= 0 or min(actions) <= 0 or args.passes < 1:
        parser.error("positive devices, actions and passes required")
    print(json.dumps({
        "benchmark": "universal_action_scorer_u04a",
        "results": [benchmark(d, a, args.passes) for d in devices for a in actions],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
