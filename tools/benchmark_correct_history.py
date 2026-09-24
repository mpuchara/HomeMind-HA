#!/usr/bin/env python3
"""Deterministic Correct-history scaling benchmark for the 0.14.76 hotfix.

Wall-clock numbers are reported for engineering context only.  The pass/fail gate uses
semantic parity plus deterministic work counters so CI does not depend on runner speed.
"""
from __future__ import annotations

import argparse
import bisect
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_correct_generation_history import _live_observed_points
from teach_observed_history import DESIRED_STALE_SECONDS


def legacy_live_points(decisions, currents, start, end):
    start = float(start)
    end = float(end)
    times = {start, end}
    times.update(float(row["ts"]) for row in decisions)
    times.update(float(row["ts"]) for row in currents)
    for row in decisions:
        cutoff = float(row["ts"]) + float(DESIRED_STALE_SECONDS) + 1e-4
        if start <= cutoff <= end:
            times.add(cutoff)

    points = []
    gaps = []
    gap_start = None
    for ts in sorted(times):
        if decisions:
            dts = [float(row["ts"]) for row in decisions]
            index = bisect.bisect_right(dts, ts) - 1
            if index < 0 or ts - float(decisions[index]["ts"]) > DESIRED_STALE_SECONDS:
                desired = None
            else:
                value = decisions[index].get("desired")
                desired = None if value is None else float(value)
        else:
            desired = None

        if currents:
            cts = [float(row["ts"]) for row in currents]
            index = bisect.bisect_right(cts, ts) - 1
            current = None if index < 0 else currents[index].get("current")
        else:
            current = None

        points.append({"ts": ts, "current": current, "desired": desired})
        if desired is None and gap_start is None:
            gap_start = ts
        elif desired is not None and gap_start is not None:
            gaps.append({"start": gap_start, "end": ts})
            gap_start = None
    if gap_start is not None:
        gaps.append({"start": gap_start, "end": end})
    return points, gaps


def timed(fn):
    started = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - started


def parse_sizes(raw):
    values = [int(part.strip()) for part in str(raw).split(",") if part.strip()]
    if not values or any(value <= 0 for value in values):
        raise ValueError("sizes must contain positive integers")
    return values


def run(sizes):
    samples = []
    for n in sizes:
        decisions = [{"ts": float(i * 30), "desired": float(i % 2)} for i in range(n)]
        currents = [{"ts": float(i * 30 + 1), "current": float(i % 2)} for i in range(n)]
        start, end = 0.0, float(n * 30)

        legacy, legacy_seconds = timed(
            lambda: legacy_live_points(decisions, currents, start, end)
        )
        stats = {}
        optimized, optimized_seconds = timed(
            lambda: _live_observed_points(decisions, currents, start, end, stats=stats)
        )
        if optimized != legacy:
            raise AssertionError(f"semantic mismatch at {n} rows/stream")

        payload = {"points": optimized[0], "gaps": optimized[1]}
        json_bytes = len(
            json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        )
        legacy_work = len(optimized[0]) * (len(decisions) + len(currents))
        sample = {
            "rows_per_stream": n,
            "points": len(optimized[0]),
            "gaps": len(optimized[1]),
            "json_bytes": json_bytes,
            "legacy_seconds": legacy_seconds,
            "optimized_seconds": optimized_seconds,
            "wall_speedup": (
                None if optimized_seconds <= 0 else legacy_seconds / optimized_seconds
            ),
            "legacy_timestamp_scan_cells": legacy_work,
            "optimized_lookup_work": stats["lookup_work"],
            "decision_advances": stats["decision_advances"],
            "current_advances": stats["current_advances"],
            "identical": True,
        }
        if sample["optimized_lookup_work"] >= max(1, legacy_work // 50):
            raise AssertionError(f"optimized work did not fall enough at {n}: {sample}")
        samples.append(sample)

    growth = []
    for left, right in zip(samples, samples[1:]):
        optimized_ratio = right["optimized_lookup_work"] / left["optimized_lookup_work"]
        legacy_ratio = right["legacy_timestamp_scan_cells"] / left["legacy_timestamp_scan_cells"]
        growth.append({
            "from": left["rows_per_stream"],
            "to": right["rows_per_stream"],
            "optimized_work_ratio": optimized_ratio,
            "legacy_work_ratio": legacy_ratio,
        })
        if optimized_ratio >= 2.2:
            raise AssertionError(f"optimized work is not linear enough: {growth[-1]}")
        if legacy_ratio <= 3.5:
            raise AssertionError(f"legacy oracle no longer demonstrates quadratic growth: {growth[-1]}")
    return {
        "contract": "correct_history_observed_stream_merge_v1",
        "sizes": sizes,
        "samples": samples,
        "growth": growth,
        "pass": True,
        "timing_note": "wall times are informational; CI gates semantic parity and deterministic work scaling",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="1000,2000,4000,8000")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    report = run(parse_sizes(args.sizes))
    print(json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2))


if __name__ == "__main__":
    main()
