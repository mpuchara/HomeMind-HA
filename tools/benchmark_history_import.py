"""Recorder import parity and writer batch-size benchmark; no HA CPU claim."""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adaptive_ai/src"))
if __name__ == "__main__":
    _scratch = tempfile.TemporaryDirectory(prefix="hm-import-benchmark-")
    os.environ["ADAPTIVE_AI_DATA"] = _scratch.name
import history
from settings import parse_ts
from storage import Store


def legacy_rows(data, source, numeric_min_interval=60.0):
    """Frozen 0.14.165 parser, before any database call."""
    rows = []
    for group in data or []:
        if not group:
            continue
        group_entity = group[0].get("entity_id")
        last_kept_ts = None
        last_item = None
        for item in group:
            eid = item.get("entity_id") or group_entity
            ts = parse_ts(item.get("last_changed") or item.get("last_updated"))
            if not eid or not ts:
                continue
            keep = True
            if source == "ha_history_minimal":
                try:
                    float(item.get("state"))
                    is_numeric = True
                except (TypeError, ValueError):
                    is_numeric = False
                if is_numeric and last_kept_ts is not None and ts - last_kept_ts < numeric_min_interval:
                    keep = False
            if keep:
                rows.append((eid, ts, item.get("state"), item.get("attributes") or {},
                             (item.get("context") or {}).get("user_id"), source))
                last_kept_ts = ts
            last_item = (eid, ts, item)
        if source == "ha_history_minimal" and last_item:
            eid, ts, item = last_item
            if last_kept_ts != ts:
                rows.append((eid, ts, item.get("state"), item.get("attributes") or {},
                             (item.get("context") or {}).get("user_id"), source))
    return rows


def payload(size):
    from datetime import datetime, timezone
    groups = []
    for entity in range(4):
        rows = []
        for i in range(size):
            rows.append(dict(entity_id=f"sensor.room_{entity}" if entity < 2 else f"binary_sensor.room_{entity}",
                last_changed=datetime.fromtimestamp(1700000000 + i, timezone.utc).isoformat(),
                state=str(i % 100) if entity < 2 else ("on" if i % 2 else "off"),
                attributes={"friendly_name": f"Room {entity}", "energy": i % 16},
                context={"user_id": None}))
        groups.append(rows)
    return groups


def manager():
    result = history.HistoryManager.__new__(history.HistoryManager)
    result.stop_event = threading.Event()
    result.job_cancel_event = threading.Event()
    return result


def canonical(store):
    return [{k: v for k, v in row.items() if k != "mutation_revision"}
            for row in store.archive_iter()]


def run(size=2400):
    data = payload(size)
    cases = []
    for source in ("ha_history_full", "ha_history_minimal"):
        repeats = []
        expected = None
        for repeat in range(3):
            metrics = {}
            for variant in (["previous", "bounded"] if repeat % 2 == 0 else ["bounded", "previous"]):
                with tempfile.TemporaryDirectory(prefix="hm-import-case-") as root:
                    store = Store(Path(root) / "archive.db")
                    store.start_wal_keeper()
                    store.start_wal_checkpoint()
                    samples = []
                    batch_sizes = []
                    original = store.archive_batch
                    def measured(rows):
                        batch_sizes.append(len(rows))
                        started = time.perf_counter()
                        value = original(rows)
                        samples.append((time.perf_counter() - started) * 1000)
                        return value
                    started = time.perf_counter()
                    try:
                        with patch.object(history, "STORE", store), patch.object(store, "archive_batch", measured), \
                                patch.object(history.TRAINING_BUDGET, "checkpoint", lambda *a, **k: 0), \
                                patch.dict(history.OPTIONS, history_context_import_interval_seconds=60):
                            written = measured(legacy_rows(data, source)) if variant == "previous" else manager()._archive_history_payload(data, source)
                        wall_ms = (time.perf_counter() - started) * 1000
                        rows = canonical(store)
                        if expected is None:
                            expected = rows
                        assert rows == expected, (source, variant)
                        assert written == len(legacy_rows(data, source))
                        if variant == "bounded":
                            assert max(batch_sizes) <= history.HISTORY_IMPORT_BATCH_ROWS
                        metrics[variant] = dict(rows=written, batches=len(samples), max_batch_rows=max(batch_sizes),
                            total_ms=round(wall_ms, 3), max_archive_call_ms=round(max(samples), 3),
                            median_archive_call_ms=round(statistics.median(samples), 3))
                    finally:
                        store.stop_wal_checkpoint()
                        store.stop_wal_keeper()
            repeats.append(metrics)
        cases.append(dict(source=source, every_archive_row_parity=True, repeats=repeats))
    return dict(cases=cases, batch_rows=history.HISTORY_IMPORT_BATCH_ROWS, background_checkpoint=True,
                limits="Synthetic import only. More commits may increase total time; max_archive_call_ms includes packing/open/commit/close. Cooperative sleeps disabled; no HA CPU or latency guarantee.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows-per-entity", type=int, default=2400)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.rows_per_entity), indent=None if args.compact else 2))
