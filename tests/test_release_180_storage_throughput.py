"""Fast guarded SQLite imports, proactive WAL recovery and resumable coverage."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import history
from storage import Store
from storage_budget import MIB, StorageBudget, StoragePressure
from wal_checkpoint import WALCheckpointWorker


class GuardedImport180Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'test.db')
        self.budget = self.store.storage_budget

    def free(self, count):
        return patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=count))

    def test_many_small_rows_keep_all_data_with_few_disk_samples(self):
        before = self.budget.snapshot().get('disk_samples', 0)
        with patch('storage_budget.time.monotonic', return_value=100.0), self.store.conn() as c:
            c.executemany('INSERT INTO app_meta VALUES(?,?)', ((str(i), 'value') for i in range(2000)))
        samples = self.budget.snapshot()['disk_samples'] - before
        self.assertLess(samples, 20)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 2000)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_batch_payload_threshold_observes_pressure_and_rolls_back(self):
        def rows():
            yield ('first', 'x' * (MIB // 2))
            with self.free(self.budget.hard_reserve - 1):
                yield ('second', 'x' * (MIB // 2))
        with patch('storage_budget.time.monotonic', return_value=100.0):
            with self.assertRaises(StoragePressure), self.store.conn() as c:
                c.executemany('INSERT INTO app_meta VALUES(?,?)', rows())
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_batch_elapsed_threshold_observes_pressure_and_rolls_back(self):
        clock = SimpleNamespace(now=100.0)
        def rows():
            yield ('first', 'value')
            clock.now += .06
            with self.free(self.budget.hard_reserve - 1):
                yield ('second', 'value')
        with patch('storage_budget.time.monotonic', side_effect=lambda: clock.now):
            with self.assertRaises(StoragePressure), self.store.conn() as c:
                c.executemany('INSERT INTO app_meta VALUES(?,?)', rows())
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_commit_refreshes_even_inside_sampling_interval(self):
        with patch('storage_budget.time.monotonic', return_value=100.0):
            with self.assertRaises(StoragePressure), self.store.conn() as c:
                c.execute("INSERT INTO app_meta VALUES('new','value')")
                with self.free(self.budget.hard_reserve - 1):
                    c.commit()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_oversized_row_is_rejected_while_snapshot_is_cached(self):
        with patch('storage_budget.time.monotonic', return_value=100.0):
            with self.assertRaises(StoragePressure), self.store.conn() as c:
                c.executemany('INSERT INTO app_meta VALUES(?,?)', [('a', 'small'), ('b', 'x' * (17 * MIB))])
        self.assertEqual(self.budget.snapshot()['reason'], 'statement_payload_budget')
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_manual_capacity_above_8_gib_is_honoured_and_bounded(self):
        budget = StorageBudget(self.store.path, dict(storage_database_max_mb=20480,
            storage_wal_max_mb=512, storage_min_free_mb=4096))
        self.assertEqual((budget.db_limit, budget.wal_limit, budget.reserve),
                         (20480 * MIB, 512 * MIB, 4096 * MIB))
        maximum = StorageBudget(self.store.path, dict(storage_database_max_mb=999999,
            storage_wal_max_mb=999999, storage_min_free_mb=999999))
        self.assertEqual((maximum.db_limit, maximum.wal_limit, maximum.reserve),
                         (65536 * MIB, 4096 * MIB, 65536 * MIB))
        self.assertEqual(StorageBudget(self.store.path, {}).db_limit, 2048 * MIB)

    def test_real_wal_reclaims_before_pause_and_retries_after_five_seconds(self):
        self.store.start_wal_keeper()
        self.addCleanup(self.store.stop_wal_keeper)
        self.budget.wal_limit = MIB
        worker = WALCheckpointWorker(self.store)
        c = sqlite3.connect(self.store.path, timeout=0, isolation_level=None)
        self.addCleanup(c.close)
        c.execute('PRAGMA wal_autocheckpoint=0')
        def grow(prefix):
            for index in range(20):
                with self.store.conn() as writer:
                    writer.execute('INSERT INTO app_meta VALUES(?,?)', (prefix + str(index), 'x' * 32000))
                if self.budget.sample()['wal_bytes'] >= worker.RECLAIM_THRESHOLD_BYTES:
                    break
            self.assertEqual(self.budget.snapshot()['level'], 'normal')
        grow('first')
        self.assertTrue(worker.reclaim(c, worker.checkpoint(c), now=100))
        grow('second')
        self.assertFalse(worker.reclaim(c, worker.checkpoint(c), now=104))
        self.assertTrue(worker.reclaim(c, worker.checkpoint(c), now=105))
        self.assertEqual(self.budget.snapshot()['level'], 'normal')
        self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


class RecorderCoverage180Tests(unittest.TestCase):
    def manager(self):
        manager = history.HistoryManager.__new__(history.HistoryManager)
        manager.stop_event = threading.Event()
        manager.job_cancel_event = threading.Event()
        manager.training_recorder_coverage = {}
        manager.training_recorder_coverage_hits = 0
        manager.training_recorder_coverage_misses = 0
        manager.recorder_skipped_slices = 0
        manager.progress = 0.0
        manager.set_status = Mock()
        return manager

    def run_import(self, manager, start=0, end=10800):
        return manager._training_recorder_import(['switch.test'], start, end,
            batch_size=1, minimal=False, no_attributes=False, source='ha_history_full',
            label='test', max_hours=1, progress_lo=0, progress_hi=.1)

    def test_completed_chunk_persists_before_later_failure_and_restart_fetches_older_delta(self):
        manager = self.manager()
        meta = {}
        store = Mock()
        store.meta_set.side_effect = lambda key, value: meta.update({key: value})
        manager._fetch_history_resilient = Mock(side_effect=[7, InterruptedError('cancelled')])
        with patch('history.STORE', store):
            with self.assertRaises(InterruptedError):
                self.run_import(manager)
        restored = self.manager()
        restored.training_recorder_coverage = restored._decode_training_recorder_coverage(
            meta[manager.RECORDER_COVERAGE_META_KEY])
        self.assertEqual(restored.training_recorder_coverage[('ha_history_full', 'switch.test')], (7200., 10800.))
        restored._import_section = Mock(return_value=5)
        with patch('history.STORE', store):
            self.run_import(restored)
        call = restored._import_section.call_args
        self.assertEqual(call.args[1:3], (0., 9000.))
        self.assertIn('older delta', call.kwargs['label'])

    def test_unfinished_response_never_marks_chunk_complete(self):
        manager = self.manager()
        manager._fetch_history_resilient = Mock(side_effect=InterruptedError('partial response'))
        with patch('history.STORE', Mock()), self.assertRaises(InterruptedError):
            self.run_import(manager)
        self.assertEqual(manager.training_recorder_coverage, {})

    def test_skipped_window_never_claims_gap_as_covered(self):
        manager = self.manager()
        def fetch(batch, start, end, **kwargs):
            if start == 3600:
                manager.recorder_skipped_slices += 1
                return 0
            return 7
        manager._fetch_history_resilient = Mock(side_effect=fetch)
        with patch('history.STORE', Mock()):
            self.run_import(manager)
        self.assertEqual(manager.training_recorder_coverage[('ha_history_full', 'switch.test')], (0., 3600.))

    def test_completed_head_and_long_tail_preserve_original_middle(self):
        manager = self.manager()
        manager.training_recorder_coverage[('ha_history_full', 'switch.test')] = (3600., 7200.)
        manager._fetch_history_resilient = Mock(return_value=1)
        with patch('history.STORE', Mock()):
            self.run_import(manager, end=18000)
        self.assertEqual(manager.training_recorder_coverage[('ha_history_full', 'switch.test')], (0., 18000.))

    def test_stop_after_one_completed_chunk_cannot_cache_unvisited_windows(self):
        manager = self.manager()
        def fetch(*args, **kwargs):
            manager.stop_event.set()
            return 7
        manager._fetch_history_resilient = Mock(side_effect=fetch)
        with patch('history.STORE', Mock()):
            self.run_import(manager)
        self.assertEqual(manager.training_recorder_coverage[('ha_history_full', 'switch.test')], (7200., 10800.))


if __name__ == '__main__':
    unittest.main()
