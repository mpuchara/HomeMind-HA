"""Expected storage pressure must not become an authorization/error storm."""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from storage import Store
from storage_budget import StorageBudget, StoragePressure, StorageRetry, MIB
from provenance import ProvenanceJournal
from history import HistoryManager
from training_queue import TrainingQueue
from test_training_queue import FakeStore, FakeEngine, FakeHistory
import test_executor as executor_fixture
from provenance_runtime import install


class Pressure177Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
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
