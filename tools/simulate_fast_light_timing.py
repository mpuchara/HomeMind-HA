#!/usr/bin/env python3
"""Deterministic release simulation for the 0.12.1 fast-light objective.

This simulator is deliberately small: it exercises the production reward semantics and
the exact Tournament timing metric.  It demonstrates that copying the automation is no
longer the optimum: safe earlier ON and safe earlier OFF score higher, while false ON
and premature OFF are strongly negative.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adaptive_ai/src"))

from rewards import RewardEngine
from fast_light_objective import TIMING_METRIC_MODE, timing_metric_row_factory
import context_tournament_metrics as metrics


def run():
    reward = RewardEngine()

    # Existing automation: ON occurs 1.8 s after the useful precursor; RL would turn ON
    # 1.3 s earlier than that.  OFF automation waits 45 s after vacancy; RL can safely
    # request OFF 35 s earlier.
    on = reward.evaluate(timing_direction="on", baseline_delay=1.3, timing_window=8)
    off = reward.evaluate(timing_direction="off", baseline_delay=35.0, timing_window=120)
    false_on = reward.evaluate(timing_direction="on", false_timing=True)
    premature_off = reward.evaluate(timing_direction="off", premature_off=True)

    metric = timing_metric_row_factory(metrics.metric_row)
    model = {
        "metric_mode": TIMING_METRIC_MODE,
        "timing_samples": 60,
        # Active policy mostly copies the baseline. The challenger adds useful precursor
        # timing, so its normalized timing utility is materially larger.
        "timing_active_utility_sum": 6.0,
        "timing_shadow_utility_sum": 24.0,
        "timing_active_failures": 1,
        "timing_shadow_failures": 2,
        # Same-instant accuracy remains healthy and acts only as a safety constraint.
        "samples": 60,
        "class_totals": [30, 30],
        "active_correct_by_class": [29, 29],
        "shadow_correct_by_class": [29, 29],
    }
    tournament = metric(model, [0.0, 1.0])

    result = {
        "automation_baseline": {
            "on_delay_seconds": 1.8,
            "off_delay_seconds": 45.0,
        },
        "rl_shadow": {
            "on_lead_seconds": 1.3,
            "off_saved_seconds": 35.0,
            "confirmed_on_reward": on.value,
            "confirmed_off_reward": off.value,
            "false_on_reward": false_on.value,
            "premature_off_reward": premature_off.value,
        },
        "sensor_tournament": tournament,
    }

    assert on.value > 0
    assert off.value > 0
    assert false_on.value < 0
    assert premature_off.value == -1.0
    assert tournament["metric"] == "fast_timing_utility"
    assert tournament["gain"] > 0.03
    assert tournament["safety_ok"] is True
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
