"""Real WAL contention, typed multi-queue errors and cooperative retry admission."""
import json
import gc
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import observation_contract as observation
import fast_light_objective as fast
from context import TemporalHistory
from context_row_buffer import ContextRowBuffer
from context_tournament import ContextTournament
from rewards import RewardEngine
from sqlite_background import DeferredBatchError, is_sqlite_busy
from storage import Store
from storage_budget import MIB, StorageRetry, StoragePressure
from wal_checkpoint import WALCheckpointWorker


class DeferredContention196Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(gc.collect)
        self.store = Store(Path(temp.name) / 'test.db')
        with self.store.conn() as c:
            c.execute('CREATE TABLE rows(a TEXT,e TEXT,n INTEGER,PRIMARY KEY(a,e))')
        self.sql = 'INSERT INTO rows VALUES(?,?,?) ON CONFLICT(a,e) DO UPDATE SET n=excluded.n'

    def test_typed_aggregate_recognizes_every_busy_result(self):
        for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_LOCKED):
            exc = sqlite3.OperationalError('native failure')
            exc.sqlite_errorcode = code
            error = DeferredBatchError([('observations', exc), ('windows', exc)])
            retry = StorageRetry()
            self.assertTrue(is_sqlite_busy(error))
            self.assertFalse(retry.defer(error))
            self.assertEqual(retry.busy_retries, 1)
            self.assertGreater(retry.delay(), .9)

    def test_mixed_io_failure_is_reported_even_beside_busy(self):
        error = DeferredBatchError([('observations', sqlite3.OperationalError('database is locked')),
                                    ('windows', sqlite3.OperationalError('disk I/O error'))])
        self.assertFalse(is_sqlite_busy(error))
        self.assertTrue(StorageRetry().defer(error))
        self.assertIn('windows: OperationalError: disk I/O error', str(error))
        self.assertFalse(is_sqlite_busy(RuntimeError('database is locked')))
        self.assertFalse(is_sqlite_busy(DeferredBatchError([])))

    def test_reclamation_defers_only_background_write_and_keeps_reads(self):
        self.store._wal_reclaiming.set()
        with self.store.connection_session() as session:
            with self.store.background_sqlite(), self.store.conn() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM rows').fetchone()[0], 0)
                with self.assertRaises(sqlite3.OperationalError) as result:
                    c.cursor().execute("INSERT INTO rows VALUES('a','sensor',1)")
                self.assertEqual(result.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                self.assertFalse(c.in_transaction)
            # Explicit durability must remain usable, even when maintenance is active.
            with self.store.conn() as c:
                c.execute("INSERT INTO rows VALUES('a','sensor',2)")
            self.assertEqual(session.execute('SELECT n FROM rows').fetchone()[0], 2)
        self.assertEqual(self.store.sqlite_snapshot()['wal_writer_deferrals'], 1)

    def test_gate_is_thread_local(self):
        self.store._wal_reclaiming.set()
        errors = []
        def explicit():
            try:
                with self.store.conn() as c:
                    c.execute("INSERT INTO rows VALUES('a','explicit',1)")
            except Exception as exc:
                errors.append(exc)
        with self.store.background_sqlite():
            thread = threading.Thread(target=explicit)
            thread.start(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_running_transaction_is_not_interrupted(self):
        with self.store.background_sqlite(), self.store.conn() as c:
            c.execute("INSERT INTO rows VALUES('a','first',1)")
            self.store._wal_reclaiming.set()
            c.executemany('INSERT INTO rows VALUES(?,?,?)', [('a', 'second', 2)])
        self.assertEqual(self.store.sqlite_snapshot()['wal_writer_deferrals'], 0)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM rows').fetchone()[0], 2)

    def test_deferred_buffer_retains_latest_counters_until_reclaim_finishes(self):
        buffer = ContextRowBuffer(self.store, 'rows', self.sql)
        buffer.submit([('a', 'sensor', 1)])
        self.store._wal_reclaiming.set()
        with self.store.background_sqlite(), patch.object(self.store, 'conn', side_effect=AssertionError('SQL attempted')):
            with self.assertRaises(sqlite3.OperationalError):
                buffer.flush()
        self.assertEqual(buffer.snapshot()['pending'], 1)
        buffer.submit([('a', 'sensor', 9)])
        self.store._wal_reclaiming.clear()
        with self.store.background_sqlite():
            self.assertEqual(buffer.flush(), 1)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT n FROM rows').fetchone()[0], 9)

    def test_shadow_waits_before_json_encoding_then_restarts_exactly(self):
        service = ContextTournament(self.store, SimpleNamespace())
        source = dict(version=1, action_count=2, samples=7, counts={'0': [3, 4]})
        service._save_shadow_model('a', 'sensor', source)
        self.store._wal_reclaiming.set()
        with self.store.background_sqlite(), patch('context_tournament.snapshot_json', side_effect=AssertionError('encoded')):
            with self.assertRaises(sqlite3.OperationalError):
                service._flush_shadow_models()
        self.assertEqual(service.shadow_persistence_snapshot()['pending'], 1)
        self.store._wal_reclaiming.clear()
        service._flush_shadow_models()
        fresh = ContextTournament(self.store, SimpleNamespace())
        self.assertEqual(fresh._load_shadow_model('a', 'sensor', 2), source)

    def test_reclaim_admission_marker_covers_sql_and_clears_on_io_error(self):
        worker = WALCheckpointWorker(self.store)
        worker.RECLAIM_THRESHOLD_BYTES = 128 * MIB
        connection = Mock()
        def run(sql):
            self.assertTrue(self.store._wal_reclaiming.is_set())
            if 'TRUNCATE' in sql:
                raise sqlite3.OperationalError('disk I/O error')
            return Mock()
        connection.execute.side_effect = run
        with patch.object(worker, 'sample_storage', return_value=200 * MIB):
            self.assertFalse(worker.reclaim(connection, {'busy': 0, 'remaining_frames': 0}, now=100))
        self.assertFalse(self.store._wal_reclaiming.is_set())
        self.assertEqual(worker.snapshot()['reclaim_errors'], 1)

    def test_reclaim_marker_clears_on_success_and_native_busy(self):
        for busy in (0, 1):
            worker = WALCheckpointWorker(self.store)
            worker.RECLAIM_THRESHOLD_BYTES = 128 * MIB
            connection = Mock()
            def run(sql):
                self.assertTrue(self.store._wal_reclaiming.is_set())
                return SimpleNamespace(fetchone=lambda: (busy, 0, 0))
            connection.execute.side_effect = run
            with patch.object(worker, 'sample_storage', return_value=200 * MIB):
                self.assertEqual(worker.reclaim(connection, {'busy': 0, 'remaining_frames': 0}, now=100), not busy)
            self.assertFalse(self.store._wal_reclaiming.is_set())

    def journal_fixture(self):
        restore = observation.install_training_contract()['restore']
        self.addCleanup(restore)
        agent = support.agent(id='a')
        engine = SimpleNamespace(stop_event=threading.Event(), lock=threading.RLock(), models={},
            agent_index_at=1., agent_configs={'a': agent}, temporal_history=TemporalHistory(),
            on_state_changed=lambda *_: None, refresh_states=lambda: {'sensor.radar': support.state('sensor.radar', 'on')},
            process_agent=lambda *_: None, executor=SimpleNamespace(submit=Mock(return_value='accepted')))
        with patch('observation_contract.threading.Thread'), patch('observation_contract._migrate_models', return_value=[]):
            observation.install(SimpleNamespace(STORE=self.store, ENGINE=engine))
        engine.models['a'] = SimpleNamespace(schema=SimpleNamespace(entities=['sensor.radar']))
        engine.refresh_states()
        engine.executor.submit(SimpleNamespace(agent_id='a', intent_id='i', created_at=time.time()))
        return engine

    def test_real_journal_lock_preserves_both_queues_and_native_codes(self):
        engine = self.journal_fixture()
        blocker = sqlite3.connect(self.store.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            with self.store.background_sqlite(), self.assertRaises(DeferredBatchError) as caught:
                engine.flush_feature_journal()
            self.assertTrue(is_sqlite_busy(caught.exception))
            self.assertEqual({name for name, _ in caught.exception.errors}, {'observations', 'windows'})
            self.assertTrue(all(exc.sqlite_errorcode == sqlite3.SQLITE_BUSY for _, exc in caught.exception.errors))
            self.assertFalse(StorageRetry().defer(caught.exception))
            snapshot = engine.feature_observation_deferred_snapshot()
            self.assertEqual(snapshot['observations']['pending'], 1)
            self.assertEqual(snapshot['windows']['pending'], 1)
        finally:
            blocker.rollback(); blocker.close()
        result = engine.flush_feature_journal()
        self.assertEqual(result, {'observations': 1, 'windows': 1})
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM feature_observation_events').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM feature_windows').fetchone()[0], 1)

    def test_journal_admission_does_not_pop_or_encode_pending_evidence(self):
        engine = self.journal_fixture()
        self.store._wal_reclaiming.set()
        with self.store.background_sqlite(), patch.object(engine.feature_journal, 'record_batch', side_effect=AssertionError('recorded')):
            with self.assertRaises(sqlite3.OperationalError):
                engine.flush_feature_journal()
        snapshot = engine.feature_observation_deferred_snapshot()
        self.assertEqual(snapshot['observations']['pending'], 1)
        self.assertEqual(snapshot['windows']['pending'], 1)
        self.store._wal_reclaiming.clear()
        self.assertEqual(engine.flush_feature_journal(), {'observations': 1, 'windows': 1})

    def test_storage_pressure_still_rolls_back_after_admission(self):
        with self.assertRaises(StoragePressure), self.store.background_sqlite(), self.store.conn() as c:
            c.execute("INSERT INTO rows VALUES('a','guarded',1)")
            with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                c.commit()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM rows').fetchone()[0], 0)

    def fast_fixture(self):
        agent = support.agent(id='a', mode='shadow')
        source = {'weights': [0.25, -0.75], 'revision': 'unchanged'}
        policy = SimpleNamespace(horizons=[3], heads={3: Mock()},
            schema=SimpleNamespace(entities=['binary_sensor.radar']),
            features=Mock(return_value=({0: 1.0}, {}, {})), serialize=Mock(return_value=source))
        engine = SimpleNamespace(stop_event=threading.Event(), models={'a': policy}, runtime_for=lambda *_: {},
            runtime={'a': {'last_prediction': 1, 'prediction_horizon': 3, 'automation_priors': [{'enabled': True}]}}, temporal_history=None,
            state_map={}, executor=SimpleNamespace(reward_engine=RewardEngine()))
        service = SimpleNamespace(engine=engine, store=self.store, lock=threading.RLock(),
            observe_shadow=lambda *_: None, shadow_status=lambda *_: {},
            state=lambda *_: {'challenger_features': []}, _shadow_runtime={})
        metric = fast.metrics_module.metric_row
        self.addCleanup(setattr, fast.metrics_module, 'metric_row', metric)
        with patch('fast_light_objective.threading.Thread') as constructor, patch('fast_light_objective._patch_promotion_for_timing'):
            fast.install(service)
            writer = constructor.call_args.kwargs['target']
        # Actual confirmed timing outcome queues both statistics and a model checkpoint.
        with patch('fast_light_objective._primary_occupancy', return_value=('binary_sensor.radar', False)):
            service.observe_shadow(agent, {agent['target_entity']: support.state(agent['target_entity'], 'off')})
            service.observe_shadow(agent, {agent['target_entity']: support.state(agent['target_entity'], 'on')})
        self.assertEqual(service.fast_light_persistence_snapshot()['pending_metrics'], 1)
        self.assertEqual(service.fast_light_persistence_snapshot()['pending_models'], 1)
        closure = dict(zip(writer.__code__.co_freevars, (cell.cell_contents for cell in writer.__closure__)))
        return service, engine, policy, source, writer, closure['persistence_event']

    def run_fast_retry(self, reclaim):
        service, engine, policy, source, writer, event = self.fast_fixture()
        blocker = None
        if reclaim:
            self.store._wal_reclaiming.set()
        else:
            blocker = sqlite3.connect(self.store.path)
            blocker.execute('BEGIN IMMEDIATE')
        thread = threading.Thread(target=writer)
        thread.start()
        event.set()
        try:
            deadline = time.monotonic() + 3
            while service.fast_light_persistence_snapshot()['storage_retry']['busy_retries'] == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            snapshot = service.fast_light_persistence_snapshot()
            self.assertEqual(snapshot['pending_models'], 1)
            self.assertEqual(snapshot['pending_metrics'], 1)
            policy.serialize.assert_not_called()
            self.assertFalse(any(row['kind'] == 'fast_light_persistence_failed' for row in self.store.list_events()))
            # Producer wakeups must not bypass retry cooldown.
            for _ in range(20):
                event.set()
            time.sleep(.05)
            self.assertEqual(service.fast_light_persistence_snapshot()['storage_retry']['busy_retries'], 1)
            if blocker:
                blocker.rollback(); blocker.close(); blocker = None
            self.store._wal_reclaiming.clear()
            event.set()
            deadline = time.monotonic() + 4
            while (service.fast_light_persistence_snapshot()['flushed_models'] != 1
                   or service.fast_light_persistence_snapshot()['storage_retry']['reason'] is not None):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            self.assertEqual(self.store.get_model('a'), {**source, '_history_watermark': 0})
            with self.store.conn() as c:
                packed = json.loads(c.execute('SELECT metrics_json FROM fast_light_timing_metrics').fetchone()[0])
            self.assertEqual(packed['on_samples'], 1)
            self.assertEqual(packed['on_success'], 1)
            self.assertIsNone(service.fast_light_persistence_snapshot()['storage_retry']['reason'])
        finally:
            if blocker:
                blocker.rollback(); blocker.close()
            self.store._wal_reclaiming.clear()
            engine.stop_event.set(); event.set(); thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_fast_light_real_lock_retries_without_losing_models_or_metrics(self):
        self.run_fast_retry(False)

    def test_fast_light_reclaim_preserves_checkpoint_without_serializing_it(self):
        self.run_fast_retry(True)


if __name__ == '__main__':
    unittest.main()
