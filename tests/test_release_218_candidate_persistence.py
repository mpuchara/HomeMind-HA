"""Candidate inference must finish while an unrelated Store writer is blocked."""
import sqlite3
import json
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

    def queued_return(self):
        self.fixture.observe(0)
        self.f.manager.request_candidate_shadow_persistence = Mock()
        self.fixture.observe(1)
        self.fixture.observe(2)

    def summary(self):
        with self.f.store.conn() as c:
            row = c.execute('SELECT summary_json FROM candidate_generation_comparisons').fetchone()
        return json.loads(row[0]) if row else {}

    def test_return_to_current_publishes_under_held_store_and_pending_quality_blocks_promote(self):
        self.fixture.observe(0)
        self.f.manager.request_candidate_shadow_persistence = Mock()
        self.fixture.observe(1)
        done = threading.Event()
        failures = []
        def run():
            try:self.assertEqual(self.fixture.observe(2), 1)
            except BaseException as exc:failures.append(exc)
            finally:done.set()
        with self.f.store.lock:
            thread = threading.Thread(target=run);thread.start()
            self.assertTrue(done.wait(2), 'False-early accounting still blocks inference on Store')
            self.assertEqual(failures, [])
            self.assertEqual(self.f.manager.candidate_latest_runtime(self.f.generation_id)['desired'], 0.)
            self.assertTrue(self.f.manager.candidate_summary_pending(self.f.root['id']))
        thread.join(2)
        from agent_candidate_atomic_promote import install as install_atomic
        install_atomic(self.f.manager)
        with self.assertRaisesRegex(ValueError, 'awaiting persistence'):
            self.f.manager.promote(self.f.root['id'])
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['candidate_false_early'], 1)
        self.assertFalse(self.f.manager.candidate_summary_pending(self.f.root['id']))
        self.f.executor.service.assert_not_called()

    def test_post_commit_retry_and_intervening_summary_rebuild_do_not_double_count(self):
        import agent_candidate_shadow_runtime as shadow
        self.queued_return()
        self.fixture.observe(3);self.fixture.observe(4)
        original = shadow._persist_summary
        def fail_after_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('false_early_token'):
                raise RuntimeError('after committed quality write')
            return result
        with patch.object(shadow, '_persist_summary', fail_after_commit):
            with self.assertRaisesRegex(RuntimeError, 'after committed'):
                self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['candidate_false_early'], 2)  # One atomic Root summary for both returns.
        edge = shadow._active_comparison_edge(self.f.manager, self.f.root['id'])
        shadow._rebuild_summary(self.f.manager, edge)
        with patch.object(self.f.engine, 'policy', side_effect=AssertionError('prediction replay')):
            self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['candidate_false_early'], 2)
        self.assertEqual(self.summary()['live_false_early'], 2)
        self.assertEqual(self.f.manager.candidate_shadow_persistence_snapshot()['pending'], 0)

    def test_quality_write_failure_rolls_back_cursor_and_counters_before_retry(self):
        self.queued_return()
        queue = self.f.manager.candidate_shadow_decision_writes
        before = queue.snapshot()
        remaining = {key:raw for key,raw in queue.pending.items() if str(key).startswith('false-early:')}
        with self.f.store.conn() as c:
            c.execute("CREATE TRIGGER fail_quality BEFORE UPDATE ON agent_candidates "
                      "WHEN NEW.comparison_json LIKE '%_false_early_cursor%' "
                      "BEGIN SELECT RAISE(ABORT,'quality rollback'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'quality rollback'):
            self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary(), {})
        self.assertEqual(dict(queue.pending), remaining)
        self.assertEqual(queue.snapshot()['bytes'], sum(map(len,remaining.values())))
        self.assertLess(queue.snapshot()['bytes'], before['bytes'])
        with self.f.store.conn() as c:c.execute('DROP TRIGGER fail_quality')
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['candidate_false_early'], 1)

    def test_replaced_generation_cannot_receive_previous_model_false_early_counts(self):
        self.queued_return()
        with self.f.store.conn() as c:
            c.execute('UPDATE agent_candidate_generations SET created_ts=created_ts+1 WHERE generation_id=?',
                      (self.f.generation_id,))
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary().get('candidate_false_early', 0), 0)
        self.assertEqual(self.f.manager.candidate_shadow_persistence_snapshot()['comparison_evidence_invalidations'], 1)
        self.assertFalse(self.f.manager.candidate_summary_pending())

    def test_counter_writer_does_not_build_full_metrics_or_grant_readiness(self):
        self.queued_return()
        with self.f.store.conn() as c:
            c.execute("UPDATE agent_candidates SET state='ready'")
        with patch.object(self.f.manager, '_comparison_summary', side_effect=AssertionError('full metrics on counter writer')):
            self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['candidate_false_early'], 1)
        with self.f.store.conn() as c:
            self.assertEqual(c.execute('SELECT state FROM agent_candidates').fetchone()[0], 'comparing')
        self.f.executor.service.assert_not_called()

    def test_retirement_during_counter_commit_cannot_resurrect_edge_or_generation(self):
        self.queued_return()
        with self.f.store.conn() as c:
            c.execute("CREATE TRIGGER retire_counter BEFORE INSERT ON candidate_generation_comparisons "
                      "WHEN NEW.summary_json LIKE '%_false_early_cursor%' BEGIN "
                      "UPDATE agent_candidates SET state='discarding'; "
                      "UPDATE agent_candidate_generations SET lifecycle_state='discarded' WHERE generation_type='candidate'; END")
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        with self.f.store.conn() as c:
            self.assertEqual(c.execute('SELECT state FROM agent_candidates').fetchone()[0], 'discarding')
            self.assertEqual(c.execute('SELECT lifecycle_state FROM agent_candidate_generations WHERE generation_id=?',
                                      (self.f.generation_id,)).fetchone()[0], 'discarded')
        self.f.executor.service.assert_not_called()

    def test_offline_block_during_counter_commit_stays_blocked(self):
        self.queued_return()
        with self.f.store.conn() as c:
            c.execute("CREATE TRIGGER block_counter BEFORE INSERT ON candidate_generation_comparisons "
                      "WHEN NEW.summary_json LIKE '%_false_early_cursor%' BEGIN "
                      "UPDATE agent_candidates SET state='offline_blocked'; "
                      "UPDATE agent_candidate_generations SET lifecycle_state='offline_blocked' WHERE generation_type='candidate'; END")
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        with self.f.store.conn() as c:
            self.assertEqual(c.execute('SELECT state FROM agent_candidates').fetchone()[0], 'offline_blocked')
            self.assertEqual(c.execute('SELECT lifecycle_state FROM agent_candidate_generations WHERE generation_id=?',
                                      (self.f.generation_id,)).fetchone()[0], 'offline_blocked')

    def test_epoch_changed_after_batch_read_is_checked_again_with_writer_reserved(self):
        import agent_candidate_shadow_runtime as shadow
        self.queued_return()
        original = shadow._persist_summary
        def replace_before_write(*args, **kwargs):
            with self.f.store.conn() as c:
                c.execute('UPDATE agent_candidate_generations SET created_ts=created_ts+1 WHERE generation_id=?',
                          (self.f.generation_id,))
            return original(*args, **kwargs)
        with patch.object(shadow, '_persist_summary', replace_before_write):
            self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary(), {})
        self.assertEqual(self.f.manager.candidate_shadow_persistence_snapshot()['comparison_evidence_invalidations'], 1)

    def test_large_drain_keeps_decision_transactions_at_32_and_retries_committed_groups(self):
        self.fixture.observe(0)
        self.f.manager.request_candidate_shadow_persistence = Mock()
        queue = self.f.manager.candidate_shadow_decision_writes
        queue.batch_items = 256
        for index in range(1,71):self.fixture.observe(index)
        before = queue.snapshot()
        self.assertEqual(before['pending'], 105)
        remaining = {key:raw for key,raw in queue.pending.items()
                     if str(key).startswith('false-early:') or json.loads(raw)['ts']>=26.5}
        with self.f.store.conn() as c:
            c.execute("CREATE TRIGGER fail_third_group BEFORE INSERT ON candidate_generation_decisions "
                      "WHEN NEW.ts>=26.5 BEGIN SELECT RAISE(ABORT,'third group'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'third group'):
            self.f.manager.flush_candidate_shadow_decisions(force=True)
        with self.f.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM candidate_generation_decisions WHERE generation_id=?',
                                      (self.f.generation_id,)).fetchone()[0], 65)
            c.execute('DROP TRIGGER fail_third_group')
        self.assertEqual(dict(queue.pending), remaining)
        self.assertEqual(queue.snapshot()['bytes'], sum(map(len,remaining.values())))
        self.assertEqual(queue.snapshot()['pending'], 41)
        self.f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['candidate_false_early'], 35)
        self.assertEqual(queue.snapshot()['pending'], 0)

    def test_backup_cleanup_with_no_expired_row_never_waits_for_store(self):
        from agent_candidates import AgentCandidateManager
        done = threading.Event()
        with self.f.store.lock:
            thread = threading.Thread(target=lambda: (AgentCandidateManager._maintenance(self.f.manager), done.set()))
            thread.start()
            self.assertTrue(done.wait(2), 'Empty backup cleanup still takes Store')
        thread.join(2)

    def test_backup_cleanup_still_deletes_only_expired_rows(self):
        from agent_candidates import AgentCandidateManager
        with self.f.store.conn() as c:
            c.executemany('INSERT INTO agent_generation_backups '
                '(agent_id,generation,created_ts,expires_ts,model_json,agent_json,comparison_json) '
                "VALUES('a',?,1,?,'{}','{}','{}')", [(1,2.),(2,4.)])
        with patch('agent_candidates.time.time', return_value=3.):
            AgentCandidateManager._maintenance(self.f.manager)
        with self.f.store.conn() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT generation FROM agent_generation_backups')], [2])

    def test_lifecycle_query_excludes_idle_rows_but_keeps_empty_state_queued_semantics(self):
        states = ['queued','building','discarding','','ready','offline_blocked','comparing']
        with self.f.store.conn() as c:
            c.executemany('INSERT INTO agent_candidates(parent_agent_id,candidate_id,generation,state,reason,queued_ts,updated_ts) '
                          "VALUES(?,?,1,?,'test',1,1)",[(str(i),str(i)+'c',state) for i,state in enumerate(states)])
        self.assertEqual({row['state'] for row in self.f.manager._maintenance_rows()},set(states[:4]))
        self.assertGreater(len(self.f.manager._all_rows()),len(self.f.manager._maintenance_rows()))

    def test_custom_lifecycle_row_provider_keeps_its_original_contract(self):
        rows = [dict(state='custom')]
        with patch.object(self.f.manager,'_all_rows',return_value=rows) as provider:
            self.assertIs(self.f.manager._maintenance_rows(),rows)
        provider.assert_called_once_with()

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
        self.assertEqual(before['pending'], 4)  # Three decisions and one return-to-current check.
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
            self.assertEqual(self.f.manager.flush_candidate_shadow_decisions(force=True), before['pending'])
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

    def test_parent_only_bundle_is_not_a_successful_candidate_latency_sample(self):
        tls = self.f.engine._inference_tls = threading.local()
        tls.event_received_perf = {'binary_sensor.presence':time.perf_counter()-.01}
        tls.websocket_received_perf = dict(tls.event_received_perf)
        self.fixture.observe(0,drain=False)
        with patch('agent_candidate_shadow_runtime._predict_candidate',return_value=None), \
             patch('agent_candidate_shadow_runtime.TELEMETRY.observe') as observe:
            self.f.manager.drain_deferred_candidate_shadow()
        names = [args[0] for args,_ in observe.call_args_list]
        self.assertNotIn('candidate_enqueue_to_decision',names)
        self.assertNotIn('candidate_deferred_event_to_decision',names)
        self.assertNotIn('ws_receive_to_candidate_decision',names)
        self.f.executor.service.assert_not_called()


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
