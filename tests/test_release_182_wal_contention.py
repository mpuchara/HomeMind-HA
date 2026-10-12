"""Capacity-aware WAL maintenance and durable retry of real writer contention."""
import sqlite3
import gc
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import test_executor as executor_fixture
import test_release_178_wal_housekeeping as housekeeping_fixture
from storage import Store
from storage_budget import MIB, StorageRetry
from sqlite_background import is_sqlite_busy
from provenance_runtime import install
from wal_checkpoint import WALCheckpointWorker


class WALCapacity182Tests(unittest.TestCase):
    def worker(self, limit):
        return WALCheckpointWorker(SimpleNamespace(storage_budget=SimpleNamespace(wal_limit=limit, sample=Mock())))

    def test_thresholds_scale_with_every_installation(self):
        for limit in (32 * MIB, 128 * MIB, 592891810, 4 * 1024 * MIB):
            with self.subTest(limit=limit):
                worker = self.worker(limit)
                self.assertEqual(worker.RECLAIM_THRESHOLD_BYTES, limit // 4)
                self.assertEqual(worker.storage_pressure_threshold_bytes, limit * 2 // 5)
                self.assertLess(worker.storage_pressure_threshold_bytes, limit // 2)

    def test_user_report_72_mb_is_not_pressure_with_593_mb_budget(self):
        worker = self.worker(592891810)
        connection = Mock()
        with patch.object(worker, 'sample_storage', return_value=72013512):
            self.assertFalse(worker.reclaim(connection, {'busy': 0, 'remaining_frames': 0}, now=100))
        connection.execute.assert_not_called()
        self.assertEqual(worker.snapshot()['reclaim_pressure_attempts'], 0)

    def test_ordinary_reclaim_obeys_minute_cooldown(self):
        worker = self.worker(592891810)
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (0, 0, 0)
        with patch.object(worker, 'sample_storage', return_value=160 * MIB):
            self.assertTrue(worker.reclaim(connection, {'busy': 0, 'remaining_frames': 0}, now=100))
            self.assertFalse(worker.reclaim(connection, {'busy': 0, 'remaining_frames': 0}, now=105))
            self.assertFalse(worker.reclaim(connection, {'busy': 0, 'remaining_frames': 0}, now=159))
            self.assertTrue(worker.reclaim(connection, {'busy': 0, 'remaining_frames': 0}, now=160))
        self.assertEqual(worker.snapshot()['reclaim_pressure_attempts'], 0)

    def test_near_pause_bypasses_minute_cooldown_with_five_second_floor(self):
        worker = self.worker(128 * MIB)
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (0, 0, 0)
        complete = {'busy': 0, 'remaining_frames': 0}
        with patch.object(worker, 'sample_storage', return_value=40 * MIB):
            self.assertTrue(worker.reclaim(connection, complete, now=100))
        with patch.object(worker, 'sample_storage', return_value=55 * MIB):
            self.assertFalse(worker.reclaim(connection, complete, now=104))
            self.assertTrue(worker.reclaim(connection, complete, now=105))
        self.assertEqual(worker.snapshot()['reclaim_pressure_attempts'], 1)

    def test_pressure_still_preserves_incomplete_checkpoint(self):
        worker = self.worker(128 * MIB)
        connection = Mock()
        with patch.object(worker, 'sample_storage', return_value=70 * MIB):
            self.assertFalse(worker.reclaim(connection, {'busy': 0, 'remaining_frames': 1}, now=100))
        connection.execute.assert_not_called()


class BusyRetry182Tests(unittest.TestCase):
    def test_native_busy_and_extended_locked_are_retryable(self):
        for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_LOCKED,
                     sqlite3.SQLITE_LOCKED_SHAREDCACHE):
            exc = sqlite3.OperationalError('native message')
            exc.sqlite_errorcode = code
            self.assertTrue(is_sqlite_busy(exc))

    def test_other_failures_are_not_hidden_by_message(self):
        for exc in (RuntimeError('database is locked'), sqlite3.OperationalError('disk I/O error'),
                    sqlite3.OperationalError('database disk image is malformed')):
            self.assertFalse(is_sqlite_busy(exc))
        exc = sqlite3.OperationalError('database is locked')
        exc.sqlite_errorcode = sqlite3.SQLITE_IOERR
        self.assertFalse(is_sqlite_busy(exc))

    def test_busy_cooldown_does_not_consume_real_error_report(self):
        retry = StorageRetry()
        with patch('storage_budget.time.monotonic', return_value=100):
            self.assertFalse(retry.defer(sqlite3.OperationalError('database is locked')))
            self.assertEqual(retry.delay(), 1)
            self.assertEqual(retry.snapshot()['busy_retries'], 1)
            self.assertTrue(retry.defer(sqlite3.OperationalError('disk I/O error')))
        retry.recovered()
        self.assertIsNone(retry.snapshot()['reason'])
        self.assertEqual(retry.snapshot()['busy_retries'], 1)

    def test_real_housekeeping_busy_retains_rows_and_recovers_quietly(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.db')
            engine = housekeeping_fixture.Storage178Tests().engine_fixture()
            before = list(engine.pending_archive)
            blocker = sqlite3.connect(store.path)
            try:
                blocker.execute('BEGIN IMMEDIATE')
                with patch('engine.STORE', store), patch.object(store, 'event') as event, \
                        patch('engine.time.monotonic', return_value=100):
                    engine._run_housekeeping()
                    for _ in range(10):
                        engine._run_housekeeping()
                    event.assert_not_called()
                self.assertEqual(engine.pending_archive, before)
                self.assertEqual(engine.housekeeping_stats['errors'], 0)
                self.assertEqual(engine.housekeeping_retry.snapshot()['busy_retries'], 1)
            finally:
                blocker.rollback()
                blocker.close()
            with patch('engine.STORE', store), patch('engine.time.monotonic', return_value=101):
                engine._run_housekeeping()
            self.assertEqual(engine.pending_archive, [])
            self.assertEqual(store.archive_count(), len(before))


class ProvenanceBusy182Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = executor_fixture.ExecutorTests()
        self.fixture.setUp()
        install(SimpleNamespace(STORE=self.fixture.store, ENGINE=self.fixture.e))

    def tearDown(self):
        self.stop_writer()
        gc.collect()  # Release exception/SQLite cycles before Windows removes the fixture.
        self.fixture.tearDown()

    def stop_writer(self):
        self.fixture.e.stop_event.set()
        self.fixture.e.provenance_writer_event.set()
        self.fixture.e.provenance_writer_thread.join(timeout=4)
        self.assertFalse(self.fixture.e.provenance_writer_thread.is_alive())

    def queue_event(self):
        self.fixture.e.provenance.record_event('light.test', support.state('light.test', 'on'),
                                             event_time=1, event_id='queued')

    def test_explicit_flush_preserves_native_error_queue_and_later_durability(self):
        self.stop_writer()
        self.queue_event()
        blocker = sqlite3.connect(self.fixture.store.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError) as raised, self.fixture.store.background_sqlite():
                self.fixture.store._flush_all_provenance()
            self.assertTrue(is_sqlite_busy(raised.exception))
            self.assertEqual(self.fixture.e.provenance.pending_event_count(), 1)
        finally:
            blocker.rollback()
            blocker.close()
        self.fixture.store._flush_all_provenance()
        self.assertEqual(self.fixture.e.provenance.pending_event_count(), 0)
        with self.fixture.store.conn() as c:
            self.assertEqual(c.execute("SELECT event_id FROM provenance_events").fetchone()[0], 'queued')

    def test_real_background_writer_retries_without_warning_or_event_loss(self):
        blocker = sqlite3.connect(self.fixture.store.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            with patch.object(self.fixture.store, 'event') as event:
                self.queue_event()
                self.fixture.e.provenance_writer_event.set()
                deadline = time.monotonic() + 3
                while not self.fixture.e.provenance_deferred_snapshot()['storage_retry']['busy_retries']:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                for _ in range(100):
                    self.fixture.e.provenance_writer_event.set()
                time.sleep(.1)
                event.assert_not_called()
                data = self.fixture.e.provenance_deferred_snapshot()
                self.assertEqual(data['storage_retry']['busy_retries'], 1)
                self.assertEqual(data['events']['errors'], 0)
                self.assertEqual(data['events']['pending'], 1)
        finally:
            blocker.rollback()
            blocker.close()
        deadline = time.monotonic() + 4
        while self.fixture.e.provenance_deferred_snapshot()['events']['flushed'] < 1:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        self.assertEqual(self.fixture.e.provenance.pending_event_count(), 0)
        with self.fixture.store.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM provenance_events").fetchone()[0], 1)

    def test_io_error_dominates_busy_when_flushing_multiple_queues(self):
        self.stop_writer()
        f = self.fixture
        f.store.update_agent(f.a['id'], {'mode': 'shadow'})
        f.e.agent_configs = {f.a['id']: f.store.get_agent_config(f.a['id'])}
        self.assertEqual(f.e.executor.submit(f.intent(), {0: 1.0}, 1)['status'], 'SHADOW')
        self.queue_event()
        with patch.object(f.e.provenance, 'flush_events_batch', side_effect=sqlite3.OperationalError('database is locked')), \
                patch.object(f.e.provenance, 'record_decisions_batch', side_effect=sqlite3.OperationalError('disk I/O error')):
            with self.assertRaisesRegex(RuntimeError, 'disk I/O error'):
                f.store._flush_all_provenance()
        self.assertEqual(f.e.provenance.pending_event_count(), 1)
        self.assertGreater(f.e.provenance_deferred_snapshot()['decisions']['pending'], 0)
        f.store._flush_all_provenance()
        self.assertEqual(f.e.provenance_deferred_snapshot()['decisions']['pending'], 0)
