"""Slow durable writers must not block sensor ingestion or warm shadow observation."""
import json
import tempfile
import threading
import time
import unittest
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import agent, state
from storage import Store
from context_engine import ContextEngine
from context_row_buffer import ContextRowBuffer
from context_tournament import ContextTournament
from context_tournament_quality import install_sensor_quality
from context_tournament_policy_candidate import install_policy_candidates
import context_tournament_promotion as promotion
from policy import MultiHorizonPolicy
from context import ExplicitFeatureSchema
from teaching import Teaching


class PausedTournament(ContextTournament):
    def _shadow_writer(self):
        # The real writer is alive, but tests decide when commits may proceed.
        self.engine.stop_event.wait()


class ContextPersistence152Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.db')
        chooser, migrate = promotion._choose_schema_after_promotion, promotion._migrate_schema
        self.addCleanup(setattr, promotion, '_choose_schema_after_promotion', chooser)
        self.addCleanup(setattr, promotion, '_migrate_schema', migrate)
        with self.store.conn() as c:
            c.execute('CREATE TABLE test_rows(aid TEXT,eid TEXT,v INTEGER,raw TEXT,PRIMARY KEY(aid,eid))')
        self.buffer = ContextRowBuffer(self.store, 'test_rows',
            'INSERT INTO test_rows VALUES(?,?,?,?) ON CONFLICT(aid,eid) DO UPDATE SET v=excluded.v,raw=excluded.raw')

    def disk(self):
        with self.store.conn() as c:
            return [tuple(row) for row in c.execute('SELECT * FROM test_rows ORDER BY eid')]

    def launch(self, operation):
        done, errors = threading.Event(), []
        def run():
            try:
                operation()
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        thread = threading.Thread(target=run)
        thread.start()
        return thread, done, errors

    def blocked_store(self, operation):
        held, release = threading.Event(), threading.Event()
        def writer():
            with self.store.lock, self.store.conn() as c:
                c.execute("INSERT OR REPLACE INTO test_rows VALUES('block','writer',0,'{}')")
                held.set()
                release.wait(5)
        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(held.wait(3))
        caller, done, errors = self.launch(operation)
        try:
            completed = done.wait(2)
        finally:
            release.set()
            thread.join(5)
            caller.join(5)
        self.assertTrue(completed, 'realtime work waited for the unrelated durable writer')
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, [])

    def tournament(self):
        a = self.store.create_agent(agent(mode='shadow'))
        primary, challenger = 'binary_sensor.primary', 'binary_sensor.precursor'
        states = {a['target_entity']: state(a['target_entity'], 'off'),
                  primary: state(primary, 'on', device_class='occupancy'),
                  challenger: state(challenger, 'on', device_class='occupancy')}
        policy = MultiHorizonPolicy(a, states, {}, set())
        policy.schema = ExplicitFeatureSchema(policy.dims, [primary])
        policy.selection_meta = {'selection_reasons': {primary: ['historical']}}
        engine = SimpleNamespace(stop_event=threading.Event(), models={a['id']: policy},
            state_map=states, entity_registry={}, context_relevance={a['id']: {challenger: .9}}, context=None,
            temporal_history=None, state_revision=1, runtime={a['id']: {'last_prediction': 0.,
            'last_change_origin': 'external'}}, lock=threading.RLock())
        service = PausedTournament(self.store, engine)
        def stop():
            engine.stop_event.set()
            service._shadow_writer_thread.join(3)
        self.addCleanup(stop)
        service._eligible_entities = lambda *args: {primary, challenger}
        install_sensor_quality(service)
        install_policy_candidates(service)
        service.sync_agent(a, policy=policy)
        service.observe_shadow(a, states, set(states))
        return a, states, service

    def test_buffer_keeps_latest_complete_cumulative_votes_and_detaches_rows(self):
        for count in range(50):
            row = ['agent', 'sensor', count, json.dumps({'samples': list(range(count))})]
            self.buffer.submit([row])
            row[2] = -1
        self.assertEqual(self.buffer.snapshot()['pending'], 1)
        self.assertEqual(self.buffer.flush(), 1)
        row = self.disk()[0]
        self.assertEqual(row[2], 49)
        self.assertEqual(json.loads(row[3])['samples'], list(range(49)))

    def test_queue_submission_does_not_wait_for_store_lock_or_sqlite_writer(self):
        self.blocked_store(lambda: self.buffer.submit([('agent', 'sensor', 1, '{}')]))
        self.assertEqual(self.buffer.flush(), 1)

    def test_failed_batch_restores_all_rows_with_newest_pending_snapshot_winning(self):
        self.buffer.submit([('a', 'first', 1, '{}'), ('a', 'second', 1, '{}')])
        def fail():
            self.buffer.submit([('a', 'first', 2, '{"new":true}')])
            raise OSError('disk unavailable')
        with patch.object(self.store, 'conn', side_effect=fail):
            with self.assertRaises(OSError):
                self.buffer.flush()
        self.assertEqual(self.buffer.snapshot(), dict(pending=2, flushed=0, errors=1))
        self.buffer.flush()
        self.assertEqual([row[2] for row in self.disk()], [2, 1])

    def test_sql_failure_rolls_back_entire_snapshot_before_retry(self):
        self.buffer.submit([('a', 'first', 1, '{}'), ('a', 'second', 2)])
        with self.assertRaises(Exception):
            self.buffer.flush()
        self.assertEqual(self.disk(), [])
        self.buffer.submit([('a', 'second', 2, '{}')])
        self.assertEqual(self.buffer.flush(), 2)

    def test_serial_drains_never_commit_old_snapshot_after_new_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store.conn
        calls = []
        @contextmanager
        def paused():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
            with original() as c:
                yield c
        self.buffer.submit([('a', 'sensor', 1, '{}')])
        with patch.object(self.store, 'conn', paused):
            first, _, errors = self.launch(self.buffer.flush)
            second = None
            try:
                self.assertTrue(entered.wait(3))
                self.buffer.submit([('a', 'sensor', 2, '{}')])
                second, _, more = self.launch(self.buffer.flush)
                self.assertFalse(self.buffer.flush_lock.acquire(blocking=False))
            finally:
                release.set()
                first.join(5)
                if second is not None:
                    second.join(5)
        self.assertEqual(errors + more, [])
        self.assertEqual(self.disk()[0][2], 2)

    def test_warm_pool_and_quality_observation_continue_under_durable_writer_contention(self):
        a, states, service = self.tournament()
        self.blocked_store(lambda: service.observe_shadow(a, states, set(states)))
        stats = service.shadow_persistence_snapshot()['context_rows']
        self.assertGreater(stats['observed_pool']['pending'], 0)
        service._flush_background_context()
        with self.store.conn() as c:
            rows = list(c.execute('SELECT opportunities FROM context_tournament_observed_pool'))
            quality = list(c.execute('SELECT opportunities FROM context_tournament_sensor_quality'))
        self.assertTrue(rows)
        self.assertTrue(quality)
        self.assertTrue(all(row[0] == 2 for row in rows + quality))

    def test_target_labels_wake_writer_without_waiting_for_shadow_flush(self):
        a, states, service = self.tournament()
        states[a['target_entity']] = state(a['target_entity'], 'on')
        # Schema reconciliation still has a durable boundary; isolate the residual
        # observation here to verify removal of the former forced model flush.
        service.engine.runtime[a['id']]['last_prediction'] = 1.
        with patch.object(service, '_flush_shadow_models', side_effect=AssertionError('realtime commit')):
            result = ContextTournament.observe_shadow(service, a, states, {a['target_entity']})
        self.assertGreater(result['scored'], 0)
        self.assertTrue(service._shadow_flush_event.is_set())
        self.assertGreater(service.shadow_persistence_snapshot()['pending'], 0)
        service._flush_background_context()

    def test_pool_counters_preserve_labels_and_short_visits_before_commit_and_restart(self):
        a, states, service = self.tournament()
        for value in ('on', 'off', 'on', 'off'):
            states[a['target_entity']] = state(a['target_entity'], value)
            service.observe_shadow(a, states, {a['target_entity']})
        for _ in range(3):
            service.observe_shadow(a, states, set())
        self.assertGreater(service.state(a['id'])['observed_pool_count'], 0)
        service._flush_background_context()
        with self.store.conn() as c:
            rows = [dict(row) for row in c.execute('SELECT * FROM context_tournament_observed_pool')]
        self.assertTrue(all(row['opportunities'] == 8 for row in rows))
        self.assertTrue(all(len(json.loads(row['samples_json'])) == 4 for row in rows))
        self.assertTrue(all([x['label'] for x in json.loads(row['samples_json'])] == [1., 0., 1., 0.] for row in rows))
        service.engine.stop_event.set()
        service._shadow_writer_thread.join(3)
        restarted = ContextTournament(self.store, SimpleNamespace())
        self.assertEqual(restarted._load_shadow_model(a['id'], 'binary_sensor.precursor', 2)['samples'], 4)

    def test_writer_shutdown_flushes_context_rows_and_models(self):
        service = ContextTournament(self.store, SimpleNamespace())
        self.buffer.submit([('a', 'sensor', 1, '{}')])
        service._context_row_buffers['test'] = self.buffer
        service._save_shadow_model('a', 'sensor', dict(version=1, action_count=2, samples=3, counts={}))
        service.engine.stop_event = threading.Event()
        service.engine.stop_event.set()
        service._shadow_writer()
        self.assertEqual(self.disk()[0][2], 1)
        self.assertEqual(service.shadow_persistence_snapshot()['pending'], 0)

    def test_failed_context_table_does_not_starve_other_tables_or_shadow_models(self):
        service = ContextTournament(self.store, SimpleNamespace())
        service._context_row_buffers['bad'] = SimpleNamespace(flush=lambda: (_ for _ in ()).throw(OSError('bad')))
        service._context_row_buffers['good'] = self.buffer
        self.buffer.submit([('a', 'sensor', 1, '{}')])
        service._save_shadow_model('a', 'sensor', dict(version=1, action_count=2, samples=3, counts={}))
        with self.assertRaises(OSError):
            service._flush_background_context()
        self.assertEqual(self.disk()[0][2], 1)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM context_tournament_shadow').fetchone()[0], 1)

    def test_context_save_releases_belief_lock_and_retains_new_live_observation(self):
        context = ContextEngine({'entity_area_mapping': {'binary_sensor.motion': 'bathroom'}}, self.store)
        context.configure({'binary_sensor.motion': state('binary_sensor.motion', 'off', device_class='occupancy')})
        context.observe('binary_sensor.motion', state('binary_sensor.motion', 'off'), time.time())
        entered, release = threading.Event(), threading.Event()
        original = self.store.meta_set
        def paused(key, value):
            entered.set()
            release.wait(5)
            return original(key, value)
        with patch.object(self.store, 'meta_set', side_effect=paused):
            writer, _, errors = self.launch(lambda: context.save(force=True))
            observer = None
            try:
                self.assertTrue(entered.wait(3))
                observer, done, more = self.launch(lambda: context.observe('binary_sensor.motion',
                    state('binary_sensor.motion', 'on'), time.time() + 1))
                completed = done.wait(2)
            finally:
                release.set()
                writer.join(5)
                if observer is not None:
                    observer.join(5)
        self.assertTrue(completed)
        self.assertEqual(errors + more, [])
        live_raw = json.dumps(context.home.export(), separators=(',', ':'), sort_keys=True)
        self.assertNotEqual(self.store.meta_get(context.ROOM_MODEL_KEY), live_raw)
        context.save(force=True)
        self.assertEqual(self.store.meta_get(context.ROOM_MODEL_KEY), live_raw)

    def test_teaching_recording_continues_during_slow_commit_and_keeps_new_records(self):
        teaching = Teaching(self.store)
        teaching.record('a', 0, 0, time.time())
        entered, release = threading.Event(), threading.Event()
        original = self.store.conn
        @contextmanager
        def paused():
            entered.set()
            release.wait(5)
            with original() as c:
                yield c
        with patch.object(self.store, 'conn', paused):
            writer, _, errors = self.launch(teaching.flush)
            recorder = None
            try:
                self.assertTrue(entered.wait(3))
                recorder, done, more = self.launch(lambda: teaching.record('a', 0, 1, time.time() + 1))
                completed = done.wait(2)
            finally:
                release.set()
                writer.join(5)
                if recorder is not None:
                    recorder.join(5)
        self.assertTrue(completed)
        self.assertEqual(errors + more, [])
        self.assertEqual(len(teaching.buffer), 1)
        teaching.flush()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM decision_history').fetchone()[0], 2)

    def test_teaching_failed_commit_restores_batch_before_new_records_with_bounded_overflow(self):
        teaching = Teaching(self.store)
        teaching.buffer = deque(maxlen=3)
        teaching.record('a', 0, 0, 1.)
        teaching.record('a', 0, 1, 2.)
        def fail():
            teaching.record('a', 1, 1, 3.)
            teaching.record('a', 1, 0, 4.)
            raise OSError('disk unavailable')
        with patch.object(self.store, 'conn', side_effect=fail):
            with self.assertRaises(OSError):
                teaching.flush()
        self.assertEqual([row[1] for row in teaching.buffer], [2., 3., 4.])
        self.assertEqual(teaching.dropped_records, 1)
        self.assertEqual(teaching.last_prune, 0)
        self.assertEqual(teaching.flush(), 3)

    def test_debug_export_exposes_pending_context_rows_without_database_io(self):
        from runtime_debug_log import RuntimeDebugLogService
        a, states, service = self.tournament()
        core = SimpleNamespace(ENGINE=service.engine)
        service.engine.context_tournament = service
        with patch.object(self.store, 'conn', side_effect=AssertionError('diagnostic DB read')):
            payload = RuntimeDebugLogService(core, SimpleNamespace()).export_payload()
        stats = payload['engine']['context_persistence']['context_rows']
        self.assertGreater(stats['observed_pool']['pending'], 0)
        self.assertEqual(stats['observed_pool']['errors'], 0)
        self.assertIn('context_rows_persist', payload['notes']['context_persistence_metrics'])

    def test_unobserved_health_lookup_does_not_inflate_pending_pool_count(self):
        a, states, service = self.tournament()
        count = service.state(a['id'])['observed_pool_count']
        service.sensor_pool_stats(a['id'], 'binary_sensor.not_observed')
        self.assertEqual(service.state(a['id'])['observed_pool_count'], count)

    def test_forced_teaching_shutdown_flush_waits_for_durable_writer_instead_of_skipping(self):
        teaching = Teaching(self.store)
        teaching.record('a', 0, 1, time.time())
        held, release, started = threading.Event(), threading.Event(), threading.Event()
        def hold():
            with self.store.lock:
                held.set()
                release.wait(5)
        owner = threading.Thread(target=hold)
        owner.start()
        self.assertTrue(held.wait(3))
        def flush():
            started.set()
            teaching.flush(force=True)
        writer, done, errors = self.launch(flush)
        try:
            self.assertTrue(started.wait(3))
            skipped = done.wait(.2)
        finally:
            release.set()
            owner.join(5)
            writer.join(5)
        self.assertFalse(skipped)
        self.assertEqual(errors, [])
        self.assertEqual(len(teaching.buffer), 0)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM decision_history').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
