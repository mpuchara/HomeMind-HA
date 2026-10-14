"""Real WAL writer contention must retain observed evidence without policy replay."""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import test_candidate_shadow_context as candidate_fixture
from cold_start_drift import AdaptationService
from context_tournament import ContextTournament
from deferred_evidence import DeferredEvidence
from episode_evaluator import EpisodeEvaluator
from episode_evaluator_runtime import install_tournament
from storage import Store
from storage_budget import StoragePressure


class RetainedEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'runtime.db')
        with self.store.conn() as c:
            c.execute('CREATE TABLE retained(k TEXT PRIMARY KEY,n INTEGER)')

    def write(self, payload):
        with self.store.lock, self.store.conn() as c:
            c.execute('INSERT OR REPLACE INTO retained VALUES(?,?)', (payload['k'], payload['n']))

    def blocker(self):
        blocker = sqlite3.connect(self.store.path)
        blocker.execute('BEGIN IMMEDIATE')
        self.addCleanup(blocker.close)
        return blocker

    def test_external_writer_retains_immutable_values_and_force_flushes_after_release(self):
        queue = DeferredEvidence(self.store, self.write)
        source = dict(k='observed', n=7)
        queue.submit('event', source)
        source['n'] = 99
        blocker = self.blocker()
        started = time.monotonic()
        self.assertEqual(queue.flush(), 0)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(queue.snapshot()['pending'], 1)
        self.assertEqual(queue.snapshot()['errors'], 0)
        self.assertEqual(queue.snapshot()['storage_retry']['busy_retries'], 1)
        blocker.rollback()
        self.assertEqual(queue.flush(force=True), 1)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT n FROM retained').fetchone()[0], 7)

    def test_native_disk_failure_is_visible_and_keeps_pending_evidence(self):
        queue = DeferredEvidence(self.store, Mock(side_effect=sqlite3.OperationalError('disk I/O error')))
        queue.submit('event', dict(k='a', n=1))
        with self.assertRaisesRegex(sqlite3.OperationalError, 'disk I/O error'):
            queue.flush()
        self.assertEqual(queue.snapshot()['pending'], 1)
        self.assertEqual(queue.snapshot()['errors'], 1)

    def test_force_flush_overrides_background_timeout_and_restores_its_admission(self):
        queue = DeferredEvidence(self.store, self.write)
        queue.submit('event', dict(k='barrier', n=1))
        held = threading.Event()
        def writer():
            c = sqlite3.connect(self.store.path)
            try:
                c.execute('BEGIN IMMEDIATE')
                held.set()
                time.sleep(.4)
            finally:
                c.rollback(); c.close()
        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(held.wait(2.))
        try:
            with self.store.background_sqlite():
                self.assertEqual(queue.flush(force=True), 1)
                with self.store.conn() as c:
                    self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 250)
        finally:
            thread.join(2.)
        self.assertFalse(thread.is_alive())

    def test_pressure_rolls_back_then_retries_exact_evidence(self):
        queue = DeferredEvidence(self.store, self.write)
        queue.submit('event', dict(k='a', n=1))
        with patch.object(self.store.storage_budget, 'check', side_effect=StoragePressure('disk reserve')):
            self.assertEqual(queue.flush(), 0)
        self.assertEqual(queue.snapshot()['pending'], 1)
        self.assertEqual(queue.flush(force=True), 1)

    def test_ram_budget_and_conflicting_duplicate_are_explicit_gaps(self):
        queue = DeferredEvidence(self.store, self.write, max_items=1, max_bytes=40)
        queue.submit('one', dict(k='a', n=1))
        self.assertFalse(queue.submit('one', dict(k='a', n=1)))
        with self.assertRaises(ValueError):
            queue.submit('one', dict(k='a', n=2))
        with self.assertRaises(BufferError):
            queue.submit('two', dict(k='b', n=2))
        self.assertEqual(queue.snapshot()['pending'], 1)
        self.assertLessEqual(queue.snapshot()['bytes'], 40)
        self.assertEqual(queue.flush(force=True), 1)
        with self.assertRaises(BufferError):
            queue.submit('large', dict(k='x' * 41, n=2))

    def test_read_only_observer_does_not_inherit_synthetic_reclaim_rejection(self):
        self.store._wal_reclaiming.set()
        with self.store.background_sqlite():
            with self.store.background_sqlite(retryable=False), self.store.conn() as c:
                c.execute("INSERT INTO retained VALUES('observation',1)")
            with self.store.conn() as c, self.assertRaises(sqlite3.OperationalError):
                c.execute("INSERT INTO retained VALUES('retained-batch',2)")
        self.store._wal_reclaiming.clear()
        self.assertEqual(self.store.sqlite_snapshot()['wal_writer_deferrals'], 1)

    def test_tournament_retry_preserves_final_endpoint_and_idempotent_fingerprint(self):
        evaluator = EpisodeEvaluator(self.store, clock=lambda: 100.0)
        runtime = {'last_prediction': 1., 'last_intent': {'decision_id': 'observed'}}
        engine = SimpleNamespace(runtime={'a': runtime}, state_map={})
        class Tournament:
            def __init__(self, store):
                self.store, self.engine, self._shadow_runtime = store, engine, {}
            def state(self, aid):
                return {'challenger_features': ['binary_sensor.hall']}
            def observe_shadow(self, agent, states=None, changed_entities=None):
                self._shadow_runtime[agent['id']] = {'predictions': {'binary_sensor.hall': {'shadow_index': 1}}}
            def shadow_status(self, agent):
                return {'challengers': []}
        service = install_tournament(Tournament(self.store), evaluator)
        cfg = support.agent(id='a')
        with patch('episode_evaluator_runtime.time.time', return_value=100.):
            service.observe_shadow(cfg, {'light.kitchen': support.state('light.kitchen', 'off')})
        blocker = self.blocker()
        with patch('episode_evaluator_runtime.time.time', return_value=102.):
            service.observe_shadow(cfg, {'light.kitchen': support.state('light.kitchen', 'on')})
        self.assertEqual(service.tournament_episode_persistence_snapshot()['pending'], 1)
        blocker.rollback()
        self.assertEqual(service.flush_tournament_episodes(force=True), 1)
        with self.store.conn() as c:
            row = dict(c.execute('SELECT * FROM episode_evaluator_episodes').fetchone())
            self.assertEqual((row['start_ts'], row['end_ts']), (100., 102.))
            self.assertEqual(c.execute('SELECT count(*) FROM episode_evaluator_policy_results').fetchone()[0], 2)
        fresh = EpisodeEvaluator(Store(self.store.path))
        self.assertEqual(fresh.compare_policies('a', 'tournament:active:a:binary_sensor.hall',
                                                'tournament:shadow:a:binary_sensor.hall')['matched_episodes'], 1)

    def test_existing_tournament_writer_drains_episode_queue_without_new_observation(self):
        service = ContextTournament(self.store, SimpleNamespace())
        queue = DeferredEvidence(self.store, self.write)
        service.flush_tournament_episodes = queue.flush
        queue.submit('event', dict(k='quiet-after-transition', n=1))
        self.store._wal_reclaiming.set()
        with self.store.background_sqlite():
            service._flush_background_context(force=False)
        self.assertEqual(queue.snapshot()['pending'], 1)
        self.store._wal_reclaiming.clear()
        service._flush_background_context(force=True)
        self.assertEqual(queue.snapshot()['pending'], 0)

    def test_drift_native_busy_retains_latest_request_and_other_errors_stay_visible(self):
        engine = SimpleNamespace(lock=threading.RLock(), state_map={'sensor.x': {'state': 'old'}})
        service = AdaptationService(SimpleNamespace(store=self.store, engine=engine))
        cfg = support.agent(id='a')
        def observe(agent, states, now=None):
            self.write(dict(k='drift', n=1))
            return states
        service.observe_live = Mock(side_effect=observe)
        blocker = self.blocker()
        with patch.object(self.store, 'event') as event:
            self.assertFalse(service._observe_background(cfg, 100.))
            event.assert_not_called()
        self.assertEqual(service.observer_snapshot()['busy_retries'], 1)
        self.assertEqual(service.observer_snapshot()['pending'], 1)
        blocker.rollback()
        engine.state_map['sensor.x'] = {'state': 'new'}
        self.assertTrue(service._observe_background(cfg, 101.))
        self.assertEqual(service.observe_live.call_args.args[1]['sensor.x']['state'], 'new')
        service.observe_live.side_effect = ValueError('invalid model')
        with patch.object(self.store, 'event') as event:
            self.assertFalse(service._observe_background(cfg, 102.))
            self.assertEqual(event.call_args.args[2], 'controlled_drift_monitor_gap')


class CandidateRetainedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = candidate_fixture.CandidateShadowExactContextTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def enqueue(self):
        f = self.fixture
        states = f.states('on')
        f.engine.runtime[f.root['id']] = dict(last_prediction=1., last_confidence=.88, last_inference_ts=10.)
        f.manager.before_live_process(f.root, states)
        f.engine.runtime[f.root['id']]['last_inference_ts'] = 20.
        f.engine.policy(f.root).features(states, None, at_ts=20.25)
        f.manager.after_live_process(f.root, states)

    def test_real_writer_lock_keeps_decision_then_flushes_without_predicting_again(self):
        f = self.fixture
        self.enqueue()
        blocker = sqlite3.connect(f.store.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            self.assertEqual(f.manager.drain_deferred_candidate_shadow(max_roots=1), 1)
            self.assertEqual(f.manager.candidate_shadow_async_diagnostics()['errors'], 0)
            self.assertEqual(f.manager.candidate_shadow_persistence_snapshot()['pending'], 1)
            hot = f.manager.candidate_latest_runtime(f.generation_id)
            self.assertEqual((hot['ts'], hot['desired']), (20.25, 1.))
        finally:
            blocker.rollback(); blocker.close()
        with patch.object(f.engine, 'policy', side_effect=AssertionError('prediction replayed')):
            f.manager.flush_candidate_shadow_decisions(force=True)
        with f.store.conn() as c:
            row = c.execute('SELECT ts,desired FROM candidate_generation_decisions WHERE generation_id=?',
                            (f.generation_id,)).fetchone()
        self.assertEqual(tuple(row), (20.25, 1.))
        f.executor.service.assert_not_called()

    def test_already_aligned_event_does_not_issue_timestamp_update(self):
        f = self.fixture
        self.enqueue()
        with f.store.connection_session() as c:
            trace = []
            c.set_trace_callback(trace.append)
            f.manager.drain_deferred_candidate_shadow(max_roots=1)
            c.set_trace_callback(None)
        self.assertFalse(any('UPDATE candidate_generation_decisions' in sql for sql in trace))
        with f.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM candidate_generation_decisions').fetchone()[0], 2)
