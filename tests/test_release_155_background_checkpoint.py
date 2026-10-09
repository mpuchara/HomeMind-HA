"""Checkpoint isolation, real SQLite snapshots, fallback and lifecycle regressions."""
import json
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
from telemetry import RUNTIME_DEBUG
from wal_checkpoint import WALCheckpointWorker


class BackgroundCheckpoint155Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.addCleanup(self.store.stop_wal_keeper)
        self.addCleanup(self.store.stop_wal_checkpoint)

    def start(self):
        self.store.start_wal_keeper()
        self.assertTrue(self.store.start_wal_checkpoint())
        self.assertTrue(self.store.sqlite_snapshot()['wal_checkpoint']['active'])
        return self.store._wal_checkpoint_worker

    def write(self, key='committed'):
        with self.store.lock, self.store.conn() as c:
            c.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)', (key, 'yes'))

    def wait_until(self, check, timeout=3):
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() >= deadline:
                self.fail('worker condition timed out')
            time.sleep(.005)

    def test_standalone_store_keeps_default_checkpoint_and_normal_sync(self):
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
            self.assertEqual(c.execute('PRAGMA synchronous').fetchone()[0], 1)

    def test_active_worker_disables_commit_checkpoint_but_preserves_sync(self):
        self.start()
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 0)
            self.assertEqual(c.execute('PRAGMA synchronous').fetchone()[0], 1)

    def test_stopped_worker_restores_default_on_new_connection(self):
        self.start()
        self.store.stop_wal_checkpoint()
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)

    def test_committed_wal_is_visible_before_explicit_copy_to_database_file(self):
        # immutable reads only the database file, ignoring WAL. This is a test
        # witness of checkpoint progress, never an application access mode.
        with patch.object(WALCheckpointWorker, 'POLL_SECONDS', 60):
            worker = self.start()
            self.write('wal-only')
            uri = Path(self.store.path).as_uri() + '?immutable=1'
            with closing(sqlite3.connect(uri, uri=True)) as file_only:
                self.assertIsNone(file_only.execute("SELECT value FROM app_meta WHERE key='wal-only'").fetchone())
            with self.store.conn() as ordinary:
                self.assertEqual(ordinary.execute("SELECT value FROM app_meta WHERE key='wal-only'").fetchone()[0], 'yes')
            with closing(sqlite3.connect(self.store.path, isolation_level=None)) as checkpoint:
                result = worker.checkpoint(checkpoint)
                self.assertGreater(result['checkpointed_frames'], 0)
            with closing(sqlite3.connect(uri, uri=True)) as file_only:
                self.assertEqual(file_only.execute("SELECT value FROM app_meta WHERE key='wal-only'").fetchone()[0], 'yes')
            worker.stop()

    def test_borrowed_session_tracks_worker_start_and_failure_without_merging(self):
        with self.store.connection_session() as session:
            self.assertEqual(session.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
            worker = self.start()
            self.write('before-failure')
            self.assertEqual(session.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 0)
            worker._error(sqlite3.OperationalError('checkpoint failed'))
            self.write('after-failure')
            self.assertEqual(session.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
            with closing(sqlite3.connect(self.store.path)) as witness:
                self.assertEqual(witness.execute("SELECT count(*) FROM app_meta WHERE key IN ('before-failure','after-failure')").fetchone()[0], 2)

    def test_slow_checkpoint_does_not_own_store_lock_or_block_foreground_commit(self):
        entered, release = threading.Event(), threading.Event()
        original = WALCheckpointWorker.checkpoint
        def slow(worker, connection, **kwargs):
            if kwargs.get('reason', 'periodic') == 'periodic':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test checkpoint not released')
            return original(worker, connection, **kwargs)
        with patch.object(WALCheckpointWorker, 'POLL_SECONDS', .02), patch.object(WALCheckpointWorker, 'THRESHOLD_PAGES', 1), patch.object(WALCheckpointWorker, 'checkpoint', slow):
            self.addCleanup(release.set)
            worker = self.start()
            self.write('trigger')
            self.assertTrue(entered.wait(3))
            try:
                self.assertTrue(self.store.lock.acquire(timeout=.2))
                self.store.lock.release()
                self.write('while-checkpoint-slow')
                with closing(sqlite3.connect(self.store.path, timeout=.2)) as witness:
                    self.assertEqual(witness.execute("SELECT value FROM app_meta WHERE key='while-checkpoint-slow'").fetchone()[0], 'yes')
                json.dumps(self.store.sqlite_snapshot())
                self.assertTrue(worker.active)
            finally:
                release.set()
                worker.stop()

    def test_passive_checkpoint_leaves_pinned_reader_and_next_attempt_finishes(self):
        self.start()
        self.write('first')
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as reader, closing(sqlite3.connect(self.store.path, isolation_level=None)) as checkpoint:
            reader.execute('BEGIN')
            reader.execute('SELECT count(*) FROM app_meta').fetchone()
            self.write('later')
            checkpoint.execute('PRAGMA busy_timeout=0')
            result = self.store._wal_checkpoint_worker.checkpoint(checkpoint)
            self.assertGreater(result['remaining_frames'], 0)
            self.assertIsNone(reader.execute("SELECT value FROM app_meta WHERE key='later'").fetchone())
            reader.execute('COMMIT')
            result = self.store._wal_checkpoint_worker.checkpoint(checkpoint)
            self.assertEqual(result['remaining_frames'], 0)
            self.assertEqual(checkpoint.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_passive_checkpoint_does_not_wait_for_active_writer(self):
        self.start()
        self.write('existing')
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer, closing(sqlite3.connect(self.store.path, timeout=0, isolation_level=None)) as checkpoint:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO app_meta VALUES('private','no')")
            started = time.monotonic()
            self.store._wal_checkpoint_worker.checkpoint(checkpoint)
            self.assertLess(time.monotonic() - started, 1)
            writer.execute('ROLLBACK')
        with self.store.conn() as c:
            self.assertIsNone(c.execute("SELECT value FROM app_meta WHERE key='private'").fetchone())

    def test_threshold_triggers_periodic_checkpoint(self):
        with patch.object(WALCheckpointWorker, 'POLL_SECONDS', .02), patch.object(WALCheckpointWorker, 'THRESHOLD_PAGES', 1):
            worker = self.start()
            self.write()
            self.wait_until(lambda: worker.snapshot()['runs'] > 0)
            self.assertGreater(worker.snapshot()['wal_bytes'], 32)
            worker.stop()

    def test_small_wal_checkpointed_by_age(self):
        with patch.object(WALCheckpointWorker, 'POLL_SECONDS', .02), patch.object(WALCheckpointWorker, 'MAX_IDLE_SECONDS', .06):
            worker = self.start()
            self.write()
            self.wait_until(lambda: worker.snapshot()['runs'] > 0)
            self.assertLess(worker.snapshot()['wal_bytes'], 32 + 1000 * 4120)
            worker.stop()

    def test_setup_failure_closes_connection_and_keeps_default_policy(self):
        worker = WALCheckpointWorker(self.store)
        self.store._wal_checkpoint_worker = worker
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError('setup failed')
        with patch.object(worker, '_connect', return_value=connection):
            worker.start()
            worker.stop()
        connection.close.assert_called_once()
        self.assertFalse(worker.active)
        self.assertEqual(worker.snapshot()['errors'], 1)
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)

    def test_thread_start_failure_does_not_break_runtime_or_shutdown(self):
        worker = WALCheckpointWorker(self.store)
        self.store._wal_checkpoint_worker = worker
        with patch('wal_checkpoint.threading.Thread.start', side_effect=RuntimeError('thread limit')):
            self.assertFalse(worker.start())
        worker.stop()
        self.assertFalse(worker.snapshot()['alive'])
        self.assertEqual(worker.snapshot()['errors'], 1)
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)

    def test_checkpoint_failure_fallback_and_recovery_are_counted_once(self):
        worker = self.start()
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError('I/O failed')
        with self.assertRaises(sqlite3.OperationalError):
            worker.checkpoint(connection)
        self.assertFalse(worker.active)
        self.assertEqual(worker.snapshot()['errors'], 1)
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
        connection.execute.side_effect = None
        connection.execute.return_value.fetchone.return_value = (0, 2, 2)
        worker.checkpoint(connection)
        self.assertTrue(worker.active)
        self.assertEqual(worker.snapshot()['errors'], 1)

    def test_busy_results_accumulate_and_remaining_frames_are_separate(self):
        worker = WALCheckpointWorker(self.store)
        connection = MagicMock()
        for result in [(1, 8, 3), (1, 9, 3), (0, 9, 9)]:
            connection.execute.return_value.fetchone.return_value = result
            worker.checkpoint(connection)
        snapshot = worker.snapshot()
        self.assertEqual(snapshot['busy_results'], 2)
        self.assertEqual(snapshot['last_busy'], 0)
        self.assertEqual(snapshot['remaining_frames'], 0)

    def test_active_worker_preserves_store_rollback_and_independent_visibility(self):
        self.start()
        with self.store.connection_session():
            self.write('durable-before-error')
            with self.assertRaises(ValueError), self.store.conn() as c:
                c.execute("INSERT INTO app_meta VALUES('uncommitted','no')")
                with self.store.conn() as witness:
                    self.assertIsNone(witness.execute("SELECT value FROM app_meta WHERE key='uncommitted'").fetchone())
                raise ValueError('rollback')
        with self.store.conn() as c:
            self.assertIsNone(c.execute("SELECT value FROM app_meta WHERE key='uncommitted'").fetchone())
            self.assertEqual(c.execute("SELECT value FROM app_meta WHERE key='durable-before-error'").fetchone()[0], 'yes')

    def test_periodic_errors_retry_with_bounded_poll_and_no_double_count(self):
        worker = WALCheckpointWorker(self.store)
        self.store._wal_checkpoint_worker = worker
        connection = MagicMock()
        def execute(sql):
            if 'wal_checkpoint(' in sql:
                raise RuntimeError('checkpoint unavailable')
            result = MagicMock()
            result.fetchone.return_value = ('wal',) if sql == 'PRAGMA journal_mode' else (4096,)
            return result
        connection.execute.side_effect = execute
        self.store.start_wal_keeper()
        self.write()
        with patch.object(worker, '_connect', return_value=connection), patch.object(worker, 'POLL_SECONDS', .05), patch.object(worker, 'THRESHOLD_PAGES', 1):
            worker.start()
            try:
                self.wait_until(lambda: worker.snapshot()['errors'] >= 2)
                self.assertLessEqual(worker.snapshot()['errors'], 3)
                self.assertFalse(worker.active)
                with self.store.conn() as c:
                    self.assertEqual(c.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
            finally:
                worker.stop()
        attempts = sum('wal_checkpoint(' in call.args[0] for call in connection.execute.call_args_list)
        self.assertEqual(worker.snapshot()['errors'], attempts)
        connection.close.assert_called_once()

    def test_stop_joins_inflight_checkpoint_and_keeps_keeper_available(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        worker = self.start()
        original = worker.checkpoint
        def slow(connection, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('checkpoint not released')
            return original(connection, **kwargs)
        with patch.object(worker, 'checkpoint', side_effect=slow):
            shutdown = threading.Thread(target=lambda: (worker.stop(), finished.set()))
            shutdown.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(finished.wait(.05))
                self.assertTrue(self.store.sqlite_snapshot()['wal_keeper_active'])
            finally:
                release.set()
                shutdown.join(3)
            self.assertTrue(finished.is_set())
            self.assertFalse(worker.snapshot()['alive'])

    def test_start_stop_and_restart_are_idempotent(self):
        worker = self.start()
        self.assertFalse(self.store.start_wal_checkpoint())
        worker.stop()
        worker.stop()
        self.assertFalse(worker.snapshot()['alive'])
        self.assertTrue(self.store.start_wal_checkpoint())
        self.assertTrue(worker.snapshot()['active'])

    def test_shutdown_waits_for_checkpoint_before_releasing_keeper(self):
        worker = self.start()
        worker.stop()
        snapshot = worker.snapshot()
        self.assertGreaterEqual(snapshot['runs'], 1)
        self.assertTrue(self.store.sqlite_snapshot()['wal_keeper_active'])
        self.write('after-stop')
        self.store.stop_wal_keeper()
        self.assertFalse(Path(self.store.path + '-wal').exists())
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT value FROM app_meta WHERE key='after-stop'").fetchone()[0], 'yes')

    def test_training_worker_skips_parent_checkpoint(self):
        self.store.training_worker_process = True
        self.assertFalse(self.store.start_wal_checkpoint())
        self.assertIsNone(self.store._wal_checkpoint_worker)

    def test_snapshot_uses_ram_even_when_store_lock_is_held_elsewhere(self):
        self.start()
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.store.lock:
                held.set()
                release.wait(3)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(1))
        try:
            with patch.object(self.store, 'conn', side_effect=AssertionError('unexpected SQL')):
                self.assertTrue(self.store.sqlite_snapshot()['wal_checkpoint']['active'])
        finally:
            release.set()
            thread.join(3)

    def test_trace_reports_checkpoint_and_commit_policy(self):
        worker = self.start()
        RUNTIME_DEBUG.set_enabled(True, clear=True)
        try:
            self.write()
            with closing(sqlite3.connect(self.store.path, isolation_level=None)) as c:
                worker.checkpoint(c, reason='regression')
            rows = [r for r in RUNTIME_DEBUG.export()['entries'] if r['kind'] == 'end']
            transactions = [r for r in rows if r['operation'] == 'sqlite_transaction' and 'test_release_155_background_checkpoint.write:' in r['fields'].get('caller', '')]
            self.assertEqual(transactions[0]['fields']['autocheckpoint_pages'], 0)
            checkpoints = [r for r in rows if r['operation'] == 'sqlite_wal_checkpoint' and r['fields'].get('reason') == 'regression']
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0]['fields']['remaining_frames'], 0)
            self.assertEqual(checkpoints[0]['status'], 'ok')
        finally:
            RUNTIME_DEBUG.set_enabled(False, clear=True)

    def test_runtime_starts_before_extensions_and_stops_after_drains_before_keeper(self):
        source = (support.ROOT / 'adaptive_ai/src/main.py').read_text(encoding='utf-8')
        initialize = source.split('def initialize_runtime():', 1)[1].split('def ', 1)[0]
        self.assertLess(initialize.index('STORE.start_wal_keeper()'), initialize.index('STORE.start_wal_checkpoint()'))
        self.assertLess(initialize.index('STORE.start_wal_checkpoint()'), initialize.index('prepare_runtime_extensions()'))
        shutdown = source.split('def shutdown_runtime():', 1)[1].split('def run_initialize_runtime', 1)[0]
        self.assertLess(shutdown.index('shutdown_step("runtime_events"'), shutdown.index('stop_wal_checkpoint'))
        self.assertLess(shutdown.index('stop_wal_checkpoint'), shutdown.index('stop_wal_keeper'))
