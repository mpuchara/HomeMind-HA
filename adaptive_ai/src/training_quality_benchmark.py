"""Independent synthetic preference rollouts for the shipped Ridge/evidence math.

This is a component experiment, not a measurement of a real household or Executor.
Training days and test days are disjoint. Test truth is generated from resident need,
not from automation actions. Each controller maintains its own light state/timer.
"""
import argparse
import json
import random

from policy import DiagonalLinUCB
from training_evidence import evidence_weight_for
from training_quality import ConditionalPatternMemory, light_dwell_reward


def features(motion, radar, manual):
    # Match the shipped centred ON=+1/OFF=-1 categorical representation.
    result = {0: 1., 1: 1. if motion else -1., 2: 0. if radar is None else (1. if radar else -1.),
              3: 1. if manual else -1., 4: 1. if radar is None else -1.}
    # ExplicitFeatureSchema also supplies pairwise interactions of selected sensors.
    from itertools import combinations
    for index, (left, right) in enumerate(combinations(range(1, 5), 2), 5):
        result[index] = result[left] * result[right]
    return result


def sensing(motion, radar):
    return {"active": (["motion"] if motion else []) + (["radar"] if radar else []),
            "reliable": [] if radar is None else ["radar"],
            "absent": ["radar"] if radar is False else []}


def train(seed, min_days=3):
    rng = random.Random(seed)
    head = DiagonalLinUCB(11, [0., 1.])
    memory = ConditionalPatternMemory(2, min_days=min_days)
    base = head.last_decay_ts - 20 * 86400
    contexts = [(False, False, False), (True, True, False), (False, True, False),
                (True, False, False), (False, False, True), (True, None, False), (False, None, False)]
    corrections = 0
    for day in range(12):
        order = list(contexts)
        rng.shuffle(order)
        for index, (motion, radar, manual) in enumerate(order):
            ts = base + day * 86400 + index * 300
            signature = [("motion", motion), ("radar", radar), ("manual", manual)]
            logged = int(motion)
            # A periodically stuck automation adds a false activation to vacancy.
            if not motion and radar is False and not manual and day % 3 == 0:
                logged = 1
            snapshot = sensing(motion, radar)
            reward, _ = light_dwell_reward(logged, .8, snapshot, snapshot, False)
            if reward:
                factor = memory.factor(signature, logged, ts, "automation") if reward > 0 else 1
                head.update(logged, features(motion, radar, manual), reward, ts,
                            evidence_weight=evidence_weight_for("automation", "onset") * factor)
                memory.observe(signature, logged, ts, ts + 120, reward)
            # Sparse explicit corrections are collected only on training days.
            desired = int(bool(radar or manual or (radar is None and motion)))
            if desired != logged and day % 3 == 0:
                head.update(logged, features(motion, radar, manual), -1., ts,
                            sample_mass=4.,
                            evidence_weight=evidence_weight_for("manual_feedback", "onset"))
                head.update(desired, features(motion, radar, manual), 1., ts,
                            sample_mass=4.,
                            evidence_weight=evidence_weight_for("manual_feedback", "onset"))
                corrections += 1
    return head, memory.status(), corrections


def rollout(head, scenario, baseline):
    metrics = {"waiting_seconds": 0, "unnecessary_on_seconds": 0,
               "premature_off_seconds": 0, "switches": 0, "false_activations": 0}
    light, last_motion, on_due = False, -1000, None
    for second in range(90):
        needed = 10 <= second < 55
        motion = 10 <= second < 15
        radar = needed
        manual = False
        if scenario == "false_motion":
            needed, radar, motion = False, False, 10 <= second < 15
        elif scenario == "rare_manual_need":
            motion, radar, manual = False, False, needed
        elif scenario == "sensor_failure":
            radar, motion = None, needed
        elif scenario == "late_entry":
            needed, radar, motion = 25 <= second < 65, 25 <= second < 65, 25 <= second < 30
        if baseline:
            if motion:
                last_motion = second
                if not light and on_due is None:
                    on_due = second + 3
            desired = light
            if on_due is not None and second >= on_due:
                desired, on_due = True, None
            if second - last_motion >= 20:
                desired = False
        else:
            desired = bool(max(head.evaluate(features(motion, radar, manual)), key=lambda arm: arm["mean"])["index"])
        if desired != light:
            metrics["switches"] += 1
            metrics["false_activations"] += int(desired and not needed)
        light = desired
        metrics["waiting_seconds"] += int(needed and not light)
        metrics["premature_off_seconds"] += int(needed and not light and second > (25 if scenario == "late_entry" else 10))
        metrics["unnecessary_on_seconds"] += int(not needed and light)
    return metrics


def run():
    seeds, scenarios = [11, 23, 37], ["entry_still_exit", "false_motion", "rare_manual_need", "sensor_failure", "late_entry"]
    trials, paired = [], []
    for seed in seeds:
        head, profile, corrections = train(seed)
        for scenario in scenarios:
            baseline, agent = rollout(head, scenario, True), rollout(head, scenario, False)
            # Evaluation cost is deliberately independent of historical imitation score.
            cost = lambda row: row["waiting_seconds"] + row["unnecessary_on_seconds"] + 3 * row["premature_off_seconds"]
            gain = cost(baseline) - cost(agent)
            paired.append(gain)
            trials.append({"seed": seed, "scenario": scenario, "baseline": baseline, "agent": agent, "cost_gain": gain})
    passed = all(row["agent"]["premature_off_seconds"] <= row["baseline"]["premature_off_seconds"]
                 and row["agent"]["false_activations"] <= row["baseline"]["false_activations"]
                 and row["cost_gain"] >= 0 for row in trials) and sum(paired) > 0
    return {"contract": "independent_preference_rollouts_v1", "synthetic": True,
            "scope": "production Ridge and quality math; not full runtime/Executor or household evidence",
            "training_days": 12, "test_days": 5, "seeds": seeds, "trials": trials,
            "paired_cost_gain": {"minimum": min(paired), "mean": sum(paired) / len(paired), "pairs": len(paired)},
            "pattern_profile": profile, "training_corrections": corrections, "pass": passed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    report = run()
    print(json.dumps(report, indent=None if args.compact else 2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
