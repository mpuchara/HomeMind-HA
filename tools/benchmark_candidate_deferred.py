#!/usr/bin/env python3
"""ETAP-2 benchmark for Candidate Shadow critical-path isolation.

Absolute timings are descriptive. CI asserts execution topology:
- active Candidate enqueue does not execute Candidate backend,
- one latest job per root is retained,
- repeated events coalesce,
- bounded queue does not drop in the nominal 8-root fixture,
- drain executes exactly the latest job for each root.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from candidate_shadow_deferred import DeferredCandidateShadowQueue
from inference_hot_path_metrics import InferenceHotPathMetrics


def percentile(values, q):
    rows = sorted(float(x) for x in values)
    if not rows:
        return None
    pos = (len(rows) - 1) * float(q)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return rows[lo]
    return rows[lo] + (rows[hi] - rows[lo]) * (pos - lo)


def summary(values):
    rows = [float(x) for x in values]
    return {
        "count": len(rows),
        "p50_us": percentile(rows, .50),
        "p95_us": percentile(rows, .95),
        "p99_us": percentile(rows, .99),
        "max_us": max(rows) if rows else None,
        "mean_us": statistics.fmean(rows) if rows else None,
    }


class Wake:
    def __init__(self):
        self.calls = 0
    def set(self):
        self.calls += 1


def run(iterations=800, roots=8):
    iterations = max(32, int(iterations))
    roots = max(1, min(24, int(roots)))
    engine = SimpleNamespace(
        temporal_history=SimpleNamespace(home_context=None),
        _inference_tls=threading.local(),
        inference_hot_path_metrics=InferenceHotPathMetrics(),
    )
    store = SimpleNamespace(
        _provenance_generation_revision=1,
        event=lambda *args, **kwargs: None,
    )
    wake = Wake()
    manager = SimpleNamespace(engine=engine, store=store, wake_event=wake)
    executed = []

    queue = DeferredCandidateShadowQueue(manager, limit=max(roots + 2, 12))
    manager.candidate_shadow_temporal = queue.current_temporal
    manager.candidate_shadow_home_provider = queue.current_home_provider
    manager.candidate_shadow_current_job = queue.current_job

    def execute(job):
        executed.append((
            str(job["root_agent_id"]),
            int(job["state_revision"]),
            float(job["context_ts"]),
        ))
        return {"ok": True}

    manager.execute_candidate_shadow_job = execute

    off = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        # Candidate-off path has no Candidate post-Live work.
        _ = None
        off.append((time.perf_counter_ns() - started) / 1000.0)

    active = []
    expected_latest = {}
    for idx in range(iterations):
        root = f"root-{idx % roots}"
        revision = idx + 1
        context_ts = 1_700_000_000.0 + idx / 1000.0
        job = {
            "root_agent_id": root,
            "state_map": {"light.target": {"state": "off"}},
            "context_ts": context_ts,
            "inference_ts": context_ts,
            "state_revision": revision,
            "entity_revisions": {"light.target": revision},
            "context_revision": revision,
            "generation_revision": 1,
            "home_forecast_captured": True,
            "home_forecast": {"known": True, "occupancy_now": .5},
            "parent_observation": {
                "desired": 0.0,
                "confidence": .9,
                "model_revision": "root",
                "schema_revision": "12",
            },
        }
        started = time.perf_counter_ns()
        queue.enqueue(job)
        active.append((time.perf_counter_ns() - started) / 1000.0)
        expected_latest[root] = (revision, context_ts)

    executor_calls_before_drain = len(executed)
    before = queue.diagnostics()
    drained = queue.drain(max_roots=roots + 4)
    after = queue.diagnostics()

    actual_latest = {
        root: (revision, context_ts)
        for root, revision, context_ts in executed
    }
    expected_coalesced = iterations - roots
    stable = {
        "executor_calls_before_drain": executor_calls_before_drain,
        "expected_executor_calls_before_drain": 0,
        "queue_depth_before_drain": before["queue_depth"],
        "expected_queue_depth_before_drain": roots,
        "coalesced": before["coalesced"],
        "expected_coalesced": expected_coalesced,
        "dropped": before["dropped"],
        "expected_dropped": 0,
        "drained": drained,
        "expected_drained": roots,
        "latest_revision_parity": actual_latest == expected_latest,
        "wake_calls": wake.calls,
    }
    off_summary = summary(off)
    active_summary = summary(active)
    return {
        "contract": "candidate_shadow_deferred_etap2_v1",
        "iterations": iterations,
        "roots": roots,
        "candidate_off_critical_path": off_summary,
        "candidate_active_deferred_enqueue": active_summary,
        "descriptive_p95_ratio_active_vs_off": (
            active_summary["p95_us"] / max(off_summary["p95_us"], 1e-9)
        ),
        "worker": {
            "executor_calls_after_drain": len(executed),
            "diagnostics_after_drain": after,
        },
        "stable_assertions": stable,
        "pass": bool(
            executor_calls_before_drain == 0
            and before["queue_depth"] == roots
            and before["coalesced"] == expected_coalesced
            and before["dropped"] == 0
            and drained == roots
            and actual_latest == expected_latest
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=800)
    parser.add_argument("--roots", type=int, default=8)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    result = run(args.iterations, args.roots)
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
    main()
