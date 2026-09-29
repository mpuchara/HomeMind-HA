#!/usr/bin/env python3
"""Synthetic multicore capacity probe for adaptive dual-agent training.

This does not benchmark HomeMind learning quality or Raspberry Pi wall time. It measures
only the structural benefit of running two independent CPU-heavy agent workers at the
same 85% duty target instead of serializing them.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time


def burn_cpu(cpu_seconds, duty, slice_seconds):
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    slices = 0
    while time.process_time() - cpu_start < cpu_seconds:
        slice_cpu = time.process_time()
        value = 0.0
        while (
            time.process_time() - slice_cpu < slice_seconds
            and time.process_time() - cpu_start < cpu_seconds
        ):
            # Deterministic scalar work approximates the Python-heavy historical replay
            # path and deliberately does not release work to a BLAS thread pool.
            for index in range(256):
                value += math.sin(index * 0.001 + value * 1e-12)
        active = max(0.0, time.process_time() - slice_cpu)
        pause = active * (1.0 - duty) / max(duty, 1e-9)
        if pause > 0:
            time.sleep(pause)
        slices += 1
    cpu = time.process_time() - cpu_start
    wall = time.perf_counter() - wall_start
    return {
        "cpu_seconds": cpu,
        "wall_seconds": wall,
        "duty_observed": cpu / max(wall, 1e-9),
        "slices": slices,
    }


def child_main(args):
    result = burn_cpu(args.cpu_seconds, args.duty, args.slice_seconds)
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


def run_child(args):
    cmd = [
        sys.executable,
        __file__,
        "--child",
        "--cpu-seconds",
        str(args.cpu_seconds),
        "--duty",
        str(args.duty),
        "--slice-seconds",
        str(args.slice_seconds),
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def collect(process):
    out, err = process.communicate(timeout=30)
    if process.returncode != 0:
        raise RuntimeError(err or out)
    return json.loads(out.strip().splitlines()[-1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--cpu-seconds", type=float, default=0.35)
    parser.add_argument("--duty", type=float, default=0.85)
    parser.add_argument("--slice-seconds", type=float, default=0.035)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    args.duty = max(0.10, min(0.90, float(args.duty)))
    args.slice_seconds = max(0.005, min(0.10, float(args.slice_seconds)))
    args.cpu_seconds = max(0.10, min(2.0, float(args.cpu_seconds)))

    if args.child:
        child_main(args)
        return

    sequential_start = time.perf_counter()
    seq_a = collect(run_child(args))
    seq_b = collect(run_child(args))
    sequential_wall = time.perf_counter() - sequential_start

    parallel_start = time.perf_counter()
    proc_a = run_child(args)
    proc_b = run_child(args)
    par_a = collect(proc_a)
    par_b = collect(proc_b)
    parallel_wall = time.perf_counter() - parallel_start

    parallel_cpu = float(par_a["cpu_seconds"]) + float(par_b["cpu_seconds"])
    logical_cpus = max(1, int(os.cpu_count() or 1))
    result = {
        "contract": "adaptive_dual_agent_cpu_capacity_probe_v1",
        "environment": {
            "logical_cpu_count": logical_cpus,
            "note": (
                "synthetic independent Python replay capacity; not Raspberry Pi "
                "Train/Rebuild timing"
            ),
        },
        "duty_target": args.duty,
        "worker_cpu_seconds_target": args.cpu_seconds,
        "sequential": {
            "wall_seconds": sequential_wall,
            "workers": [seq_a, seq_b],
        },
        "parallel": {
            "wall_seconds": parallel_wall,
            "workers": [par_a, par_b],
            "aggregate_cpu_seconds": parallel_cpu,
            "aggregate_one_core_percent": (
                100.0 * parallel_cpu / max(parallel_wall, 1e-9)
            ),
            "estimated_host_cpu_percent": (
                100.0 * parallel_cpu
                / max(parallel_wall * logical_cpus, 1e-9)
            ),
        },
        "throughput_speedup": sequential_wall / max(parallel_wall, 1e-9),
    }
    # On a multicore host, two independent workers should overlap materially. Keep the
    # gate tolerant of noisy shared CI; exact speed is descriptive, not a release claim.
    result["pass"] = bool(
        logical_cpus < 2 or result["throughput_speedup"] >= 1.35
    )
    print(json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
    ))
    if not result["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
