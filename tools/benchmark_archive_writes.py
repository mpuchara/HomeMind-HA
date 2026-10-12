"""Recorder import cost and WAL bytes versus the frozen 0.14.184 writer.

Runs the actual parser and SQLite with 128-row transactions; no HA/Pi timing claim.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import MethodType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
if __name__ == '__main__':
    _scratch = tempfile.TemporaryDirectory(prefix='hm-archive-benchmark-')
    os.environ['ADAPTIVE_AI_DATA'] = _scratch.name
import history
from storage import Store
from tools.benchmark_history_import import payload, manager, canonical
from tools.fixtures.archive_batch_014184 import archive_batch, sample


def run(size=6000, repeats=3):
    data = payload(size)
    cases = []
    for source in ('ha_history_fast_context', 'ha_history_minimal', 'ha_history_full'):
        measurements = []
        for repeat in range(repeats):
            metrics = {}
            witness = None
            for variant in (('previous', 'current') if repeat % 2 == 0 else ('current', 'previous')):
                with tempfile.TemporaryDirectory(prefix='hm-archive-case-') as directory:
                    store = Store(Path(directory) / 'archive.db')
                    if variant == 'previous':
                        store.archive_batch = MethodType(archive_batch, store)
                        store.storage_budget.sample = MethodType(sample, store.storage_budget)
                    store.start_wal_keeper()
                    # Match active runtime's disabled foreground autocheckpoint,
                    # but keep maintenance off so file growth measures WAL writes.
                    store._wal_checkpoint_worker = SimpleNamespace(active=True)
                    try:
                        before_samples = store.storage_budget.snapshot().get('disk_samples', 0)
                        sample_stats = []
                        batches = []
                        original = store.archive_batch
                        def measured(rows):
                            batches.append(len(rows))
                            started = time.perf_counter()
                            value = original(rows)
                            sample_stats.append((time.perf_counter() - started) * 1000)
                            return value
                        with patch.object(store, 'archive_batch', measured), \
                             patch.object(history, 'STORE', store), \
                             patch.object(history.TRAINING_BUDGET, 'checkpoint', return_value=0):
                            started = time.perf_counter()
                            written = manager()._archive_history_payload(data, source)
                            insert_seconds = time.perf_counter() - started
                            wal_before = Path(store.path + '-wal').stat().st_size
                            fingerprint = store.archive_window_fingerprint(
                                ['sensor.room_0'], 1700000000, 1700000000 + size)
                            started = time.perf_counter()
                            manager()._archive_history_payload(data, source)
                            retry_seconds = time.perf_counter() - started
                        rows = canonical(store)
                        if witness is None:
                            witness = rows
                        assert rows == witness, (source, variant, 'content parity')
                        assert written == len(rows)
                        assert max(batches) <= 128
                        with store.conn() as c:
                            assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                        if variant == 'current':
                            assert store.archive_window_fingerprint(
                                ['sensor.room_0'], 1700000000, 1700000000 + size) == fingerprint
                        metrics[variant] = dict(rows=written, insert_seconds=round(insert_seconds, 6),
                            retry_seconds=round(retry_seconds, 6),
                            retry_wal_growth_bytes=Path(store.path + '-wal').stat().st_size - wal_before,
                            disk_samples=store.storage_budget.snapshot()['disk_samples'] - before_samples,
                            batches=len(batches), max_batch_rows=max(batches),
                            median_batch_ms=round(statistics.median(sample_stats), 3),
                            max_batch_ms=round(max(sample_stats), 3))
                    finally:
                        store.stop_wal_keeper()
            # Slow I/O may trigger extra mandatory 50 ms samples. Never gate CI
            # on a timing-dependent sample ratio; the frozen-clock regression
            # verifies the 5 -> 3 fresh samples for a short transaction.
            assert metrics['current']['disk_samples'] >= metrics['current']['batches'] * 3
            assert metrics['current']['retry_wal_growth_bytes'] < metrics['previous']['retry_wal_growth_bytes']
            measurements.append(metrics)
        cases.append(dict(source=source, content_parity=True,
            median_insert_speedup=round(statistics.median(
                m['previous']['insert_seconds'] / m['current']['insert_seconds'] for m in measurements), 3),
            median_retry_speedup=round(statistics.median(
                m['previous']['retry_seconds'] / m['current']['retry_seconds'] for m in measurements), 3),
            repeats=measurements))
    return dict(reference='91c7343 / 0.14.184', cases=cases, batch_rows=128,
        limits='Synthetic local parser/SQLite component with cooperative sleeps and checkpoints disabled for WAL byte comparison. Not a full training, HA or Raspberry Pi SD-card measurement. Mutation revisions differ for unchanged retries only.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows-per-entity', type=int, default=6000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.rows_per_entity, args.repeats), indent=None if args.compact else 2))
