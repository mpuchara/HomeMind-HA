"""Real Recorder/SQLite row parity and 0.14.179 storage guard cost comparison."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
if __name__ == '__main__':
    _scratch = tempfile.TemporaryDirectory(prefix='hm-storage-benchmark-')
    os.environ['ADAPTIVE_AI_DATA'] = _scratch.name
import history
import storage
from tools.benchmark_history_import import payload, manager, canonical

spec = importlib.util.spec_from_file_location('storage_guard_179', ROOT / 'tools/fixtures/storage_budget_014179.py')
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)


def run(size=2000, repeats=3):
    data = payload(size)
    cases = []
    for source in ('ha_history_fast_context', 'ha_history_minimal'):
        results = []
        witness = None
        for repeat in range(repeats):
            order = ('previous', 'current') if repeat % 2 == 0 else ('current', 'previous')
            metrics = {}
            for variant in order:
                budget_class = previous.StorageBudget if variant == 'previous' else storage.StorageBudget
                connection_class = previous.BudgetConnection if variant == 'previous' else storage.BudgetConnection
                with tempfile.TemporaryDirectory(prefix='hm-storage-case-') as directory, \
                        patch.object(storage, 'StorageBudget', budget_class), \
                        patch.object(storage, 'BudgetConnection', connection_class), \
                        patch.dict(storage.OPTIONS, storage_budget_mode='manual'):
                    store = storage.Store(Path(directory) / 'archive.db')
                    store.start_wal_keeper()
                    calls = [0]
                    sample = store.storage_budget.sample
                    def counted_sample():
                        calls[0] += 1
                        return sample()
                    batches = []
                    archive = store.archive_batch
                    def measured(rows):
                        batches.append(len(rows))
                        return archive(rows)
                    try:
                        with patch.object(store.storage_budget, 'sample', counted_sample), \
                                patch.object(store, 'archive_batch', measured), \
                                patch.object(history, 'STORE', store), \
                                patch.object(history.TRAINING_BUDGET, 'checkpoint', return_value=0), \
                                patch.dict(history.OPTIONS, history_context_import_interval_seconds=60):
                            started = time.perf_counter()
                            written = manager()._archive_history_payload(data, source)
                            # Exercise the real upsert path as well as fresh inserts.
                            manager()._archive_history_payload(data, source)
                            elapsed = time.perf_counter() - started
                        rows = canonical(store)
                        checksum = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                        if witness is None:
                            witness = rows
                        assert rows == witness, (source, variant, 'archive row parity')
                        assert written == len(rows)
                        assert max(batches) <= history.HISTORY_IMPORT_BATCH_ROWS
                        metrics[variant] = dict(rows=written, insert_and_upsert_seconds=round(elapsed, 6),
                            disk_samples=calls[0], checksum_sha256=checksum, max_batch_rows=max(batches))
                    finally:
                        store.stop_wal_keeper()
            # Structural gate, deliberately no noisy runner timing threshold.
            assert metrics['current']['disk_samples'] < metrics['previous']['disk_samples'] / 4
            results.append(metrics)
        speedups = [r['previous']['insert_and_upsert_seconds'] / r['current']['insert_and_upsert_seconds'] for r in results]
        cases.append(dict(source=source, every_archive_row_parity=True,
            median_component_speedup=round(statistics.median(speedups), 3), repeats=results))
    return dict(reference='frozen 0.14.179 guard from eb4950ab65be379ae27dfecd63390cff4afe65bf',
        cases=cases, batch_rows=history.HISTORY_IMPORT_BATCH_ROWS,
        limits='Local Recorder parsing and real SQLite insert/upsert component. Cooperative sleeps disabled. No HA/SD-card or complete training speed guarantee.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows-per-entity', type=int, default=2000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.rows_per_entity, args.repeats), indent=None if args.compact else 2))
