#!/usr/bin/env python3
"""Synthetic benchmark for 0.14.112 preindexed causal transition edges.

Measures only fast-anchor/dwell transition lookup transport. It is not Raspberry Pi
end-to-end Train/Rebuild timing.
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


def ha_state(value):
    return {
        "state": str(value),
        "attributes": {"device_class": "occupancy"},
    }


def workload(tracker, entities, queries):
    signature = []
    started = time.perf_counter()
    for query_index in range(queries):
        eid = entities[query_index % len(entities)]
        at_ts = 900.0 + float((query_index * 17) % 2800)
        window = 120.0 if query_index % 3 else 30.0
        positive = bool(query_index % 2)
        signature.append((
            tracker.directional_transition_before(
                eid, at_ts, positive, window
            ),
            tracker.first_directional_transition_after(
                eid, at_ts - window, at_ts, not positive
            ),
        ))
    return {
        "seconds": time.perf_counter() - started,
        "signature": signature,
        "stats": tracker.stats(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entities", type=int, default=4)
    parser.add_argument("--rows-per-entity", type=int, default=1600)
    parser.add_argument("--queries", type=int, default=800)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    entities = [
        f"binary_sensor.edge_{i}"
        for i in range(max(1, args.entities))
    ]
    rows_per_entity = max(600, args.rows_per_entity)
    query_count = max(100, args.queries)

    with tempfile.TemporaryDirectory(prefix="hm-edge-bench-") as tmp:
        os.environ["ADAPTIVE_AI_DATA"] = tmp
        from observation_contract import (
            FeatureJournal,
            ObservationSQLiteTemporalTracker,
        )
        from replay import RAMReplayIndex
        from storage import Store

        store = Store(Path(tmp) / "adaptive_ai.db")
        journal = FeatureJournal(store)

        base_rows = []
        feature_rows = []
        for entity_index, eid in enumerate(entities):
            for row_index in range(rows_per_entity):
                ts = float(row_index * 2)
                state_value = "on" if (row_index // 5 + entity_index) % 2 else "off"
                # Durable archive is deliberately sparse. High-resolution feature rows
                # are the expensive legacy edge path this stage removes from each query.
                if row_index % 8 == 0:
                    base_rows.append((
                        eid, ts, state_value,
                        {"device_class": "occupancy"}, None, "test", ts,
                    ))
                received = ts + (7.0 if row_index % 211 == 0 and row_index else 0.05)
                feature_rows.append({
                    "entity_id": eid,
                    "state": ha_state(state_value),
                    "event_time": ts,
                    "received_time": received,
                    "source": "ha_state_changed",
                    "event_key": f"{entity_index}:{row_index}",
                })

        for offset in range(0, len(base_rows), 4000):
            store.archive_batch(base_rows[offset:offset + 4000])
        for offset in range(0, len(feature_rows), 2000):
            journal.record_batch(feature_rows[offset:offset + 2000])

        cover_start = 0.0
        # The lookup workload reaches ~3700 s with the default deterministic modulo.
        # Keep the declared index coverage wider than every synthetic query; out-of-range
        # fallback has its own regression and would contaminate this component benchmark.
        cover_end = max(float(rows_per_entity * 2 + 20), 3800.0)
        connection = sqlite3.connect(store.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            ram_index = RAMReplayIndex.build(
                connection, entities, cover_start, cover_end,
                max_bytes=64 * 1024 * 1024,
            )
        finally:
            connection.close()

        build_started = time.perf_counter()
        edge_index = ObservationSQLiteTemporalTracker.build_transition_edge_index(
            store, entities, cover_start, cover_end,
            ram_replay_index=ram_index,
        )
        edge_build_seconds = time.perf_counter() - build_started

        legacy = ObservationSQLiteTemporalTracker(
            store, entities, context_stub(), cover_start, cover_end,
            ram_replay_index=ram_index,
        )
        indexed = ObservationSQLiteTemporalTracker(
            store, entities, context_stub(), cover_start, cover_end,
            ram_replay_index=ram_index,
            transition_edge_index=edge_index,
        )
        try:
            legacy_before = legacy.stats()["sql_queries"]
            indexed_before = indexed.stats()["sql_queries"]
            legacy_run = workload(legacy, entities, query_count)
            indexed_run = workload(indexed, entities, query_count)
        finally:
            legacy.close()
            indexed.close()

        parity = legacy_run["signature"] == indexed_run["signature"]
        legacy_lookup_sql = (
            int(legacy_run["stats"]["sql_queries"]) - int(legacy_before)
        )
        indexed_lookup_sql = (
            int(indexed_run["stats"]["sql_queries"]) - int(indexed_before)
        )
        build_sql = int(edge_index.status().get("sql_queries") or 0)
        total_indexed_sql = build_sql + indexed_lookup_sql
        sql_reduction = 1.0 - total_indexed_sql / max(1, legacy_lookup_sql)
        warm_speedup = (
            legacy_run["seconds"] / max(indexed_run["seconds"], 1e-9)
        )
        total_indexed_seconds = edge_build_seconds + indexed_run["seconds"]
        build_plus_speedup = (
            legacy_run["seconds"] / max(total_indexed_seconds, 1e-9)
        )

        result = {
            "contract": "transition_edge_index_component_benchmark_v1",
            "environment": "synthetic archive + feature journal; not Raspberry Pi full training",
            "entities": len(entities),
            "rows_per_entity": rows_per_entity,
            "queries": query_count,
            "parity": parity,
            "legacy_lookup_seconds": legacy_run["seconds"],
            "indexed_warm_lookup_seconds": indexed_run["seconds"],
            "edge_index_build_seconds": edge_build_seconds,
            "build_plus_lookup_seconds": total_indexed_seconds,
            "warm_lookup_speedup": warm_speedup,
            "build_plus_lookup_speedup": build_plus_speedup,
            "legacy_lookup_sql_queries": legacy_lookup_sql,
            "edge_index_build_sql_queries": build_sql,
            "indexed_lookup_sql_queries": indexed_lookup_sql,
            "build_plus_lookup_sql_queries": total_indexed_sql,
            "sql_query_reduction_ratio": sql_reduction,
            "edge_index": edge_index.status(),
            "indexed_tracker": {
                "lookups": indexed_run["stats"].get("transition_edge_index_lookups"),
                "hits": indexed_run["stats"].get("transition_edge_index_hits"),
                "fallbacks": indexed_run["stats"].get("transition_edge_index_fallbacks"),
                "rows_applied": indexed_run["stats"].get("transition_edge_rows_applied"),
                "scan_rows_avoided_estimate": indexed_run["stats"].get(
                    "transition_edge_scan_rows_avoided_estimate"
                ),
            },
            "pass": bool(
                parity
                and edge_index.status()["fallback_entities"] == 0
                and indexed_lookup_sql == 0
                and sql_reduction >= 0.90
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
