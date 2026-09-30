#!/usr/bin/env python3
"""Measure the outer-loop row reduction from target-only historical replay.

This benchmark exercises Store.archive_iter(), i.e. the same checkpointed SQLite paging
path used by isolated training workers. It does not measure full policy training.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
sys.path.insert(0, str(SRC))


def build_rows(context_entities, context_rows, target_rows):
    rows = []
    target_every = max(1, context_rows // max(1, target_rows))
    for idx in range(context_rows):
        base_ts = float(idx) * 0.25
        for sensor in range(context_entities):
            rows.append((
                f"sensor.ctx_{sensor}",
                base_ts + sensor * 0.0001,
                str((idx + sensor) % 1000),
                {},
                None,
                "live",
            ))
        if idx % target_every == 0:
            rows.append((
                "light.target",
                base_ts + 0.002,
                "on" if (idx // target_every) % 2 else "off",
                {},
                None,
                "live",
            ))
    return rows


def scan(store, entities, target_only_filter=False):
    started = time.perf_counter()
    rows = 0
    target = []
    for row in store.archive_iter(0, 10**12, entities, chunk_size=256):
        rows += 1
        if not target_only_filter or row["entity_id"] == "light.target":
            target.append((row["id"], row["ts"], row["state"]))
    return {
        "wall_seconds": time.perf_counter() - started,
        "rows_deserialized": rows,
        "target_signature": target,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-entities", type=int, default=12)
    parser.add_argument("--context-rows", type=int, default=4000)
    parser.add_argument("--target-rows", type=int, default=80)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADAPTIVE_AI_DATA"] = tmp
        # Import only after ADAPTIVE_AI_DATA is set.
        from storage import Store

        store = Store(Path(tmp) / "adaptive_ai.db")
        store.checkpointed_archive_reads = True
        data = build_rows(
            max(1, args.context_entities),
            max(100, args.context_rows),
            max(2, args.target_rows),
        )
        # Keep insertion bounded, matching product batch behavior more closely.
        for offset in range(0, len(data), 4000):
            store.archive_batch(data[offset:offset + 4000])

        broad_entities = {"light.target"} | {
            f"sensor.ctx_{i}" for i in range(max(1, args.context_entities))
        }
        broad = scan(store, broad_entities, target_only_filter=True)
        narrow = scan(store, {"light.target"}, target_only_filter=False)

        parity = broad["target_signature"] == narrow["target_signature"]
        row_reduction = (
            1.0 - narrow["rows_deserialized"] / max(1, broad["rows_deserialized"])
        )
        wall_speedup = (
            broad["wall_seconds"] / max(narrow["wall_seconds"], 1e-9)
        )
        result = {
            "contract": "target_only_replay_stream_benchmark_v1",
            "environment": "synthetic SQLite Store.archive_iter; not Raspberry Pi full training",
            "context_entities": int(args.context_entities),
            "rows_written": len(data),
            "broad_rows_deserialized": broad["rows_deserialized"],
            "target_only_rows_deserialized": narrow["rows_deserialized"],
            "row_reduction_ratio": row_reduction,
            "broad_wall_seconds": broad["wall_seconds"],
            "target_only_wall_seconds": narrow["wall_seconds"],
            "outer_stream_wall_speedup": wall_speedup,
            "target_row_parity": parity,
            "pass": bool(parity and row_reduction >= 0.80),
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
