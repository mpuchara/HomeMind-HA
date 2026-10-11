"""Expected storage pressure must not become an authorization/error storm."""
import sqlite3
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from storage import Store
from storage_budget import StorageBudget, StoragePressure, StorageRetry, MIB, run_storage_write
from provenance import ProvenanceJournal
from history import HistoryManager
from training_queue import TrainingQueue
from test_training_queue import FakeStore, FakeEngine, FakeHistory
import test_executor as executor_fixture
import test_release_080_training_process as process_fixture
from provenance_runtime import install


class Pressure177Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict('storage.OPTIONS', storage_budget_mode='manual'):
            self.store = Store(Path(self.temp.name) / 'test.db')
        self.budget = self.store.storage_budget

    def pressure(self):
        return patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=self.budget.reserve - MIB))

    def test_our_authorizer_denial_reports_reason_and_capacity(self):
        with self.pressure(), self.assertRaises(StoragePressure) as error, self.store.conn() as c:
            c.execute("INSERT INTO entity_history(entity_id,ts,state,source) VALUES('sensor.radar',1,'on','live')")
        self.assertIn('disk_reserve', str(error.exception))
        self.assertIn('WAL=', str(error.exception))
        self.assertIn('free=', str(error.exception))
        self.assertIsNone(c.storage_denial)

    def test_external_authorizer_denial_is_not_misclassified(self):
        with self.pressure(), self.store.conn() as c:
            c.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError) as error:
                c.execute("INSERT INTO app_meta VALUES('external','denied')")
            self.assertNotIsInstance(error.exception, StoragePressure)
            self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_AUTH)

    def test_pressure_has_cooldown_and_never_creates_error_event(self):
        retry = StorageRetry()
        with patch('storage_budget.time.monotonic', return_value=100):
            self.assertFalse(retry.defer(StoragePressure('WAL budget')))
            self.assertEqual(retry.delay(), 5)
            self.assertEqual(retry.snapshot()['pauses'], 1)
        retry.recovered()
        self.assertIsNone(retry.reason)
        self.assertEqual(retry.delay(), 0)

    def test_real_failures_report_at_most_once_per_minute(self):
        retry = StorageRetry()
        with patch('storage_budget.time.monotonic', return_value=100):
            self.assertTrue(retry.defer(RuntimeError('broken')))
            for _ in range(100):
                self.assertFalse(retry.defer(RuntimeError('broken')))
            self.assertEqual(retry.delay(), 1)
        with patch('storage_budget.time.monotonic', return_value=160):
            self.assertTrue(retry.defer(RuntimeError('broken')))

    def test_paused_provenance_does_not_grow_pending_but_preserves_live_origin(self):
        journal = ProvenanceJournal(self.store)
        journal.record_event('light.test', support.state('light.test', 'on'), event_time=1, event_id='queued')
        with self.pressure():
            self.budget.sample()
            for index in range(100):
                journal.record_event('light.test', support.state('light.test', 'on'), event_time=2+index, event_id=str(index), origin='own_command')
        self.assertEqual(journal.pending_event_count(), 1)
        self.assertEqual(journal.event('99')['origin'], 'own_command')
        self.assertEqual(journal._dropped_pending_events, 100)
        self.budget.sample()
        self.assertEqual(journal.flush_events_batch(), 1)

    def test_queued_rebuild_waits_without_start_and_resumes_on_recovery(self):
        store = FakeStore()
        store.storage_budget = self.budget
        trace = []
        history = FakeHistory(store, trace)
        queue = TrainingQueue(history, store, FakeEngine(trace))
        queue.enqueue('a', rebuild=True)
        with self.pressure():
            self.assertFalse(queue._try_start_head())
            self.assertEqual(queue.status_for('a')['state'], 'queued')
            self.assertEqual(history.started, [])
            self.assertEqual(store.agents['a']['training_state'], 'waiting')
        self.assertTrue(queue._try_start_head())
        self.assertEqual(history.started, [('a', True)])
        history.complete('a')
        queue._finish_active_if_done()

    def test_direct_rebuild_admission_does_not_touch_model(self):
        manager = object.__new__(HistoryManager)
        with patch('history.STORE', self.store), patch.object(self.store, 'clear_learning') as clear, self.pressure():
            with self.assertRaises(StoragePressure):
                manager._start_agent_job('a', rebuild=True)
            clear.assert_not_called()

    def test_pressure_during_recorder_preparation_preserves_model(self):
        manager = object.__new__(HistoryManager)
        manager._training_bounds = Mock(return_value=(1, 2))
        manager._refresh_agent_history = Mock(side_effect=StoragePressure('WAL'))
        manager.set_status = Mock()
        agent = support.agent(id='a')
        with patch('history.STORE', self.store), patch.object(self.store, 'get_agent_config', return_value=agent), patch.object(self.store, 'clear_learning') as clear:
            with self.assertRaises(StoragePressure):
                manager._run_agent_indexing('a', rebuild=True)
            clear.assert_not_called()

    def test_interrupted_job_requeues_once_and_waits_for_recovery(self):
        store = FakeStore()
        store.storage_budget = self.budget
        trace = []
        history = FakeHistory(store, trace)
        queue = TrainingQueue(history, store, FakeEngine(trace))
        queue.enqueue('a', rebuild=True)
        queue._try_start_head()
        history.complete('a', state='paused', progress=0)
        store.agents['a']['benchmark_detail'] = {'storage_pressure': True}
        with self.pressure():
            self.assertTrue(queue._finish_active_if_done())
            self.assertFalse(queue._try_start_head())
            self.assertFalse(queue._finish_active_if_done())
            self.assertEqual(queue.snapshot()['queued_count'], 1)
        queue._try_start_head()
        store.agents['a']['benchmark_detail'] = {}
        history.complete('a')
        queue._finish_active_if_done()

    def test_real_wal_pause_retries_same_transaction_after_worker_reclaim(self):
        from contextlib import closing
        from wal_checkpoint import WALCheckpointWorker
        self.store.start_wal_keeper()
        self.addCleanup(self.store.stop_wal_keeper)
        self.budget.wal_limit = 512 * 1024
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as raw:
            raw.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT count(*) FROM app_meta').fetchone()
            for index in range(100):
                with self.store.conn() as c:
                    c.execute('INSERT INTO app_meta VALUES(?,?)', (str(index), 'x' * 8192))
                if self.budget.sample()['level'] == 'paused':
                    break
            self.assertEqual(self.budget.sample()['reason'], 'wal_budget')
            stop = Mock()
            stop.is_set.return_value = False
            def recover(seconds):
                self.assertFalse(self.store.lock._is_owned())
                reader.execute('COMMIT')
                with closing(sqlite3.connect(self.store.path, isolation_level=None)) as raw:
                    worker = WALCheckpointWorker(self.store)
                    self.assertTrue(worker.reclaim(raw, worker.checkpoint(raw)))
            stop.wait.side_effect = recover
            rows = [('sensor.radar', 1, 'on', {}, None, 'ha_history_full')]
            callback = Mock(side_effect=lambda: self.store.archive_batch(rows))
            self.assertEqual(run_storage_write(self.store, callback, stop), 1)
            self.assertEqual(callback.call_count, 2)
            stop.wait.assert_called_once_with(1.0)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM entity_history').fetchone()[0], 1)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_disk_and_database_pressure_never_wait_for_wal(self):
        stop = Mock()
        stop.is_set.return_value = False
        for reason in ('disk_reserve', 'database_budget', 'statement_payload_budget'):
            callback = Mock(side_effect=self.budget.pressure(reason))
            with self.assertRaises(StoragePressure):
                run_storage_write(self.store, callback, stop)
            callback.assert_called_once()
        stop.wait.assert_not_called()

    def test_wal_wait_has_timeout_and_remains_cancellable(self):
        stop = threading.Event()
        callback = Mock(side_effect=self.budget.pressure('wal_budget'))
        with self.assertRaises(StoragePressure):
            run_storage_write(self.store, callback, stop, timeout=0)
        callback.assert_called_once()
        cancel = threading.Event()
        cancel.set()
        callback.reset_mock()
        with self.assertRaises(InterruptedError):
            run_storage_write(self.store, callback, stop, cancel)
        callback.assert_not_called()

    def test_unrelated_sql_failure_is_not_retried(self):
        callback = Mock(side_effect=sqlite3.DatabaseError('not authorized'))
        with self.assertRaises(sqlite3.DatabaseError):
            run_storage_write(self.store, callback, threading.Event())
        callback.assert_called_once()

    def test_import_retains_payload_and_counts_only_committed_retry(self):
        manager = object.__new__(HistoryManager)
        manager.stop_event = Mock()
        manager.stop_event.is_set.return_value = False
        manager.stop_event.wait.return_value = False
        manager.job_cancel_event = None
        data = [[support.state('sensor.radar', 'on') | {'last_changed': '2026-10-11T12:00:00+00:00'},
                 support.state('sensor.radar', 'off') | {'last_changed': '2026-10-11T12:00:01+00:00'}]]
        original = self.store.archive_batch
        attempts = []
        def archive(rows):
            attempts.append(list(rows))
            if len(attempts) == 1:
                raise self.budget.pressure('wal_budget')
            return original(rows)
        with patch('history.STORE', self.store), patch.object(self.store, 'archive_batch', side_effect=archive):
            self.assertEqual(manager._archive_history_payload(data, 'ha_history_full'), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(self.store.archive_count(), 2)
        manager.stop_event.wait.assert_called_once_with(1.0)

    def test_worker_pressure_keeps_type_and_rolls_back_uncommitted_chunk(self):
        import storage as storage_module
        import training_process
        fixture = process_fixture.SupervisorRollbackContracts()
        fixture.setUp()
        try:
            manager = fixture._fake_history()
            job = fixture._supervisor_job(fixture.temp.name)
            aid = fixture.agent['id']
            def start(*args, **kwargs):
                fixture.store.add_historical_experience(aid, 2, 1, 1.0, 1.0, 10.0, {0: 2.0})
                fixture.store.save_model(aid, {'version': 1, 'value': 'uncommitted-child'})
                Path(job['result_path']).write_text(json.dumps({
                    'ok': False, 'error_type': 'StoragePressure',
                    'error': 'Storage protection: wal_budget', 'storage_pressure_reason': 'wal_budget',
                }), encoding='utf-8')
                return SimpleNamespace(pid=os.getpid(), poll=lambda: 2, wait=lambda timeout=None: 2)
            with patch.object(storage_module, 'STORE', fixture.store), \
                 patch.object(training_process, 'DATA_DIR', Path(fixture.temp.name)), \
                 patch.object(training_process, '_build_job', return_value=dict(job)), \
                 patch.object(training_process, '_prune_job_files', return_value=None), \
                 patch.object(training_process.subprocess, 'Popen', side_effect=start):
                with self.assertRaises(StoragePressure) as error:
                    training_process.run_isolated_training_chunk(manager, 1.0, 2.0, agent_ids={aid})
            self.assertEqual(error.exception.reason, 'wal_budget')
            self.assertEqual(manager.training_process_status['state'], 'paused')
            self.assertEqual(fixture.store.get_model(aid)['value'], 'pre-worker')
            self.assertEqual([row['target_history_id'] for row in fixture.store.list_historical_experiences(aid)], [1])
        finally:
            fixture.tearDown()

    def test_interrupted_rebuild_after_reset_resumes_saved_progress(self):
        store = FakeStore()
        trace = []
        history = FakeHistory(store, trace)
        queue = TrainingQueue(history, store, FakeEngine(trace))
        queue.enqueue('a', rebuild=True)
        queue._try_start_head()
        history.complete('a', state='paused', progress=.4)
        store.agents['a']['benchmark_detail'] = {'storage_pressure': True, 'rebuild_reset_completed': True}
        queue._finish_active_if_done()
        self.assertFalse(queue.jobs[0]['rebuild'])
        queue._try_start_head()
        self.assertEqual(history.started, [('a', True), ('a', False)])
        store.agents['a']['benchmark_detail'] = {}
        history.complete('a')
        queue._finish_active_if_done()


class DeferredPressure177Tests(unittest.TestCase):
    def test_writer_ignores_repeated_wakeups_during_pressure_without_error_storm(self):
        fixture = executor_fixture.ExecutorTests()
        fixture.setUp()
        try:
            install(SimpleNamespace(STORE=fixture.store, ENGINE=fixture.e))
            budget = fixture.store.storage_budget
            journal = fixture.e.provenance
            journal.record_event('light.test', support.state('light.test', 'on'), event_time=1, event_id='queued')
            with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=budget.reserve - MIB)), patch.object(fixture.store, 'event', wraps=fixture.store.event) as event:
                fixture.e.provenance_writer_event.set()
                deadline = time.monotonic() + 3
                while not fixture.e.provenance_deferred_snapshot()['storage_retry']['pauses'] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(fixture.e.provenance_deferred_snapshot()['storage_retry']['pauses'], 1)
                for _ in range(100):
                    fixture.e.provenance_writer_event.set()
                time.sleep(.1)
                self.assertEqual(fixture.e.provenance_deferred_snapshot()['storage_retry']['pauses'], 1)
                event.assert_not_called()
                self.assertEqual(journal.pending_event_count(), 1)
        finally:
            fixture.e.stop_event.set()
            fixture.e.provenance_writer_event.set()
            fixture.e.provenance_writer_thread.join(timeout=2)
            fixture.tearDown()

    def test_combined_flush_preserves_queues_and_storage_error_type_then_recovers(self):
        fixture = executor_fixture.ExecutorTests()
        fixture.setUp()
        try:
            install(SimpleNamespace(STORE=fixture.store, ENGINE=fixture.e))
            fixture.e.stop_event.set()
            fixture.e.provenance_writer_event.set()
            fixture.e.provenance_writer_thread.join(timeout=2)
            journal = fixture.e.provenance
            journal.record_event('light.test', support.state('light.test', 'on'), event_time=1, event_id='queued')
            budget = fixture.store.storage_budget
            with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=budget.reserve - MIB)):
                with self.assertRaises(StoragePressure):
                    fixture.store._flush_all_provenance()
                self.assertEqual(journal.pending_event_count(), 1)
            fixture.store._flush_all_provenance()
            self.assertEqual(journal.pending_event_count(), 0)
        finally:
            fixture.tearDown()
