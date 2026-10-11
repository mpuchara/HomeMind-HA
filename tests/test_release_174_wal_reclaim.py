"""Real SQLite recovery of retained WAL, pinned snapshots and independent commits."""
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import support
from storage import Store
from wal_checkpoint import WALCheckpointWorker
from telemetry import RUNTIME_DEBUG
from sqlite_policy import WAL_RETAIN_BYTES


class WALReclaim174Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.store.start_wal_keeper()
        self.addCleanup(self.store.stop_wal_keeper)
        self.addCleanup(self.store.stop_wal_checkpoint)
        self.worker = WALCheckpointWorker(self.store)
        self.worker.RECLAIM_THRESHOLD_BYTES = 1
        self.connection = sqlite3.connect(self.store.path, timeout=0, isolation_level=None)
        self.connection.execute('PRAGMA wal_autocheckpoint=0')
        self.addCleanup(self.connection.close)
        self.wal = Path(self.store.path + '-wal')

    def write(self, key, value='committed'):
        with self.store.conn() as c:
            c.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)', (key, value))

    def copied(self):
        return self.worker.checkpoint(self.connection)

    def test_new_writer_and_borrowed_session_have_retention_policy(self):
        with self.store.connection_session() as c:
            self.assertEqual(c.execute('PRAGMA journal_size_limit').fetchone()[0], WAL_RETAIN_BYTES)
            with self.store.conn() as borrowed:
                self.assertIs(borrowed, c)
                self.assertEqual(borrowed.execute('PRAGMA synchronous').fetchone()[0], 1)

    def test_training_worker_writer_has_same_retention(self):
        self.store.training_worker_process = True
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA journal_size_limit').fetchone()[0], WAL_RETAIN_BYTES)
            self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 60000)

    def test_reset_releases_high_water_without_losing_large_transaction(self):
        # Large transactions can exceed the limit. Only a later safe reset trims
        # old allocation; the limit must never reject or clip active WAL frames.
        with patch('storage.WAL_RETAIN_BYTES', 4096):
            self.write('large', 'x' * 180000)
            before = self.wal.stat().st_size
            self.assertGreater(before, 4096)
            self.copied()
            self.write('after-reset')
            self.assertLess(self.wal.stat().st_size, before)
        with closing(sqlite3.connect(self.store.path)) as witness:
            self.assertEqual(witness.execute("SELECT value FROM app_meta WHERE key='large'").fetchone()[0], 'x' * 180000)
            self.assertEqual(witness.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_reclaim_releases_existing_allocation_and_keeps_durable_rows(self):
        self.write('large', 'x' * 180000)
        before = self.wal.stat().st_size
        self.assertTrue(self.worker.reclaim(self.connection, self.copied(), now=100))
        self.assertEqual(self.wal.stat().st_size, 0)
        self.assertEqual(self.worker.snapshot()['reclaimed_bytes'], before)
        self.write('after-reclaim')
        with closing(sqlite3.connect(self.store.path)) as witness:
            self.assertEqual(witness.execute("SELECT value FROM app_meta WHERE key='large'").fetchone()[0], 'x' * 180000)
            self.assertEqual(witness.execute("SELECT value FROM app_meta WHERE key='after-reclaim'").fetchone()[0], 'committed')
            self.assertEqual(witness.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_complete_copy_but_pinned_wal_reader_is_busy_without_wait(self):
        self.write('first')
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT value FROM app_meta').fetchall()
            copied = self.copied()
            self.assertEqual(copied['remaining_frames'], 0)
            self.connection.execute('PRAGMA busy_timeout=30000')
            self.assertFalse(self.worker.reclaim(self.connection, copied, now=100))
            self.assertEqual(self.connection.execute('PRAGMA busy_timeout').fetchone()[0], 0)
            self.assertGreater(self.wal.stat().st_size, 0)
            self.assertEqual(reader.execute("SELECT value FROM app_meta WHERE key='first'").fetchone()[0], 'committed')
            self.assertEqual(self.worker.snapshot()['reclaim_busy'], 1)
            reader.execute('COMMIT')
        self.assertTrue(self.worker.reclaim(self.connection, self.copied(), now=161))

    def test_partial_checkpoint_skips_reclaim_and_preserves_old_snapshot(self):
        self.write('first')
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT value FROM app_meta').fetchall()
            self.write('new')
            copied = self.copied()
            self.assertGreater(copied['remaining_frames'], 0)
            self.assertFalse(self.worker.reclaim(self.connection, copied, now=100))
            self.assertEqual(self.worker.snapshot()['reclaim_attempts'], 0)
            self.assertIsNone(reader.execute("SELECT value FROM app_meta WHERE key='new'").fetchone())
            reader.execute('COMMIT')
        self.assertTrue(self.worker.reclaim(self.connection, self.copied(), now=100))

    def test_new_writer_after_preflight_is_busy_and_rollback_stays_private(self):
        self.write('committed')
        copied = self.copied()
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO app_meta VALUES('private','no')")
            self.assertFalse(self.worker.reclaim(self.connection, copied, now=100))
            writer.execute('ROLLBACK')
        self.assertTrue(self.worker.reclaim(self.connection, self.copied(), now=161))
        self.assertIsNone(self.connection.execute("SELECT value FROM app_meta WHERE key='private'").fetchone())

    def test_retained_small_allocation_does_not_truncate_each_poll(self):
        self.worker.RECLAIM_THRESHOLD_BYTES = 1024 * 1024
        self.write('small')
        self.assertFalse(self.worker.reclaim(self.connection, self.copied(), now=100))
        self.assertEqual(self.worker.snapshot()['reclaim_attempts'], 0)

    def test_failed_reclamation_is_rate_limited_and_does_not_disable_passive(self):
        self.write('data')
        copied = self.copied()
        failing = MagicMock()
        failing.execute.side_effect = sqlite3.OperationalError('disk is full')
        self.assertFalse(self.worker.reclaim(failing, copied, now=100))
        self.assertTrue(self.worker.active)
        self.assertIn('disk is full', self.worker.snapshot()['last_reclaim_error'])
        self.assertFalse(self.worker.reclaim(self.connection, copied, now=159))
        self.assertEqual(self.worker.snapshot()['reclaim_attempts'], 1)
        self.assertTrue(self.worker.reclaim(self.connection, self.copied(), now=160))
        self.assertIsNone(self.worker.snapshot()['last_reclaim_error'])

    def test_worker_reclamation_never_acquires_store_lock(self):
        self.write('data')
        copied = self.copied()
        result = []
        def run():
            with closing(self.worker._connect()) as c:
                result.append(self.worker.reclaim(c, copied))
        with self.store.lock:
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(result, [True])

    def test_periodic_worker_reclaims_after_copy_and_refreshes_size(self):
        with patch.object(WALCheckpointWorker, 'POLL_SECONDS', .02), patch.object(WALCheckpointWorker, 'THRESHOLD_PAGES', 1), patch.object(WALCheckpointWorker, 'RECLAIM_THRESHOLD_BYTES', 1):
            self.store.start_wal_checkpoint()
            worker = self.store._wal_checkpoint_worker
            self.write('large', 'x' * 180000)
            deadline = time.monotonic() + 3
            while worker.snapshot()['reclaim_successes'] == 0 and time.monotonic() < deadline:
                time.sleep(.005)
            worker.stop()
            self.assertEqual(worker.snapshot()['reclaim_successes'], 1)
            self.assertEqual(worker.snapshot()['wal_bytes'], 0)
            self.assertEqual(self.connection.execute("SELECT length(value) FROM app_meta WHERE key='large'").fetchone()[0], 180000)

    def test_storage_diagnostics_report_physical_files_and_sample_failure(self):
        self.write('data')
        self.worker.sample_storage()
        snapshot = self.worker.snapshot()
        self.assertEqual(snapshot['db_bytes'], Path(self.store.path).stat().st_size)
        self.assertEqual(snapshot['wal_bytes'], self.wal.stat().st_size)
        self.assertGreater(snapshot['disk_free_bytes'], 0)
        self.worker.active = True
        with patch('wal_checkpoint.shutil.disk_usage', side_effect=OSError('sample failed')):
            self.assertEqual(self.worker.sample_storage(), self.wal.stat().st_size)
            self.assertTrue(self.worker.reclaim(self.connection, self.copied()))
        self.assertTrue(self.worker.active)
        self.assertIsNone(self.worker.snapshot()['disk_free_bytes'])
        self.assertIn('sample failed', self.worker.snapshot()['storage_sample_error'])

    def test_trace_distinguishes_passive_copy_and_reclamation(self):
        RUNTIME_DEBUG.set_enabled(True, clear=True)
        try:
            self.write('data')
            self.assertTrue(self.worker.reclaim(self.connection, self.copied()))
            entries = [r for r in RUNTIME_DEBUG.export()['entries'] if r['kind'] == 'end']
            reclaim = [r for r in entries if r['operation'] == 'sqlite_wal_reclaim']
            self.assertEqual(len(reclaim), 1)
            self.assertEqual(reclaim[0]['fields']['mode'], 'TRUNCATE')
            self.assertGreater(reclaim[0]['fields']['reclaimed_bytes'], 0)
        finally:
            RUNTIME_DEBUG.set_enabled(False, clear=True)
