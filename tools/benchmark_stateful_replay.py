"""Synthetic Stage-8 replay-overlap benchmark.

Compares the old physical multi-hour overlap scan with the Stage-8 contract:
logical overlap is retained for semantics, but already-committed history is replaced by
a target-only continuation seed and unique forward scanning.
"""
import argparse
import json
from pathlib import Path
import tempfile
import time
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from history import stateful_continuation_seed_rows
from storage import Store


def chunks(start, end, chunk_seconds, overlap_seconds):
    cursor = float(start)
    first = True
    while cursor < float(end) - 0.5:
        chunk_end = min(float(end), cursor + float(chunk_seconds))
        logical_start = (
            float(start)
            if first
            else max(float(start), cursor - float(overlap_seconds))
        )
        yield logical_start, cursor, chunk_end, first
        cursor = chunk_end
        first = False


def consume(store, plans, entity_ids):
    count = 0
    started = time.perf_counter()
    for lo, hi in plans:
        for _row in store.archive_iter(lo, hi, entity_ids, chunk_size=512):
            count += 1
    return count, time.perf_counter() - started


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=18.0)
    parser.add_argument("--sensors", type=int, default=32)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    hours = max(12.0, float(args.hours))
    sensors = max(4, int(args.sensors))
    interval = max(10.0, float(args.interval))
    base = 1_700_000_000.0
    end = base + hours * 3600.0
    chunk_seconds = 6 * 3600.0
    overlap_seconds = min(chunk_seconds * 0.5, 6 * 3600.0)
    target = "switch.stage8"
    contexts = [f"sensor.context_{idx}" for idx in range(sensors)]
    all_entities = contexts + [target]

    with tempfile.TemporaryDirectory(prefix="hm-stage8-benchmark-") as root:
        store = Store(Path(root) / "stage8.db")
        rows = []
        step_count = int((end - base) // interval) + 1
        for idx in range(step_count):
            ts = base + idx * interval
            for sensor_idx, entity_id in enumerate(contexts):
                rows.append((
                    entity_id,
                    ts,
                    str((idx + sensor_idx) % 17),
                    {"unit_of_measurement": "x"},
                    None,
                    "stage8_benchmark",
                ))
            if idx % 30 == 0:
                rows.append((
                    target,
                    ts + 0.25,
                    "on" if (idx // 30) % 2 else "off",
                    {},
                    None,
                    "stage8_benchmark",
                ))
        store.archive_batch(rows)

        agent = {
            "id": "stage8-agent",
            "target_entity": target,
            "target_property": "power",
            "deadband": 0.5,
        }
        target_map = {target: [agent]}

        legacy_plans = []
        stateful_plans = []
        seed_rows = 0
        logical_hours = 0.0
        unique_hours = 0.0
        avoided_hours = 0.0
        for logical_start, cursor, chunk_end, first in chunks(
            base, end, chunk_seconds, overlap_seconds
        ):
            legacy_plans.append((logical_start, chunk_end))
            logical_hours += (chunk_end - logical_start) / 3600.0
            if first:
                scan_start = logical_start
            else:
                scan_start = cursor
                _seeds, scanned = stateful_continuation_seed_rows(
                    store, [agent], target_map, logical_start, scan_start
                )
                seed_rows += int(scanned)
            stateful_plans.append((scan_start, chunk_end))
            unique_hours += (chunk_end - scan_start) / 3600.0
            avoided_hours += max(0.0, scan_start - logical_start) / 3600.0

        legacy_rows, legacy_seconds = consume(
            store, legacy_plans, all_entities
        )
        forward_rows, stateful_seconds = consume(
            store, stateful_plans, all_entities
        )
        stateful_rows = int(forward_rows + seed_rows)
        row_reduction = (
            1.0 - stateful_rows / legacy_rows if legacy_rows else 0.0
        )

        result = {
            "contract": "stateful_chunk_continuation_v1",
            "hours": hours,
            "context_sensors": sensors,
            "interval_seconds": interval,
            "chunks": len(legacy_plans),
            "logical_hours": logical_hours,
            "unique_hours_scanned": unique_hours,
            "overlap_hours_avoided": avoided_hours,
            "legacy_rows_scanned": legacy_rows,
            "stateful_forward_rows_scanned": forward_rows,
            "stateful_seed_target_rows": seed_rows,
            "stateful_total_rows_scanned": stateful_rows,
            "row_reduction_fraction": row_reduction,
            "legacy_iteration_seconds": legacy_seconds,
            "stateful_forward_iteration_seconds": stateful_seconds,
        }
        if avoided_hours <= 0:
            raise AssertionError("benchmark did not exercise overlap")
        if stateful_rows >= legacy_rows:
            raise AssertionError("stateful continuation did not reduce replay rows")
        if row_reduction < 0.10:
            raise AssertionError(
                f"unexpectedly small replay-row reduction: {row_reduction:.3f}"
            )
        print(
            json.dumps(
                result,
                separators=(",", ":") if args.compact else None,
                indent=None if args.compact else 2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
