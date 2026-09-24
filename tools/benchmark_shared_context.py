#!/usr/bin/env python3
"""Synthetic profile for 0.14.79 shared historical home-context snapshots.

Compares the exact 0.14.78-compatible path (home_context_cache=None) with the new
per-training bounded cache.  Machine wall-clock numbers are informational; CI gates
semantic parity, deterministic render-count reduction and the configured memory bounds.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "ADAPTIVE_AI_DATA",
    tempfile.mkdtemp(prefix="homemind-shared-context-benchmark-"),
)
SRC = ROOT / "adaptive_ai" / "src"
TESTS = ROOT / "tests"
for path in (str(SRC), str(TESTS)):
    if path not in os.sys.path:
        os.sys.path.insert(0, path)

from context_engine import ContextEngine
from replay import HistoricalContextCache, ReplayQueryCache, SQLiteTemporalTracker
from settings import DEFAULT_OPTIONS
from storage import Store
from support import state


def rss_mb():
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:
        return None


def make_fixture(sensor_count, dynamic):
    temp = tempfile.TemporaryDirectory()
    store = Store(Path(temp.name) / "shared-context-bench.db")
    base = 1_702_000_000.0
    states = {}
    registry = {}
    sensors = []
    areas = [f"area_{i}" for i in range(4)]

    for i in range(sensor_count):
        eid = f"binary_sensor.context_{i:03d}"
        dc = "occupancy" if i % 2 else "motion"
        sensors.append(eid)
        states[eid] = state(eid, "off", device_class=dc)
        registry[eid] = {"area_id": areas[i % len(areas)]}

    targets = []
    for i in range(20):
        eid = f"light.target_{i:02d}"
        targets.append(eid)
        states[eid] = state(eid, "off")
        registry[eid] = {"area_id": areas[i % len(areas)]}

    ctx = ContextEngine(DEFAULT_OPTIONS)
    ctx.configure(states, entities=registry)

    rows = []
    for i, eid in enumerate(sensors):
        attrs = {"device_class": "occupancy" if i % 2 else "motion"}
        rows.append((eid, base + 1, "off", attrs, None, "bench"))
        if dynamic:
            value = False
            for step in range(1, 18):
                value = not value
                rows.append((
                    eid,
                    base + 8 + step * 10 + (i % 3) * 0.1,
                    "on" if value else "off",
                    attrs,
                    None,
                    "bench",
                ))
    store.archive_batch(rows)
    return temp, store, ctx, sensors, targets, base


def requests(agent_count, targets, base):
    onset = []
    persistence = []
    for agent_idx in range(agent_count):
        target = targets[agent_idx]
        phase = float(agent_idx % 5) * 1.5
        for step in range(6):
            ts = base + 40 + step * 24 + phase
            onset.append((ts, target))
            # Half of the persistence contexts intentionally coincide with an onset
            # boundary, while the rest are distinct. This models common shared occupancy
            # events without assuming every agent acts at the same instant.
            persistence.append((ts if step % 2 == 0 else ts + 8.0, target))
    onset.sort(key=lambda row: (row[0], row[1]))
    persistence.sort(key=lambda row: (row[0], row[1]))
    return onset, persistence


def forecast_signature(forecast):
    keys = (
        "occupancy_now", "occupancy_in_1s", "occupancy_in_3s",
        "occupancy_in_5s", "arrival_probability", "departure_probability",
        "trajectory_confidence", "known", "area_id",
    )
    out = []
    for key in keys:
        value = forecast.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = round(float(value), 12)
        out.append((key, value))
    return tuple(out)


def run_once(sensor_count, agent_count, dynamic, cache_enabled):
    temp, store, ctx, sensors, targets, base = make_fixture(sensor_count, dynamic)
    query_cache = ReplayQueryCache(max_rows=16384, max_entry_rows=1024)
    home_cache = (
        HistoricalContextCache(max_entries=32, max_units=8192)
        if cache_enabled else None
    )
    contract = "benchmark-policy:11:schema:12:feature:2"
    onset = SQLiteTemporalTracker(
        store, sensors, ctx, base, base + 240,
        query_cache=query_cache,
        home_context_cache=home_cache,
        context_cache_contract=contract,
    )
    persistence = SQLiteTemporalTracker(
        store, sensors, ctx, base, base + 240,
        query_cache=query_cache,
        home_context_cache=home_cache,
        context_cache_contract=contract,
    )
    onset_requests, persistence_requests = requests(agent_count, targets, base)
    signatures = {"onset": [], "persistence": []}

    rss_before = rss_mb()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        for ts, target in onset_requests:
            onset.advance(ts)
            signatures["onset"].append(
                (ts, target, forecast_signature(onset.history.home_context.forecast(target, ts)))
            )
        for ts, target in persistence_requests:
            persistence.advance(ts)
            signatures["persistence"].append(
                (ts, target, forecast_signature(
                    persistence.history.home_context.forecast(target, ts)
                ))
            )
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        onset_stats = onset.stats()
        persistence_stats = persistence.stats()
        onset.close()
        persistence.close()
        cache_status = home_cache.status() if home_cache is not None else {
            "entries": 0, "units": 0, "hits": 0, "misses": 0,
            "evictions": 0, "max_entries": 0, "max_units": 0, "hit_rate": None,
        }
        temp.cleanup()

    return {
        "elapsed_seconds": elapsed,
        "python_peak_mb": peak / (1024 * 1024),
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_mb(),
        "signatures": signatures,
        "onset": onset_stats,
        "persistence": persistence_stats,
        "cache": cache_status,
        "home_rebuild_requests": (
            int(onset_stats["home_rebuilds"]) + int(persistence_stats["home_rebuilds"])
        ),
        "home_render_executes": (
            int(onset_stats["home_render_executes"])
            + int(persistence_stats["home_render_executes"])
        ),
    }


def run_matrix():
    rows = []
    all_parity = True
    reductions = []
    for dynamic in (False, True):
        for sensors in (8, 32, 64):
            for agents in (1, 5, 20):
                baseline = run_once(sensors, agents, dynamic, cache_enabled=False)
                cached = run_once(sensors, agents, dynamic, cache_enabled=True)
                parity = baseline["signatures"] == cached["signatures"]
                all_parity = all_parity and parity
                base_renders = max(1, int(baseline["home_render_executes"]))
                reduction = 1.0 - int(cached["home_render_executes"]) / base_renders
                reductions.append(reduction)
                rows.append({
                    "history": "dynamic" if dynamic else "static",
                    "sensors": sensors,
                    "agents": agents,
                    "parity": parity,
                    "baseline_seconds": round(baseline["elapsed_seconds"], 4),
                    "cached_seconds": round(cached["elapsed_seconds"], 4),
                    "wall_speedup": round(
                        baseline["elapsed_seconds"] / max(cached["elapsed_seconds"], 1e-9),
                        3,
                    ),
                    "baseline_home_renders": baseline["home_render_executes"],
                    "cached_home_renders": cached["home_render_executes"],
                    "render_reduction": round(reduction, 4),
                    "cache_hits": cached["cache"]["hits"],
                    "cache_misses": cached["cache"]["misses"],
                    "cache_hit_rate": cached["cache"]["hit_rate"],
                    "cache_entries": cached["cache"]["entries"],
                    "cache_units": cached["cache"]["units"],
                    "cache_evictions": cached["cache"]["evictions"],
                    "cached_python_peak_mb": round(cached["python_peak_mb"], 3),
                    "cached_rss_after_mb": (
                        None if cached["rss_after_mb"] is None
                        else round(cached["rss_after_mb"], 3)
                    ),
                })

    representative = next(
        row for row in rows
        if row["history"] == "dynamic" and row["sensors"] == 64 and row["agents"] == 20
    )
    avg_reduction = sum(reductions) / max(1, len(reductions))
    bounded = all(
        row["cache_entries"] <= 32 and row["cache_units"] <= 8192
        for row in rows
    )
    result = {
        "contract": "shared_historical_context_profile_v1",
        "matrix": rows,
        "summary": {
            "all_semantic_parity": all_parity,
            "average_render_reduction": round(avg_reduction, 4),
            "representative_dynamic_20_agents_64_sensors": representative,
            "cache_bounds_respected": bounded,
            "wall_clock_is_informational": True,
            "rss_note": "RSS is process-level current RSS; tracemalloc reports Python allocations for each measured replay run.",
        },
    }
    result["pass"] = bool(
        all_parity
        and bounded
        and representative["render_reduction"] >= 0.15
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = run_matrix()
    print(json.dumps(
        result, ensure_ascii=False,
        indent=None if args.compact else 2,
        sort_keys=True,
    ))
    if not result["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
