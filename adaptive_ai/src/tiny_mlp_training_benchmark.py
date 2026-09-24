"""Packaged Stage-4 benchmark for bounded offline Tiny MLP supervised training.

Synthetic only: no Home Assistant connection, no ActionIntent, no Executor.  The same
script can be executed inside the add-on on Raspberry Pi to measure target hardware.
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
from policy_tiny_mlp_training import evaluate_supervised, train_supervised


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
    # One clear causal driver plus deterministic low-amplitude nuisance dimensions.
    values = [float(x)]
    for col in range(1, len(feature_ids)):
        values.append((((index * (col + 3)) % 17) - 8) * 0.002)
    return {"feature_ids": list(feature_ids), "values": values}


def dataset(feature_ids, count, *, offset=0):
    rows = []
    for idx in range(int(count)):
        logical = idx + int(offset)
        x = -1.0 + 2.0 * (logical % 64) / 63.0
        rows.append({
            "observation": observation(feature_ids, x, logical),
            "action_idx": 0 if x < 0.0 else 1,
            "weight": 1.0,
            "timestamp": float(logical),
        })
    return rows


def benchmark(samples=384, holdout=160, epochs=8, features=96):
    feature_ids = tuple(f"feature:{idx}" for idx in range(int(features)))
    model = TinyMLPBackend(
        actions=(0.0, 1.0),
        horizons=(1,),
        feature_ids=feature_ids,
        schema_id="stage4-training-benchmark-schema",
        mask_id="stage4-training-benchmark-mask",
        hidden=(32, 16),
        init_seed=1483,
    )
    train_rows = dataset(feature_ids, samples)
    holdout_rows = dataset(feature_ids, holdout, offset=4096)
    agent = {
        "target_property": "power",
        "deadband": .5,
        "min_value": 0,
        "max_value": 1,
    }

    before_rss = rss_bytes()
    started = time.perf_counter()
    trainer = train_supervised(
        model,
        train_rows,
        max_samples=samples,
        max_epochs=epochs,
        batch_size=16,
        learning_rate=.03,
        l2=1e-4,
        gradient_clip=1.0,
        early_stop_patience=3,
        early_stop_min_delta=1e-3,
    )
    wall_seconds = time.perf_counter() - started
    after_rss = rss_bytes()

    metrics = evaluate_supervised(model, agent, holdout_rows)
    raw = model.serialize()
    encoded = json.dumps(
        raw, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")

    latency = []
    for row in holdout_rows:
        started = time.perf_counter_ns()
        model.predict(row["observation"])
        latency.append((time.perf_counter_ns() - started) / 1000.0)

    passed = bool(
        trainer.get("trained")
        and float(metrics.get("score") or 0.0) >= .85
        and model.parameter_count <= 50000
        and len(encoded) <= 524288
    )
    return {
        "contract": "tiny_mlp_stage4_training_benchmark_v1",
        "pass": passed,
        "synthetic": True,
        "architecture": list(model.architecture),
        "parameter_count": model.parameter_count,
        "train_samples": len(train_rows),
        "holdout_samples": len(holdout_rows),
        "trainer": trainer,
        "holdout": metrics,
        "training_wall_seconds": wall_seconds,
        "serialized_bytes": len(encoded),
        "rss_before_bytes": before_rss,
        "rss_after_bytes": after_rss,
        "rss_delta_bytes": (
            None if before_rss is None or after_rss is None
            else max(0, int(after_rss) - int(before_rss))
        ),
        "trained_inference_us": {
            "p50": percentile(latency, .50),
            "p95": percentile(latency, .95),
            "p99": percentile(latency, .99),
            "mean": statistics.fmean(latency),
        },
        "online_reward_updates": False,
        "physical_authority": False,
        "environment": {
            "python": sys.version,
            "platform": sys.platform,
            "note": "host-local timings; rerun inside add-on for Raspberry Pi measurement",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=384)
    parser.add_argument("--holdout", type=int, default=160)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--features", type=int, default=96)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    result = benchmark(args.samples, args.holdout, args.epochs, args.features)
    print(json.dumps(
        result,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
    ))
    if not result["pass"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
