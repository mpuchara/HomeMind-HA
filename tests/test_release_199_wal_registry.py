"""Real WAL recovery, registry semantics and writer connection reuse."""
import copy
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import test_context_tournament_events as event_fixture
from context_engine import ContextEngine
from context_tournament import ContextTournament
from context_tournament_events import install_context_events
from engine import Engine
from settings import OPTIONS
from storage import Store
from storage_budget import StoragePressure, StorageRetry
from sqlite_background import run_connection_worker
from wal_checkpoint import WALCheckpointWorker


class Runtime199Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'runtime.db')

    def engine(self):
        engine = Engine.__new__(Engine)
        engine.lock = threading.RLock()
        engine.context = ContextEngine(OPTIONS, self.store)
        engine.state_map = {}
        engine.entity_registry = {}
        engine._entity_registry_raw = engine._device_registry_raw = engine._area_registry_raw = None
        engine.registry_refresh_stats = dict(duplicates=0, entity_updates=0, device_updates=0,
                                            area_updates=0, max_duration_ms=0.)
        engine.models = {'trained': object()}
        engine.agent_index_at = 10.
        engine.agent_index_revision = 7
        engine.state_revision = 1
        engine.runtime = {}
        engine._inference_tls = threading.local()
        engine.stop_event = threading.Event()
        engine.wake_event = threading.Event()
        return engine

    def test_timestamp_and_order_changes_preserve_live_context_but_refresh_metadata(self):
        engine = self.engine()
        rows = [dict(entity_id='light.a', area_id='kitchen', modified_at=1),
                dict(entity_id='sensor.b', area_id='hall', modified_at=1)]
        self.assertTrue(engine._registry_update('entity', rows))
        revision = engine.context.registry_revision
        engine.agent_index_at, engine.agent_index_revision = 20., 9
        engine.context._adaptive_cache['keep'] = {'known': True}
        rows.reverse()
        rows[0]['modified_at'] = 2
        self.assertFalse(engine._registry_update('entity', rows))
        self.assertEqual(engine.context.registry_revision, revision)
        self.assertEqual(engine.context._adaptive_cache['keep'], {'known': True})
        self.assertEqual((engine.agent_index_at, engine.agent_index_revision), (20., 9))
        self.assertEqual(engine.entity_registry['sensor.b']['modified_at'], 2)
        self.assertIn('trained', engine.models)

    def test_semantic_changes_and_nested_source_mutations_still_reconfigure(self):
        engine = self.engine()
        rows = [dict(entity_id='light.a', area_id='kitchen', options={'signal': {'enabled': True}})]
        engine._registry_update('entity', rows)
        revision = engine.context.registry_revision
        rows[0]['options']['signal']['enabled'] = False
        self.assertTrue(engine._registry_update('entity', rows))
        self.assertEqual(engine.context.registry_revision, revision + 1)
        rows[0]['area_id'] = 'hall'
        self.assertTrue(engine._registry_update('entity', rows))
        self.assertEqual(engine.entity_registry['light.a']['area_id'], 'hall')

    def test_device_and_area_metadata_changes_do_not_reset_context(self):
        engine = self.engine()
        for kind, row in [('device', dict(id='d', area_id='kitchen', modified_at=1)),
                          ('area', dict(area_id='kitchen', name='Kitchen', modified_at=1))]:
            engine._registry_update(kind, [row])
            revision = engine.context.registry_revision
            row['modified_at'] = 2
            self.assertFalse(engine._registry_update(kind, [row]))
            self.assertEqual(engine.context.registry_revision, revision)
            row['name'] = 'Renamed'
            self.assertTrue(engine._registry_update(kind, [row]))

    def test_storage_pause_is_global_throttled_and_resumes_on_success(self):
        engine = self.engine()
        agents = [support.agent(id='a'), support.agent(id='b')]
        exc = StoragePressure('WAL admission paused')
        exc.reason = 'wal_budget'
        engine.process_agent = Mock(side_effect=exc)
        self.store._wal_checkpoint_worker = SimpleNamespace(active=False, request_recovery=Mock())
        snapshot = ({}, 1, {}, 0)
        with patch('engine.STORE', self.store), patch.object(self.store, 'event') as event:
            for _ in range(3):
                engine.process_target(agents, snapshot=snapshot)
            self.assertEqual(event.call_count, 1)
            self.assertEqual(event.call_args.args[2], 'runtime_storage_paused')
            self.assertEqual(engine.storage_pressure_stats['paused_passes'], 6)
            self.assertTrue(engine.runtime['a']['storage_pressure']['active'])
            engine.process_agent.side_effect = None
            engine.process_target(agents, snapshot=snapshot)
            self.assertFalse(engine.runtime['a']['storage_pressure']['active'])
        self.assertEqual(self.store._wal_checkpoint_worker.request_recovery.call_count, 6)

    def test_non_storage_pipeline_error_remains_agent_error(self):
        engine = self.engine()
        engine.process_agent = Mock(side_effect=ValueError('invalid policy'))
        with patch('engine.STORE', self.store), patch.object(self.store, 'event') as event:
            engine.process_target([support.agent()], snapshot=({}, 1, {}, 0))
        self.assertEqual(event.call_args.args[2], 'agent_error')

    def test_quiet_window_lets_existing_native_writer_finish_and_truncates_exact_rows(self):
        with self.store.conn() as c:
            c.execute('CREATE TABLE recovery(n INTEGER)')
        worker = WALCheckpointWorker(self.store)
        worker.RECLAIM_THRESHOLD_BYTES = 1
        worker.quiesce_seconds = .1
        connection = sqlite3.connect(self.store.path, timeout=0, isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute('INSERT INTO recovery VALUES(0)')
        held = threading.Event()
        release = threading.Event()
        def writer():
            c = sqlite3.connect(self.store.path)
            try:
                c.execute('INSERT INTO recovery VALUES(1)')
                held.set()
                if not release.wait(5):
                    raise RuntimeError('test did not open the quiet window')
                c.execute('INSERT INTO recovery VALUES(2)')
                c.commit()
            finally:
                c.close()
        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(held.wait(2))
        def quiet_window(seconds):
            self.assertEqual(seconds, .1)
            self.assertTrue(self.store._wal_reclaiming.is_set())
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            return False
        try:
            # Coordinate the writer at the actual quiet-window boundary. A
            # loaded CI runner need not schedule its commit inside 100 wall ms.
            with patch.object(worker.stop_event, 'wait', side_effect=quiet_window):
                self.assertTrue(worker.reclaim(connection, dict(busy=0, remaining_frames=0), now=100))
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(self.store._wal_reclaiming.is_set())
        self.assertEqual(worker.snapshot()['reclaim_successes'], 1)
        with self.store.conn() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT n FROM recovery ORDER BY n')], [0, 1, 2])

    def test_stop_cancels_quiet_window_without_touching_sqlite(self):
        worker = WALCheckpointWorker(self.store)
        worker.stop_event.set()
        connection = Mock()
        with patch.object(worker, 'sample_storage', return_value=worker.RECLAIM_THRESHOLD_BYTES + 1):
            self.assertFalse(worker.reclaim(connection, dict(busy=0, remaining_frames=0), now=100))
        connection.execute.assert_not_called()
        self.assertFalse(self.store._wal_reclaiming.is_set())

    def test_recovery_request_wakes_existing_checkpoint_before_five_second_poll(self):
        keeper = sqlite3.connect(self.store.path)
        keeper.execute('PRAGMA journal_mode').fetchone()
        self.addCleanup(keeper.close)
        with self.store.conn() as c:
            c.execute('CREATE TABLE recovery(n INTEGER)')
        worker = WALCheckpointWorker(self.store)
        worker.start()
        try:
            time.sleep(1.05)
            worker.request_recovery()
            deadline = time.monotonic() + 2.
            while worker.snapshot()['runs'] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            for _ in range(100):
                worker.request_recovery()
            time.sleep(.1)
            self.assertEqual(worker.snapshot()['runs'], 1)
        finally:
            worker.stop()

    def test_shadow_writer_reuses_one_connection_across_committed_batches(self):
        connects = []
        native = sqlite3.connect
        def connect(*args, **kwargs):
            if threading.current_thread().name == 'adaptive-ai-context-shadow-writer':
                connects.append(1)
            return native(*args, **kwargs)
        engine = SimpleNamespace(stop_event=threading.Event())
        with patch('storage.sqlite3.connect', side_effect=connect):
            service = ContextTournament(self.store, engine)
            try:
                for i in range(1, 4):
                    service._save_shadow_model('a', 'sensor', dict(version=1, action_count=2, samples=i, counts={'x': [i, 0]}))
                    service._shadow_flush_event.set()
                    deadline = time.monotonic() + 3.
                    while service.shadow_persistence_snapshot()['flushed'] < i:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
            finally:
                engine.stop_event.set()
                service._shadow_flush_event.set()
                service._shadow_writer_thread.join(3.)
        self.assertFalse(service._shadow_writer_thread.is_alive())
        self.assertEqual(len(connects), 1)
        fresh = ContextTournament(self.store, SimpleNamespace())
        self.assertEqual(fresh._load_shadow_model('a', 'sensor', 2)['samples'], 3)

    def test_worker_connection_setup_busy_retries_instead_of_killing_writer(self):
        stop, retry, report = threading.Event(), StorageRetry(), Mock()
        native = sqlite3.connect
        connects = [0]
        def connect(*args, **kwargs):
            connects[0] += 1
            if connects[0] == 1:
                raise sqlite3.OperationalError('database is locked')
            return native(*args, **kwargs)
        def body():
            with self.store.conn() as c:
                c.execute('CREATE TABLE worker_survived(n INTEGER)')
                c.execute('INSERT INTO worker_survived VALUES(1)')
            stop.set()
        with patch('storage.sqlite3.connect', side_effect=connect), patch.object(stop, 'wait', return_value=False):
            run_connection_worker(self.store, stop, body, retry, report)
        self.assertEqual(connects[0], 2)
        self.assertEqual(retry.busy_retries, 1)
        report.assert_not_called()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT n FROM worker_survived').fetchone()[0], 1)


class EventState199Tests(unittest.TestCase):
    def setUp(self):
        self.service = event_fixture.FakeService()
        self.addCleanup(self.service.store.db.close)
        template = copy.deepcopy(self.service.models['sensor.new'])
        for i in range(4):
            self.service.models[f'sensor.{i}'] = copy.deepcopy(template)
        self.service.current['challenger_features'] = [f'sensor.{i}' for i in range(4)]
        install_context_events(self.service)

    def selects(self):
        queries = []
        self.service.store.db.set_trace_callback(queries.append)
        self.service.observe_shadow(self.service.agent)
        self.service.store.db.set_trace_callback(None)
        return [q for q in queries if q.startswith('SELECT') and 'context_tournament_event_state' in q]

    def test_four_challengers_use_one_read_and_events_stay_deduplicated(self):
        self.assertEqual(len(self.selects()), 1)
        first = copy.deepcopy(self.service.store.events)
        self.assertEqual(len(first), 4)
        self.assertEqual(len(self.selects()), 1)
        self.assertEqual(self.service.store.events, first)

    def test_next_observation_refreshes_after_external_row_change(self):
        self.selects()
        self.service.store.db.execute("DELETE FROM context_tournament_event_state WHERE entity_id='sensor.0'")
        self.service.store.db.commit()
        self.selects()
        self.assertEqual(len(self.service.store.events), 5)

    def test_exception_discards_frame_and_new_challenger_is_not_fabricated(self):
        self.selects()
        self.service.mutate_on_observe = Mock(side_effect=ValueError('bad sample'))
        with self.assertRaises(ValueError):
            self.selects()
        self.service.mutate_on_observe = None
        self.service.current['challenger_features'] = ['sensor.new']
        self.selects()
        self.assertEqual(self.service.store.events[-1]['data']['entity_id'], 'sensor.new')
