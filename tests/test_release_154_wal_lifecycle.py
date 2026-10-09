"""Real WAL lifetime and checkpoint tests, with independent transaction witnesses."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import support
from storage import Store
from telemetry import RUNTIME_DEBUG
from runtime_debug_log import RuntimeDebugLogService


class WALLifecycle154Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.addCleanup(self.store.stop_wal_keeper)
        self.wal = Path(self.store.path + '-wal')

    def write(self, key='test'):
        with self.store.lock, self.store.conn() as c:
            c.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)', (key, 'committed'))

    def test_keeper_prevents_last_close_cleanup_until_explicit_stop(self):
        self.write()
        self.assertFalse(self.wal.exists(), 'baseline short-lived last connection did not clean up WAL')
        self.assertTrue(self.store.start_wal_keeper())
        self.write()
        self.assertTrue(self.wal.exists())
        self.assertGreater(self.wal.stat().st_size, 0)
        self.assertTrue(self.store.stop_wal_keeper())
        self.assertFalse(self.wal.exists())
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT value FROM app_meta WHERE key='test'").fetchone()[0], 'committed')

    def test_start_stop_are_idempotent_and_restart_reopens_keeper(self):
        self.assertTrue(self.store.start_wal_keeper())
        self.assertFalse(self.store.start_wal_keeper())
        self.assertTrue(self.store.stop_wal_keeper())
        self.assertFalse(self.store.stop_wal_keeper())
        self.assertTrue(self.store.start_wal_keeper())
        self.assertEqual(self.store.sqlite_snapshot()['wal_keeper_starts'], 2)
        self.assertEqual(self.store.sqlite_snapshot()['wal_keeper_stops'], 1)

    def test_keeper_is_query_only_and_leaves_no_transaction(self):
        self.store.start_wal_keeper()
        c = self.store._wal_keeper
        self.assertFalse(c.in_transaction)
        self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            c.execute("INSERT INTO app_meta VALUES('forbidden','write')")
        self.assertFalse(c.in_transaction)

    def test_idle_keeper_does_not_pin_reader_snapshot_or_prevent_checkpoint(self):
        self.store.start_wal_keeper()
        self.write()
        with self.store.conn() as c:
            busy, frames, checkpointed = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        self.assertEqual((busy, frames, checkpointed), (0, 0, 0))
        self.assertEqual(self.wal.stat().st_size, 0)
        self.write('after-checkpoint')
        self.assertGreater(self.wal.stat().st_size, 0)

    def test_stop_from_shutdown_thread_is_safe(self):
        self.store.start_wal_keeper()
        result = []
        def stop():
            try:
                result.append(self.store.stop_wal_keeper())
            except Exception as exc:
                result.append(exc)
        t = threading.Thread(target=stop); t.start(); t.join(3)
        self.assertEqual(result, [True])
        self.assertFalse(self.store.sqlite_snapshot()['wal_keeper_active'])

    def test_training_worker_does_not_start_parent_keeper(self):
        self.store.training_worker_process = True
        with patch('storage.sqlite3.connect') as connect:
            self.assertFalse(self.store.start_wal_keeper())
        connect.assert_not_called()

    def test_failed_setup_closes_unpublished_connection_and_can_retry(self):
        c = MagicMock()
        c.execute.side_effect = sqlite3.OperationalError('busy setup')
        with patch('storage.sqlite3.connect', return_value=c), self.assertRaises(sqlite3.OperationalError):
            self.store.start_wal_keeper()
        c.close.assert_called_once()
        self.assertFalse(self.store.sqlite_snapshot()['wal_keeper_active'])
        self.assertEqual(self.store.sqlite_snapshot()['wal_keeper_starts'], 0)
        self.assertTrue(self.store.start_wal_keeper())

    def test_keeper_does_not_merge_or_publish_other_transactions(self):
        self.store.start_wal_keeper()
        with self.store.connection_session():
            self.write('committed-before-error')
            with self.assertRaises(ValueError), self.store.conn() as writer:
                writer.execute("INSERT INTO app_meta VALUES('private','no')")
                with self.store.conn() as witness:
                    self.assertIsNone(witness.execute("SELECT value FROM app_meta WHERE key='private'").fetchone())
                raise ValueError('rollback')
        with self.store.conn() as c:
            self.assertIsNone(c.execute("SELECT value FROM app_meta WHERE key='private'").fetchone())
            self.assertEqual(c.execute("SELECT value FROM app_meta WHERE key='committed-before-error'").fetchone()[0], 'committed')

    def test_multiple_connections_read_write_with_keeper_and_integrity_check(self):
        self.store.start_wal_keeper()
        errors = []
        def exercise(n):
            try:
                for step in range(20):
                    self.write(f'{n}-{step}')
                    with self.store.conn() as c:
                        self.assertGreater(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=exercise, args=(n,)) for n in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_debug_export_of_keeper_uses_ram_only(self):
        self.store.start_wal_keeper()
        core = SimpleNamespace(STORE=self.store, ENGINE=None)
        service = RuntimeDebugLogService(core, None)
        with patch.object(self.store, 'conn', side_effect=AssertionError('export ran SQL')):
            payload = service.export_payload()
        self.assertTrue(payload['sqlite']['wal_keeper_active'])
        json.dumps(payload)

    def test_debug_export_includes_all_existing_deferred_queue_snapshots(self):
        tournament = SimpleNamespace(
            shadow_persistence_snapshot=lambda: {'pending': 4},
            fast_light_persistence_snapshot=lambda: {'pending_models': 2},
        )
        engine = SimpleNamespace(
            context_tournament=tournament,
            provenance_deferred_snapshot=lambda: {'events': {'pending': 100}},
            feature_observation_deferred_snapshot=lambda: {'observations': {'pending': 20}},
        )
        payload = RuntimeDebugLogService(SimpleNamespace(STORE=self.store, ENGINE=engine), None).export_payload()
        deferred = payload['engine']['deferred_persistence']
        self.assertEqual(deferred['provenance']['events']['pending'], 100)
        self.assertEqual(deferred['feature_journal']['observations']['pending'], 20)
        self.assertEqual(deferred['fast_light']['pending_models'], 2)

    def test_bad_queue_snapshot_does_not_hide_other_diagnostics(self):
        def fail():
            raise RuntimeError('snapshot failed')
        engine = SimpleNamespace(provenance_deferred_snapshot=fail,
            feature_observation_deferred_snapshot=lambda: {'observations': {'pending': 0}})
        payload = RuntimeDebugLogService(SimpleNamespace(STORE=self.store, ENGINE=engine), None).export_payload()
        deferred = payload['engine']['deferred_persistence']
        self.assertIn('snapshot failed', deferred['provenance']['error'])
        self.assertEqual(deferred['feature_journal']['observations']['pending'], 0)

    def test_failed_session_open_is_retained_with_code_and_timeout(self):
        RUNTIME_DEBUG.set_enabled(True, clear=True)
        exc = sqlite3.OperationalError('recovery busy')
        exc.sqlite_errorcode, exc.sqlite_errorname = 261, 'SQLITE_BUSY_RECOVERY'
        try:
            with patch('storage.sqlite3.connect', side_effect=exc), self.assertRaises(sqlite3.OperationalError):
                with self.store.background_sqlite(), self.store.connection_session():
                    self.fail('failed open yielded')
            rows = [r for r in RUNTIME_DEBUG.export()['critical_spans']
                    if 'test_failed_session_open' in r['fields'].get('caller', '')]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['operation'], 'sqlite_session_open')
            self.assertEqual(rows[0]['fields']['sqlite_errorname'], 'SQLITE_BUSY_RECOVERY')
            self.assertEqual(rows[0]['fields']['busy_timeout_ms'], 250)
        finally:
            RUNTIME_DEBUG.set_enabled(False, clear=True)

    def test_transaction_failure_identifies_prepare_phase(self):
        RUNTIME_DEBUG.set_enabled(True, clear=True)
        c = MagicMock()
        c.execute.side_effect = sqlite3.OperationalError('prepare busy')
        try:
            with patch('storage.sqlite3.connect', return_value=c), self.assertRaises(sqlite3.OperationalError):
                with self.store.conn():
                    self.fail('failed setup yielded')
            rows = [r for r in RUNTIME_DEBUG.export()['critical_spans']
                    if 'test_transaction_failure_identifies' in r['fields'].get('caller', '')]
            self.assertEqual(rows[0]['fields']['phase'], 'configure')
        finally:
            RUNTIME_DEBUG.set_enabled(False, clear=True)

    def test_runtime_starts_keeper_before_extensions_and_stops_after_drains(self):
        source = (support.ROOT / 'adaptive_ai/src/main.py').read_text(encoding='utf-8')
        initialize = source.split('def initialize_runtime():', 1)[1].split('def ', 1)[0]
        self.assertLess(initialize.index('STORE.start_wal_keeper()'), initialize.index('prepare_runtime_extensions()'))
        shutdown = source.split('def shutdown_runtime():', 1)[1].split('def run_initialize_runtime', 1)[0]
        self.assertLess(shutdown.index('shutdown_step("runtime_events"'), shutdown.index('stop_wal_keeper'))

    def test_successful_transaction_reports_separate_stage_times(self):
        RUNTIME_DEBUG.set_enabled(True, clear=True)
        try:
            self.write('trace-stages')
            # Match this test's Store helper independently of unrelated fixture threads.
            rows = [r for r in RUNTIME_DEBUG.export()['entries'] if r['kind']=='end'
                    and r['operation']=='sqlite_transaction' and 'test_release_154_wal_lifecycle.write:' in r['fields'].get('caller','')]
            self.assertEqual(len(rows), 1)
            for key in ['prepare_ms','body_ms','commit_ms','close_ms']:
                self.assertGreaterEqual(rows[0]['fields'][key], 0)
            self.assertEqual(rows[0]['status'], 'ok')
        finally:
            RUNTIME_DEBUG.set_enabled(False, clear=True)
