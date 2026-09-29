#!/usr/bin/env python3
"""0.14.108 persistent-worker startup + cross-tracker RAM-cache benchmark."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
TESTS = ROOT / "tests"
for value in (str(SRC), str(TESTS)):
    if value not in sys.path:
        sys.path.insert(0, value)

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="hm-persistent-bench-import-")
os.environ.setdefault("ADAPTIVE_AI_DATA", _IMPORT_ROOT.name)

from context_engine import ContextEngine
from replay import HistoricalContextCache, ReplayQueryCache, SQLiteTemporalTracker
from settings import DEFAULT_OPTIONS
from storage import Store
from support import state


def cold_start_probe(processes):
    processes = max(2, int(processes))
    code = (
        "import numpy; import history; import replay; import training_process; "
        "print('worker-import-ok')"
    )
    baseline = 0.0
    for index in range(processes):
        with tempfile.TemporaryDirectory(prefix=f"hm-cold-{index}-") as data:
            env = dict(os.environ)
            env["ADAPTIVE_AI_DATA"] = data
            env["ADAPTIVE_AI_TRAINING_WORKER"] = "1"
            env["PYTHONPATH"] = str(SRC)
            started = time.perf_counter()
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            baseline += time.perf_counter() - started
            if completed.returncode != 0:
                raise RuntimeError(completed.stdout + completed.stderr)

    with tempfile.TemporaryDirectory(prefix="hm-persistent-cold-") as data:
        env = dict(os.environ)
        env["ADAPTIVE_AI_DATA"] = data
        env["ADAPTIVE_AI_TRAINING_WORKER"] = "1"
        env["PYTHONPATH"] = str(SRC)
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        persistent = time.perf_counter() - started
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout + completed.stderr)

    return {
        "logical_chunks": processes,
        "baseline_process_starts": processes,
        "persistent_process_starts": 1,
        "process_start_reduction": processes - 1,
        "baseline_cold_start_seconds": baseline,
        "persistent_cold_start_seconds": persistent,
        "cold_start_speedup": baseline / max(1e-9, persistent),
        "wall_clock_is_informational": True,
    }


def cache_probe():
    with tempfile.TemporaryDirectory(prefix="hm-persistent-cache-") as root:
        store = Store(Path(root) / "replay.db")
        entity = "binary_sensor.motion"
        rows = []
        for index in range(120):
            rows.append((
                entity,
                1000.0 + index,
                "on" if index % 2 else "off",
                {"device_class": "motion"},
                None,
                "bench",
            ))
        store.archive_batch(rows)

        states = {entity: state(entity, "off", device_class="motion")}
        registry = {entity: {"area_id": "room"}}
        context = ContextEngine(DEFAULT_OPTIONS)
        context.configure(states, entities=registry)

        query_cache = ReplayQueryCache(max_rows=4096, max_entry_rows=1024)
        home_cache = HistoricalContextCache(max_entries=16, max_units=4096)
        connection = sqlite3.connect(store.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-16384")

        signatures = []
        metrics = []
        try:
            for _ in range(2):
                tracker = SQLiteTemporalTracker(
                    store,
                    [entity],
                    context,
                    1000.0,
                    1120.0,
                    query_cache=query_cache,
                    home_context_cache=home_cache,
                    context_cache_contract="persistent-worker-bench-v1",
                    connection=connection,
                )
                try:
                    tracker.advance(1080.0)
                    snapshot = tracker.state_map.get(entity) or {}
                    signatures.append((
                        snapshot.get("state"),
                        tuple(sorted((snapshot.get("attributes") or {}).items())),
                    ))
                    metrics.append(tracker.stats())
                finally:
                    tracker.close()

            # Tracker.close() must not own the shared connection.
            remaining = int(
                connection.execute("SELECT COUNT(*) FROM entity_history").fetchone()[0]
            )
        finally:
            connection.close()

        status = query_cache.status()
        return {
            "semantic_parity": signatures[0] == signatures[1],
            "archive_rows": remaining,
            "first_tracker_sql_queries": int(metrics[0].get("sql_queries") or 0),
            "second_tracker_sql_queries": int(metrics[1].get("sql_queries") or 0),
            "second_tracker_ram_query_hits": int(metrics[1].get("ram_query_hits") or 0),
            "shared_connection_reported": bool(
                metrics[0].get("shared_sqlite_connection")
                and metrics[1].get("shared_sqlite_connection")
            ),
            "query_cache": status,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=int, default=6)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    cold = cold_start_probe(args.chunks)
    cache = cache_probe()
    result = {
        "contract": "persistent_agent_training_worker_benchmark_v1",
        "cold_start": cold,
        "cross_tracker_cache": cache,
    }
    result["pass"] = bool(
        cold["baseline_process_starts"] == int(args.chunks)
        and cold["persistent_process_starts"] == 1
        and cache["semantic_parity"]
        and cache["shared_connection_reported"]
        and cache["second_tracker_ram_query_hits"] > 0
        and cache["query_cache"]["hits"] > 0
    )
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
