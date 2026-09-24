"""Packaged Stage-5 benchmark for incremental Tiny MLP Manual Correct.

Synthetic host probe only. It measures the incremental child update after a trained
parent already exists; parent historical training time is deliberately excluded from
correct_wall_seconds. Re-run inside the add-on for Raspberry Pi numbers.
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
from policy_tiny_mlp_correct import incremental_correct_finetune
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
    values = [float(x)]
    for col in range(1, len(feature_ids)):
        values.append((((index * (col + 5)) % 19) - 9) * 0.002)
    return {"feature_ids": list(feature_ids), "values": values}


def dataset(feature_ids, count, *, offset=0):
    out = []
    for idx in range(int(count)):
        logical = idx + int(offset)
        x = -1.0 + 2.0 * (logical % 80) / 79.0
        out.append({
            "observation": observation(feature_ids, x, logical),
            "action_idx": 0 if x < 0.0 else 1,
            "weight": 1.0,
            "timestamp": float(logical),
        })
    return out


def benchmark(features=96, parent_samples=384, replay=192, holdout=64, epochs=4):
    feature_ids = tuple(f"feature:{idx}" for idx in range(int(features)))
    parent = TinyMLPBackend(
        actions=(0.0, 1.0),
        horizons=(1,),
        feature_ids=feature_ids,
        schema_id="stage5-correct-benchmark-schema",
        mask_id="stage5-correct-benchmark-mask",
        hidden=(32, 16),
        init_seed=1484,
    )
    train_supervised(
        parent,
        dataset(feature_ids, parent_samples),
        max_samples=parent_samples,
        max_epochs=10,
        batch_size=16,
        learning_rate=.03,
        gradient_clip=1.0,
        early_stop_patience=3,
    )
    parent_checksum = parent.serialize()["model_checksum"]

    correction_rows = []
    for index, x in enumerate((-.75, -.25, .25, .75)):
        correction_rows.append({
            "observation": observation(feature_ids, x, 8000 + index),
            "action_idx": 0 if x < 0.0 else 1,
            "weight": 1.0,
            "timestamp": 8000.0 + index,
            "label_id": index + 1,
        })
    replay_rows = dataset(feature_ids, replay, offset=1000)
    holdout_rows = dataset(feature_ids, holdout, offset=4000)
    nearby = [
        observation(feature_ids, x, 9000 + index)
        for index, x in enumerate((-.8, -.4, .4, .8))
    ]
    agent = {
        "target_property": "power",
        "deadband": .5,
        "min_value": 0,
        "max_value": 1,
    }

    before_rss = rss_bytes()
    started = time.perf_counter()
    child, report = incremental_correct_finetune(
        parent,
        agent=agent,
        correction_samples=correction_rows,
        replay_samples=replay_rows,
        holdout_samples=holdout_rows,
        nearby_observations=nearby,
        correction_fraction=.25,
        max_samples=min(256, replay + 64),
        max_epochs=epochs,
        batch_size=16,
        learning_rate=.003,
        l2=.0001,
        gradient_clip=.5,
        early_stop_patience=2,
        early_stop_min_delta=.0005,
        minimum_holdout_samples=12,
        max_accuracy_regression=.03,
        min_parent_agreement=.85,
        min_nearby_agreement=.75,
        max_regression_fraction=.10,
        max_parent_relative_l2=.20,
        min_correction_fit=.95,
    )
    correct_wall_seconds = time.perf_counter() - started
    after_rss = rss_bytes()

    latency = []
    for row in holdout_rows:
        started_ns = time.perf_counter_ns()
        child.predict(row["observation"])
        latency.append((time.perf_counter_ns() - started_ns) / 1000.0)

    child_raw = child.serialize()
    child_bytes = len(json.dumps(
        child_raw, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8"))
    parent_unchanged = parent.serialize()["model_checksum"] == parent_checksum

    passed = bool(
        report.get("passed")
        and parent_unchanged
        and float((report.get("correction_fit_after") or {}).get("score") or 0.0) >= .95
        and correct_wall_seconds < 30.0
        and child_bytes <= 524288
    )
    return {
        "contract": "tiny_mlp_stage5_manual_correct_benchmark_v1",
        "pass": passed,
        "synthetic": True,
        "architecture": list(child.architecture),
        "parameter_count": child.parameter_count,
        "correction_samples": len(correction_rows),
        "replay_samples": len(replay_rows),
        "holdout_samples": len(holdout_rows),
        "correct_wall_seconds": correct_wall_seconds,
        "rss_before_bytes": before_rss,
        "rss_after_bytes": after_rss,
        "rss_delta_bytes": (
            None if before_rss is None or after_rss is None
            else max(0, int(after_rss) - int(before_rss))
        ),
        "serialized_bytes": child_bytes,
        "parent_unchanged": parent_unchanged,
        "report": report,
        "inference_us": {
            "p50": percentile(latency, .50),
            "p95": percentile(latency, .95),
            "p99": percentile(latency, .99),
            "mean": statistics.fmean(latency),
        },
        "physical_authority": False,
        "online_reward_updates": False,
        "environment": {
            "python": sys.version,
            "platform": sys.platform,
            "note": "host-local synthetic timings; rerun inside add-on for Raspberry Pi measurement",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", type=int, default=96)
    parser.add_argument("--parent-samples", type=int, default=384)
    parser.add_argument("--replay", type=int, default=192)
    parser.add_argument("--holdout", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    result = benchmark(
        features=args.features,
        parent_samples=args.parent_samples,
        replay=args.replay,
        holdout=args.holdout,
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
