"""WAL pressure recovery must not wait a minute or create housekeeping errors."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from engine import Engine
from storage import Store
from storage_budget import MIB, StorageRetry
from wal_checkpoint import WALCheckpointWorker
from runtime_debug_log import RuntimeDebugLogService


class Storage178Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.budget = self.store.storage_budget

    def wal_fixture(self):
        self.store.start_wal_keeper()
        self.addCleanup(self.store.stop_wal_keeper)
        self.budget.wal_limit = MIB
        worker = WALCheckpointWorker(self.store)
        connection = sqlite3.connect(self.store.path, timeout=0, isolation_level=None)
        connection.execute('PRAGMA wal_autocheckpoint=0')
        self.addCleanup(connection.close)
        return worker, connection

    def grow_wal(self, prefix):
        for index in range(40):
            with self.store.conn() as c:
                c.execute('INSERT INTO app_meta(key,value) VALUES(?,?)', (prefix + str(index), 'x' * 64000))
            if self.budget.sample()['level'] == 'paused':
                return index + 1
        self.fail('Fixture did not reach WAL pressure')

    def engine_fixture(self):
        engine = object.__new__(Engine)
        engine.lock = threading.RLock()
        engine.housekeeping_retry = StorageRetry()
        engine.housekeeping_future = None
        engine.housekeeping_last_submit = 0.0
        engine.housekeeping_worker = Mock()
        engine.last_event_monotonic = 0.0
        engine.housekeeping_stats = dict(runs=0, errors=0, busy_skips=0,
            deferred_for_realtime=0, last_duration_ms=0.0, max_duration_ms=0.0,
            archive_rows=0, decision_rows=0, context_saves=0, archive_storage_skipped=0)
        engine.pending_archive = [('sensor.radar', 1700000000.0, 'on', {}, None, 'live'),
                                  ('sensor.radar', 1700000001.0, 'off', {}, None, 'live')]
        engine.archive_seen = {}
        engine.archive_last_ts = {}
        engine.archive_flush_batch_rows = 1
        engine.archive_flush_interval_seconds = 2.0
        engine.last_archive_flush = 0.0
        engine.teaching = SimpleNamespace(flush=Mock(return_value=0))
        engine.context = SimpleNamespace(save=Mock(return_value=True))
        return engine

    def test_pressure_recovery_bypasses_minute_cooldown_but_preserves_pinned_reader(self):
        worker, connection = self.wal_fixture()
        expected = self.grow_wal('first-')
        self.assertTrue(worker.reclaim(connection, worker.checkpoint(connection), now=100))
        expected += self.grow_wal('second-')
        reader = sqlite3.connect(self.store.path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute('BEGIN')
        self.assertEqual(reader.execute('SELECT count(*) FROM app_meta').fetchone()[0], expected)
        copied = worker.checkpoint(connection)
        attempts = worker.snapshot()['reclaim_attempts']
        self.assertFalse(worker.reclaim(connection, copied, now=104))
        self.assertEqual(worker.snapshot()['reclaim_attempts'], attempts)
        self.assertFalse(worker.reclaim(connection, copied, now=105))
        self.assertEqual(worker.snapshot()['reclaim_busy'], 1)
        self.assertEqual(reader.execute('SELECT count(*) FROM app_meta').fetchone()[0], expected)
        reader.execute('COMMIT')
        self.assertFalse(worker.reclaim(connection, worker.checkpoint(connection), now=109))
        self.assertTrue(worker.reclaim(connection, worker.checkpoint(connection), now=110))
        self.assertEqual(self.budget.snapshot()['level'], 'normal')
        self.assertEqual(worker.snapshot()['reclaim_pressure_attempts'], 3)
        self.assertEqual(connection.execute('SELECT count(*) FROM app_meta').fetchone()[0], expected)
        self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.budget.wal_limit, MIB)

    def test_pressure_never_truncates_incomplete_checkpoint(self):
        worker, connection = self.wal_fixture()
        with self.store.conn() as c:
            c.execute("INSERT INTO app_meta VALUES('seed','old')")
        reader = sqlite3.connect(self.store.path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM app_meta').fetchall()
        self.grow_wal('new-')
        copied = worker.checkpoint(connection)
        self.assertGreater(copied['remaining_frames'], 0)
        self.assertFalse(worker.reclaim(connection, copied, now=105))
        self.assertEqual(worker.snapshot()['reclaim_attempts'], 0)
        self.assertEqual(reader.execute('SELECT count(*) FROM app_meta').fetchone()[0], 1)
        reader.execute('COMMIT')
        self.assertTrue(worker.reclaim(connection, worker.checkpoint(connection), now=105))
        self.assertEqual(self.budget.snapshot()['level'], 'normal')

    def test_housekeeping_pressure_is_quiet_keeps_rows_and_resumes_after_cooldown(self):
        engine = self.engine_fixture()
        rows = list(engine.pending_archive)
        with patch('engine.STORE', self.store), patch.object(self.store, 'event') as event, \
             patch('engine.time.monotonic', return_value=100), \
             patch('engine.require_storage_available', side_effect=self.budget.pressure('wal_budget')):
            engine._run_housekeeping()
            for _ in range(100):
                self.assertFalse(engine._schedule_housekeeping(force=True))
                engine._run_housekeeping()
        self.assertEqual(engine.pending_archive, rows)
        self.assertEqual(engine.housekeeping_stats['runs'], 1)
        self.assertEqual(engine.housekeeping_stats['errors'], 0)
        self.assertEqual(engine.housekeeping_retry.snapshot()['pauses'], 1)
        event.assert_not_called()
        engine.housekeeping_worker.submit.assert_not_called()
        with patch('engine.STORE', self.store), patch('engine.time.monotonic', return_value=105):
            engine._run_housekeeping()
        self.assertEqual(engine.pending_archive, [])
        self.assertEqual(self.store.archive_count(), 2)
        self.assertIsNone(engine.housekeeping_retry.snapshot()['reason'])

    def test_housekeeping_race_restores_failed_archive_batch(self):
        engine = self.engine_fixture()
        rows = list(engine.pending_archive)
        archive = self.store.archive_batch
        with patch('engine.STORE', self.store), patch.object(self.store, 'event') as event, \
             patch('engine.time.monotonic', return_value=100), \
             patch.object(self.store, 'archive_batch', side_effect=self.budget.pressure('wal_budget')):
            engine._run_housekeeping()
        self.assertEqual(engine.pending_archive, rows)
        event.assert_not_called()
        with patch('engine.STORE', self.store), patch('engine.time.monotonic', return_value=105), \
             patch.object(self.store, 'archive_batch', wraps=archive) as callback:
            engine._run_housekeeping()
        callback.assert_called_once_with(rows)
        self.assertEqual(self.store.archive_count(), 2)

    def test_live_archive_pause_keeps_existing_rows_without_new_pending_growth(self):
        engine = self.engine_fixture()
        rows = list(engine.pending_archive)
        with self.budget.lock:
            self.budget.stats.update(level='paused', reason='wal_budget')
        with patch('engine.STORE', self.store), patch.object(self.budget, 'sample') as sample:
            for index in range(1000):
                engine._queue_archive_state(support.state('binary_sensor.motion', str(index)))
        self.assertEqual(engine.pending_archive, rows)
        self.assertEqual(engine.housekeeping_stats['archive_storage_skipped'], 1000)
        sample.assert_not_called()
        self.assertEqual(engine.archive_seen, {})

    def test_unrelated_housekeeping_errors_remain_visible_and_rate_limited(self):
        engine = self.engine_fixture()
        engine.flush_archive = Mock(side_effect=sqlite3.OperationalError('disk I/O error'))
        with patch('engine.STORE', self.store), patch.object(self.store, 'event') as event:
            for now in (100, 100, 101, 159, 160):
                with patch('engine.time.monotonic', return_value=now):
                    engine._run_housekeeping()
        self.assertEqual(event.call_count, 2)
        self.assertEqual(engine.flush_archive.call_count, 4)
        self.assertEqual(engine.housekeeping_stats['errors'], 4)
        self.assertEqual(engine.housekeeping_retry.snapshot()['pauses'], 0)

    def test_debug_export_exposes_housekeeping_pause_and_pending_count(self):
        engine = self.engine_fixture()
        engine.housekeeping_retry.defer(self.budget.pressure('wal_budget'))
        service = RuntimeDebugLogService(SimpleNamespace(ENGINE=engine, STORE=self.store), SimpleNamespace())
        data = service.export_payload()['engine']['deferred_persistence']['housekeeping']
        self.assertEqual(data['pending_archive'], 2)
        self.assertEqual(data['storage_retry']['pauses'], 1)
        self.assertIn('wal_budget', data['storage_retry']['reason'])


if __name__ == '__main__':
    unittest.main()
