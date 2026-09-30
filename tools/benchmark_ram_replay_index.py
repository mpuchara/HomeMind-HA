#!/usr/bin/env python3
"""Synthetic component benchmark for the 0.14.111 RAM replay transport.

This measures SQLiteTemporalTracker archive transport only.  It is not a Raspberry Pi
end-to-end Train/Rebuild benchmark and does not change rewards, samples or model updates.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
sys.path.insert(0, str(SRC))


def context_stub():
    return SimpleNamespace(options={}, relevant_entities=lambda: [])


def signature(rows):
    return [
        (
            int(row["id"]),
            str(row["entity_id"]),
            float(row["ts"]),
            None if row["received_ts"] is None else float(row["received_ts"]),
            row["state"],
            row["attributes_json"],
        )
        for row in rows
    ]


def workload(tracker, entities, timestamps):
    started = time.perf_counter()
    checksum = 0
    parity_samples = []
    previous = timestamps[0] - 1.0
    for idx, ts in enumerate(timestamps):
        before = tracker._base_bulk_before(entities, ts, 64)
        delta = tracker._base_interval_rows(
            entities, previous, ts, per_entity_limit=64
        )
        checksum += len(before) + len(delta)
        if idx in (0, len(timestamps) // 2, len(timestamps) - 1):
            parity_samples.append((signature(before), signature(delta)))
        previous = ts
    return {
        "seconds": time.perf_counter() - started,
        "checksum": checksum,
        "parity_samples": parity_samples,
        "stats": tracker.stats(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entities", type=int, default=12)
    parser.add_argument("--rows-per-entity", type=int, default=1800)
    parser.add_argument("--queries", type=int, default=500)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    entities = [f"sensor.ctx_{i}" for i in range(max(1, args.entities))]
    rows_per_entity = max(100, args.rows_per_entity)
    queries = max(20, args.queries)

    with tempfile.TemporaryDirectory(prefix="hm-ram-replay-bench-") as tmp:
        os.environ["ADAPTIVE_AI_DATA"] = tmp
        from replay import RAMReplayIndex, SQLiteTemporalTracker
        from storage import Store

        store = Store(Path(tmp) / "adaptive_ai.db")
        rows = []
        for entity_index, eid in enumerate(entities):
            for row_index in range(rows_per_entity):
                ts = float(row_index)
                received = ts
                # Deterministic late packets verify the second causal axis without
                # dominating the workload.
                if row_index % 211 == 0 and row_index > 0:
                    received = ts + 7.0
                rows.append((
                    eid,
                    ts,
                    str((row_index + entity_index) % 9),
                    {"v": (row_index + entity_index) % 13},
                    None,
                    "live",
                    received,
                ))
        for offset in range(0, len(rows), 4000):
            store.archive_batch(rows[offset:offset + 4000])

        cover_start = 0.0
        cover_end = float(rows_per_entity + 20)
        connection = sqlite3.connect(store.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            index = RAMReplayIndex.build(
                connection,
                entities,
                cover_start,
                cover_end,
                max_bytes=192 * 1024 * 1024,
            )
        finally:
            connection.close()

        baseline = SQLiteTemporalTracker(
            store, entities, context_stub(), cover_start, cover_end
        )
        indexed = SQLiteTemporalTracker(
            store, entities, context_stub(), cover_start, cover_end,
            ram_replay_index=index,
        )
        timestamps = [
            1.0 + i * (rows_per_entity - 2.0) / max(1, queries - 1)
            for i in range(queries)
        ]
        try:
            sqlite_run = workload(baseline, entities, timestamps)
            ram_run = workload(indexed, entities, timestamps)
        finally:
            baseline.close()
            indexed.close()

        parity = (
            sqlite_run["checksum"] == ram_run["checksum"]
            and sqlite_run["parity_samples"] == ram_run["parity_samples"]
        )
        sqlite_queries = int(sqlite_run["stats"].get("sql_queries") or 0)
        ram_queries = int(ram_run["stats"].get("sql_queries") or 0)
        reduction = (
            1.0 - ram_queries / max(1, sqlite_queries)
        )
        speedup = sqlite_run["seconds"] / max(ram_run["seconds"], 1e-9)

        result = {
            "contract": "ram_replay_index_component_benchmark_v1",
            "environment": "synthetic SQLite temporal transport; not Raspberry Pi full training",
            "entities": len(entities),
            "rows_per_entity": rows_per_entity,
            "queries": queries,
            "rows_written": len(rows),
            "parity": parity,
            "sqlite_seconds": sqlite_run["seconds"],
            "ram_seconds": ram_run["seconds"],
            "component_wall_speedup": speedup,
            "sqlite_tracker_queries": sqlite_queries,
            "ram_tracker_queries": ram_queries,
            "query_reduction_ratio": reduction,
            "ram_index": index.status(),
            "pass": bool(
                parity
                and index.status()["fallback_entities"] == 0
                and reduction >= 0.90
            ),
        }
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
