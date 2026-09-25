"""Synthetic host benchmark for Stage-7 conservative TinyMLP Offline RL."""
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
from policy_tiny_mlp_offline_rl import (
    offline_rl_gate,
    train_conservative_offline_rl,
)
from policy_tiny_mlp_training import train_supervised


def rss_bytes():
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        return None


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


def observation(feature_ids, x, index):
    # Keep non-signal dimensions neutral. The benchmark is intended to verify
    # conservative reward improvement on a stationary supported distribution, not
    # accidental generalization across synthetic index aliases.
    values = [float(x)] + [0.0] * max(0, len(feature_ids) - 1)
    return {"feature_ids": list(feature_ids), "values": values}


def supervised_rows(feature_ids, count):
    out = []
    for idx in range(int(count)):
        x = -1.0 + 2.0 * (idx % 80) / 79.0
        out.append({
            "observation": observation(feature_ids, x, idx),
            "action_idx": 0 if x < 0.0 else 1,
            "weight": 1.0,
            "timestamp": float(idx),
        })
    return out


def reward_rows(feature_ids, contexts):
    out = []
    ts = 10_000.0
    for idx in range(int(contexts)):
        context_slot = (idx * 17) % 64
        x = -0.95 + 1.9 * context_slot / 63.0
        for action in (0, 1):
            good = (x < 0.0 and action == 0) or (x >= 0.0 and action == 1)
            out.append({
                "observation": observation(feature_ids, x, idx * 2 + action),
                "action_idx": action,
                "action_value": float(action),
                "reward": .8 if good else -.8,
                "weight": .95,
                "timestamp": ts,
            })
            ts += 1.0
    return out


def benchmark(
    *,
    features=96,
    parent_samples=384,
    reward_contexts=128,
    epochs=4,
):
    feature_ids = tuple(f"feature:{idx}" for idx in range(int(features)))
    parent = TinyMLPBackend(
        actions=(0.0, 1.0),
        horizons=(1,),
        feature_ids=feature_ids,
        schema_id="stage7-offline-rl-benchmark-schema",
        mask_id="stage7-offline-rl-benchmark-mask",
        hidden=(32, 16),
        init_seed=1486,
    )
    train_supervised(
        parent,
        supervised_rows(feature_ids, parent_samples),
        max_samples=parent_samples,
        max_epochs=10,
        batch_size=16,
        learning_rate=.03,
        gradient_clip=1.0,
        early_stop_patience=3,
    )
    parent_before = parent.serialize()

    rows = reward_rows(feature_ids, reward_contexts)
    holdout_count = max(16, len(rows) // 4)
    train_rows = rows[:-holdout_count]
    holdout_rows = rows[-holdout_count:]
    manual = [
        {
            "label_id": 1,
            "observation": observation(feature_ids, -.75, 9001),
            "action_idx": 0,
            "weight": 1.0,
        },
        {
            "label_id": 2,
            "observation": observation(feature_ids, .75, 9002),
            "action_idx": 1,
            "weight": 1.0,
        },
    ]

    rss_before = rss_bytes()
    started = time.perf_counter()
    child, trainer = train_conservative_offline_rl(
        parent,
        train_rows,
        manual_samples=manual,
        max_samples=384,
        max_epochs=epochs,
        batch_size=16,
        learning_rate=.0015,
        reward_clip=1.0,
        advantage_clip=1.5,
        kl_beta=2.0,
        parent_l2=.002,
        manual_weight=4.0,
        gradient_clip=.5,
        max_parent_relative_l2=.08,
        min_action_support=4,
        early_stop_patience=2,
        early_stop_min_delta=.0001,
    )
    gate = offline_rl_gate(
        parent,
        child,
        train_rows=train_rows,
        holdout_rows=holdout_rows,
        manual_samples=manual,
        min_total_samples=24,
        min_holdout_samples=8,
        min_supported_actions=2,
        min_action_support=4,
        min_effective_sample_size=4.0,
        min_reward_gain=0.0,
        min_parent_agreement=.80,
        max_mean_tv=.10,
        max_max_tv=.25,
        max_parent_relative_l2=.08,
        max_unsupported_probability_lift=.02,
        max_regression_fraction=.10,
        max_unseen_context_rate=.75,
        reward_clip=1.0,
        context_threshold=1.5,
    )
    wall = time.perf_counter() - started
    rss_after = rss_bytes()

    latency = []
    for row in holdout_rows:
        t0 = time.perf_counter_ns()
        child.predict(row["observation"])
        latency.append((time.perf_counter_ns() - t0) / 1000.0)

    parent_unchanged = parent.serialize() == parent_before
    holdout = gate.get("holdout") or {}
    manual_fit = gate.get("manual_fit_candidate") or {}
    passed = bool(
        trainer.get("trained")
        and gate.get("passed")
        and parent_unchanged
        and float((gate.get("parent_distance") or {}).get("relative_l2") or 0.0) <= .080001
        and float(holdout.get("unsupported_probability_lift_max") or 0.0) <= .020001
        and int(holdout.get("unsupported_new_argmax_count") or 0) == 0
        and float(manual_fit.get("score") or 0.0) >= 1.0
        and wall < 30.0
    )
    return {
        "contract": "tiny_mlp_stage7_offline_rl_benchmark_v1",
        "pass": passed,
        "synthetic": True,
        "architecture": list(child.architecture),
        "parameter_count": child.parameter_count,
        "train_samples": len(train_rows),
        "holdout_samples": len(holdout_rows),
        "manual_samples": len(manual),
        "wall_seconds": wall,
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "rss_delta_bytes": (
            None if rss_before is None or rss_after is None
            else max(0, int(rss_after) - int(rss_before))
        ),
        "parent_unchanged": parent_unchanged,
        "trainer": trainer,
        "gate": gate,
        "inference_us": {
            "p50": percentile(latency, .50),
            "p95": percentile(latency, .95),
            "p99": percentile(latency, .99),
            "mean": statistics.fmean(latency),
        },
        "online_exploration": False,
        "physical_authority": False,
        "behavior_propensity_known": False,
        "environment": {
            "python": sys.version,
            "platform": sys.platform,
            "note": (
                "host-local synthetic timing and logged-action reward proxy; "
                "rerun on Raspberry Pi and real trusted rewards before rollout"
            ),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", type=int, default=96)
    parser.add_argument("--parent-samples", type=int, default=384)
    parser.add_argument("--reward-contexts", type=int, default=128)
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    result = benchmark(
        features=args.features,
        parent_samples=args.parent_samples,
        reward_contexts=args.reward_contexts,
        epochs=args.epochs,
    )
    print(json.dumps(
        result,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
    ))
    if not result["pass"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
