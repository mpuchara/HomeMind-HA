#!/usr/bin/env python3
"""Synthetic boundedness benchmark for Stage-5 sparse long-term replay selection."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adaptive_ai" / "src"))

from long_memory import collect_sparse_dwells

DAY = 86400.0


class Store:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def archive_iter(self, start_ts, end_ts, entity_ids, chunk_size=256):
        ids = tuple(entity_ids)
        self.calls.append((start_ts, end_ts, ids, chunk_size))
        wanted = set(ids)
        for row in self.rows:
            if row["entity_id"] in wanted and start_ts <= row["ts"] <= end_ts:
                yield row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--transitions-per-day", type=int, default=192)
    parser.add_argument("--max-samples", type=int, default=96)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    reference = 1790553600.0
    old_start = reference - (7 + max(1, args.days)) * DAY
    old_end = reference - 7 * DAY
    rows = []
    row_id = 1
    total = max(2, int(args.days) * max(2, int(args.transitions_per_day)))
    step = max(1.0, (old_end - old_start - 2.0) / total)
    for idx in range(total + 1):
        rows.append({
            "id": row_id,
            "entity_id": "light.benchmark",
            "ts": old_start + 1.0 + idx * step,
            "state": "on" if idx % 2 else "off",
            "attributes_json": "{}",
            "context_user_id": None,
            "source": "benchmark",
        })
        row_id += 1

    store = Store(rows)
    agent = {
        "id": "benchmark",
        "target_entity": "light.benchmark",
        "target_property": "power",
        "deadband": 0.5,
    }
    started = time.perf_counter()
    selected, diagnostics = collect_sparse_dwells(
        store, agent, [0.0, 1.0],
        old_start, old_end, reference, args.max_samples,
    )
    elapsed = time.perf_counter() - started
    result = {
        "contract": diagnostics["contract"],
        "target_rows": len(rows),
        "target_rows_scanned": diagnostics["target_rows_scanned"],
        "selected_dwells": len(selected),
        "max_samples": args.max_samples,
        "retained_candidate_strata": diagnostics["retained_candidate_strata"],
        "candidate_cap": diagnostics["candidate_cap"],
        "archive_calls": len(store.calls),
        "only_target_entity_scanned": all(
            call[2] == ("light.benchmark",) for call in store.calls
        ),
        "elapsed_seconds": round(elapsed, 6),
    }
    assert result["selected_dwells"] <= args.max_samples
    assert result["retained_candidate_strata"] <= result["candidate_cap"]
    assert result["only_target_entity_scanned"]
    print(json.dumps(result, separators=(",", ":") if args.compact else None))


if __name__ == "__main__":
    main()
