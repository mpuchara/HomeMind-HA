"""Candidate inference must finish while an unrelated Store writer is blocked."""
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import support
import test_release_208_shadow_backpressure as backpressure
from candidate_shadow_persistence import install
from deferred_evidence import DeferredEvidence


class CandidatePersistence218Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = backpressure.ShadowBackpressureTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.f.engine.stop_event = threading.Event()

    def start_writer(self):
        install(self.f.manager)
        def close():
            self.f.engine.stop_event.set()
            self.f.manager.candidate_shadow_persistence_writer.close()
        self.addCleanup(close)

    def test_inference_and_tile_publish_do_not_wait_for_store_and_accepted_rows_stay_charged(self):
        self.fixture.observe(0)
        self.start_writer()
        done = threading.Event()
        errors = []
        def infer():
            try:
                self.assertEqual(self.fixture.observe(1), 1)
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        with self.f.store.lock:
            worker = threading.Thread(target=infer)
            worker.start()
            self.assertTrue(done.wait(2), 'Shadow still waits for the Store writer')
            self.assertEqual(errors, [])
            latest = self.f.manager.candidate_latest_runtime(self.f.generation_id)
            self.assertEqual(latest['desired'], 1.)
            self.assertEqual(latest['ts'], 20.1)
            queue = self.f.manager.candidate_shadow_persistence_snapshot()
            self.assertEqual(queue['pending'], 1)
            self.assertGreater(queue['bytes'], 0)
            self.assertEqual(queue['max_items'], 1024)
            self.assertEqual(queue['max_bytes'], 16 * 1024 * 1024)
            self.f.executor.service.assert_not_called()
        worker.join(2)
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        with self.f.store.conn() as c:
            row = c.execute('SELECT ts,desired FROM candidate_generation_decisions WHERE generation_id=? ORDER BY ts DESC',
                            (self.f.generation_id,)).fetchone()
        self.assertEqual(tuple(row), (20.1, 1.))
        self.assertEqual(self.f.manager.candidate_shadow_persistence_snapshot()['pending'], 0)

    def test_full_async_buffer_does_not_evict_or_wait_and_breaks_evidence_continuity(self):
        self.fixture.observe(0)
        # A manually controlled async owner holds exact evidence without any SQL.
        self.f.manager.request_candidate_shadow_persistence = Mock()
        queue = self.f.manager.candidate_shadow_decision_writes
        queue.max_items = 1
        self.assertEqual(self.fixture.observe(1), 1)
        previous = queue.snapshot()
        with patch('agent_candidate_shadow_runtime._predict_candidate', side_effect=AssertionError('unrecordable inference')):
            self.assertEqual(self.fixture.observe(2), 0)
        current = queue.snapshot()
        self.assertEqual(current['pending'], 1)
        self.assertEqual(current['bytes'], previous['bytes'])
        self.assertEqual(current['rejected'], 0)
        self.assertEqual(self.f.manager.candidate_shadow_async_diagnostics()['observation_gaps'], 1)
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.fixture.observe(3), 1)
        self.assertFalse(self.f.manager.candidate_shadow_persistence_snapshot()['admission']['paused'])
        self.f.executor.service.assert_not_called()

    def test_sql_batch_failure_rolls_back_all_rows_and_retry_never_reruns_policy(self):
        self.f.manager.request_candidate_shadow_persistence = Mock()
        for index in range(3):
            self.assertEqual(self.fixture.observe(index), 1)
        queue = self.f.manager.candidate_shadow_decision_writes
        before = queue.snapshot()
        with self.f.store.conn() as c:
            c.execute("CREATE TRIGGER reject_middle BEFORE INSERT ON candidate_generation_decisions "
                      "WHEN NEW.ts=20.1 BEGIN SELECT RAISE(ABORT,'batch failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.manager.flush_candidate_shadow_decisions(force=True)
        after = queue.snapshot()
        self.assertEqual((after['pending'], after['bytes']), (before['pending'], before['bytes']))
        with self.f.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM candidate_generation_decisions').fetchone()[0], 0)
            c.execute('DROP TRIGGER reject_middle')
        with patch.object(self.f.engine, 'policy', side_effect=AssertionError('replayed evidence')):
            self.assertEqual(self.f.manager.flush_candidate_shadow_decisions(force=True), 3)
        with self.f.store.conn() as c:
            rows = c.execute('SELECT ts,desired FROM candidate_generation_decisions WHERE generation_id=? ORDER BY ts',
                             (self.f.generation_id,)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(20., 0.), (20.1, 1.), (20.2, 0.)])

    def test_shutdown_writer_releases_connection_and_explicit_barrier_drains_after_stop(self):
        self.start_writer()
        self.f.engine.stop_event.set()
        writer = self.f.manager.candidate_shadow_persistence_writer
        writer.close()
        self.assertFalse(writer.thread.is_alive())
        self.assertEqual(self.fixture.observe(1), 1)
        self.assertEqual(self.f.manager.candidate_shadow_persistence_snapshot()['pending'], 1)
        self.assertEqual(self.f.manager.flush_candidate_shadow_decisions(force=True), 1)
        self.assertEqual(self.f.manager.candidate_shadow_persistence_snapshot()['pending'], 0)

    def test_closing_the_writer_does_not_stop_the_engine(self):
        self.start_writer()
        writer = self.f.manager.candidate_shadow_persistence_writer
        writer.close()
        self.assertFalse(writer.thread.is_alive())
        self.assertFalse(self.f.engine.stop_event.is_set())

    def test_background_turns_reuse_one_connection_without_a_transaction_between_turns(self):
        queue = self.f.manager.candidate_shadow_decision_writes
        original = queue.flush
        finished = threading.Event()
        connections = []
        def flush(**kwargs):
            result = original(**kwargs)
            if result:
                connection = self.f.store._connection_session.connection
                connections.append(connection)
                self.assertFalse(connection.in_transaction)
                finished.set()
            return result
        with patch.object(queue, 'flush', side_effect=flush):
            self.start_writer()
            self.fixture.observe(0)
            self.assertTrue(finished.wait(10))
            finished.clear()
            self.fixture.observe(1)
            self.assertTrue(finished.wait(10))
            self.f.manager.candidate_shadow_persistence_writer.close()
        self.assertEqual(len(connections), 2)
        self.assertIs(connections[0], connections[1])

    def test_correct_cannot_substitute_an_old_durable_desired_for_new_pending_observation(self):
        self.fixture.observe(0)
        self.f.manager.request_candidate_shadow_persistence = Mock()
        self.fixture.observe(1)
        self.assertIsNone(self.f.manager.generation_decision_at(self.f.generation_id, 20.15))
        history = self.f.manager.generation_history(self.f.generation_id, 20., 21.)
        self.assertTrue(history['evidence_persistence_pending'])
        self.assertEqual(history['pending_evidence_count'], 1)
        self.assertTrue(self.f.manager.generation_decision_read(self.f.generation_id,20.15)['pending'])
        self.assertEqual(self.f.manager.generation_decision_at(self.f.generation_id, 20.)['desired'], 0.)
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.f.manager.generation_decision_at(self.f.generation_id, 20.15)['desired'], 1.)

    def test_live_correct_does_not_fall_back_to_old_history_after_a_pending_generation_read(self):
        import agent_workflow_actions as workflow
        manager = SimpleNamespace(generation_decision_read=Mock(return_value={'decision':None,'pending':True}))
        generation = dict(generation_id='g',generation_type='live',agent_id='a')
        with patch.object(workflow,'_observed_live_decision',side_effect=AssertionError('stale history fallback')):
            self.assertIsNone(workflow._observed_decision(manager,generation,20.))

    def test_event_clocks_follow_the_captured_parent_frame_and_are_not_replaced_by_new_receipts(self):
        tls = self.f.engine._inference_tls = threading.local()
        stamp = time.perf_counter() - .01
        tls.event_received_perf = {'binary_sensor.presence':stamp}
        tls.websocket_received_perf = {'binary_sensor.presence':stamp-.001}
        self.fixture.observe(0,drain=False)
        tls.event_received_perf['binary_sensor.presence'] = time.perf_counter()
        tls.websocket_received_perf.clear()
        job = next(iter(self.f.manager.candidate_shadow_async.pending.values()))
        self.assertEqual(job['event_receipts'],(stamp,))
        self.assertEqual(job['websocket_receipts'],(stamp-.001,))
        with patch('agent_candidate_shadow_runtime.TELEMETRY.observe') as observe:
            self.f.manager.drain_deferred_candidate_shadow()
        measured = {args[0]:args[1] for args,_ in observe.call_args_list}
        self.assertGreaterEqual(measured['candidate_deferred_event_to_decision'],10.)
        self.assertGreater(measured['ws_receive_to_candidate_decision'],measured['candidate_deferred_event_to_decision'])

    def test_empty_timer_receipts_do_not_borrow_an_engine_or_previous_event_clock(self):
        tls = self.f.engine._inference_tls = threading.local()
        tls.event_received_perf = {}
        tls.websocket_received_perf = {}
        self.f.engine.last_event_received = time.perf_counter() - 30.
        self.fixture.observe(0,drain=False)
        with patch('agent_candidate_shadow_runtime.TELEMETRY.observe') as observe:
            self.f.manager.drain_deferred_candidate_shadow()
        names = [args[0] for args,_ in observe.call_args_list]
        self.assertIn('candidate_enqueue_to_decision',names)
        self.assertNotIn('candidate_deferred_event_to_decision',names)
        self.assertNotIn('ws_receive_to_candidate_decision',names)


class EvidenceBatch218Tests(unittest.TestCase):
    def test_peer_context_is_taken_at_admission_after_the_previous_root_finishes(self):
        import test_candidate_shadow_deferred as fixture
        f = fixture.CandidateShadowDeferredTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        original = f.manager.execute_candidate_shadow_job
        def execute(job):
            if job['root_agent_id'] == 'first':
                f.queue.enqueue(f.job(root='peer', revision=2, context_ts=22.))
            return original(job)
        f.manager.execute_candidate_shadow_job = execute
        f.queue.enqueue(f.job(root='first', revision=1))
        f.queue.enqueue(f.job(root='peer', revision=1, context_ts=11.))
        self.assertEqual(f.queue.drain(max_roots=2), 2)
        self.assertEqual([r['root'] for r in f.executed], ['first', 'peer'])
        self.assertEqual(f.executed[-1]['state_revision'], 2)
        self.assertEqual(f.executed[-1]['job']['context_ts'], 22.)
        self.assertEqual(f.queue.diagnostics()['queue_depth'], 0)

    def test_bounded_transactions_keep_every_key_and_fail_only_the_uncommitted_suffix(self):
        calls = []
        def write(rows):
            calls.append([r['value'] for r in rows])
            if len(calls) == 2:
                raise sqlite3.OperationalError('deliberate failure')
        queue = DeferredEvidence(object(), Mock(), batch_writer=write)
        for value in range(70):
            queue.submit(str(value), {'value': value})
        with self.assertRaises(sqlite3.OperationalError):
            queue.flush(force=True)
        self.assertEqual(queue.snapshot()['pending'], 38)
        self.assertEqual(queue.snapshot()['flushed'], 32)
        self.assertEqual(queue.flush(force=True), 38)
        self.assertEqual(calls, [list(range(32)), list(range(32,64)), list(range(32,64)), list(range(64,70))])
        self.assertEqual(queue.snapshot()['bytes'], 0)


if __name__ == '__main__':
    unittest.main()
