import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import context_tournament_policy_candidate as pool
from storage import Store
from workflow_request_queue import STATE_DONE, STATE_PROCESSING, WorkflowRequestQueue


class PoolPayload161Tests(unittest.TestCase):
    def row(self):
        return dict(history=[[float(i), i / 7.] for i in range(32)],
                    samples=[dict(episode=str(i), label=float(i % 2), value=i / 7.,
                                  lag_1=i / 8., interactions={'primary': i / 9.})
                             for i in range(96)], screening={})

    def test_repeated_sensor_edges_keep_examples_and_screening_bytes(self):
        row = self.row()
        old = pool._pool_json_payload(row, samples_changed=True)
        with patch.object(pool, 'semantic_predictive_score', wraps=pool.semantic_predictive_score) as score:
            for i in range(12):
                row['history'] = (row['history'] + [[32.+i, .5+i]])[-32:]
                with patch.object(pool.json, 'dumps', wraps=json.dumps) as encode:
                    new = pool._pool_json_payload(row, old, samples_changed=False)
                self.assertEqual(encode.call_count, 1)
                self.assertIs(new[1], old[1])
                self.assertIs(new[2], old[2])
                self.assertEqual(json.loads(new[0]), row['history'])
                old = new
            score.assert_not_called()

    def test_short_independent_event_regenerates_examples_and_screening(self):
        row = self.row()
        old = pool._pool_json_payload(row, samples_changed=True)
        # Brief handwashing is still an independent label; no duration filter.
        row['samples'] = (row['samples'] + [dict(episode='brief-on', label=1., value=.8)])[-96:]
        with patch.object(pool.json, 'dumps', wraps=json.dumps) as encode:
            new = pool._pool_json_payload(row, old, samples_changed=True)
        self.assertEqual(encode.call_count, 3)
        self.assertEqual(json.loads(new[1]), row['samples'])
        self.assertEqual(json.loads(new[2]), pool.semantic_predictive_score(row['samples']))
        self.assertNotEqual(new[1], old[1])
        # Older deferred snapshots remain immutable.
        self.assertNotIn('brief-on', old[1])

    def test_restart_without_ram_payload_encodes_complete_retained_row(self):
        row = self.row()
        expected = pool._pool_json_payload(row, samples_changed=True)
        with patch.object(pool, 'semantic_predictive_score', wraps=pool.semantic_predictive_score) as score:
            actual = pool._pool_json_payload(copy.deepcopy(row), samples_changed=False)
        self.assertEqual(actual, expected)
        score.assert_not_called()

    def test_missing_screening_is_recomputed_without_reencoding_examples(self):
        row = self.row()
        old = pool._pool_json_payload(row, samples_changed=True)
        row['screening'] = {}
        new = pool._pool_json_payload(row, old, samples_changed=False)
        self.assertIs(new[1], old[1])
        self.assertEqual(json.loads(new[2]), pool.semantic_predictive_score(row['samples']))


class PoolDurability161Tests(unittest.TestCase):
    def test_observer_matches_previous_rows_in_sync_and_coalesced_background_modes(self):
        from test_context_tournament_observed_pool_runtime import ObservedPoolRuntimeTests
        from context_row_buffer import ContextRowBuffer
        import context_tournament_promotion as promotion
        old_chooser, old_migrate = promotion._choose_schema_after_promotion, promotion._migrate_schema
        current = pool._pool_json_payload
        def previous(row, cached=None, *, samples_changed):
            if samples_changed or not row.get('screening'):
                row['screening'] = pool.semantic_predictive_score(row['samples'])
            return tuple(json.dumps(row[name], separators=(',', ':'), sort_keys=True)
                         for name in ('history', 'samples', 'screening'))
        def exercise(encoder, buffered):
            with tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / 'pool.db')
                a = support.agent()
                entities = {f'sensor.context_{i}' for i in range(6)}
                states = {eid: support.state(eid, .1) for eid in entities}
                states[a['target_entity']] = support.state(a['target_entity'], 'off')
                engine = SimpleNamespace(models={}, state_map=states, entity_registry={},
                                         runtime={a['id']: {}}, context_relevance={},
                                         state_revision=0, lock=threading.RLock())
                service = ObservedPoolRuntimeTests.FakeService(store, engine, dict(
                    active_features=[], challenger_features=[], feature_scores={}, schema_revision=1))
                service._eligible_entities = lambda *args: entities
                buffers = []
                if buffered:
                    def register(name, statement):
                        result = ContextRowBuffer(store, name, statement)
                        buffers.append(result)
                        return result
                    service.register_context_rows = register
                    service.background_persistence_available = lambda: True
                with patch.object(pool, '_pool_json_payload', encoder):
                    pool.install_policy_candidates(service)
                    for i in range(40):
                        eid = f'sensor.context_{i % 6}'
                        states[eid] = support.state(eid, i / 7.)
                        engine.state_revision = i
                        engine.runtime[a['id']]['last_change_origin'] = 'own_command' if i == 11 else 'external'
                        if i in (10, 20, 30):
                            states[a['target_entity']] = support.state(a['target_entity'], 'on')
                        if i in (11, 21, 31):
                            states[a['target_entity']] = support.state(a['target_entity'], 'off')
                        if i == 25:
                            states[eid] = support.state(eid, 'unavailable')
                        with patch.object(pool.time, 'time', return_value=1790000000.+i):
                            service.observe_shadow(a, states, {eid, a['target_entity']})
                    for buffer in buffers:
                        buffer.flush()
                with store.conn() as c:
                    return [dict(row) for row in c.execute(
                        'SELECT * FROM context_tournament_observed_pool ORDER BY entity_id')]
        try:
            for buffered in (False, True):
                with self.subTest(buffered=buffered):
                    old = exercise(previous, buffered)
                    new = exercise(current, buffered)
                    self.assertEqual(new, old)
                    self.assertEqual(len(new), 6)
                    for row in new:
                        self.assertEqual(row['opportunities'], 40)
                        self.assertEqual(len(json.loads(row['samples_json'])), 5)
        finally:
            promotion._choose_schema_after_promotion, promotion._migrate_schema = old_chooser, old_migrate


class WorkflowWake161Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'queue.db')
        self.commit = Mock(return_value={'ok': True, 'child_generation_id': 'candidate:1'})
        self.queue = WorkflowRequestQueue(SimpleNamespace(store=self.store, workflow_correct_commit=self.commit),
                                          start_worker=False)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.queue.stop()
        if self.queue.ident is not None:
            self.queue.join(3)
        self.temp.cleanup()

    def finish_then_stop(self):
        finish = self.queue._finish
        def wrapped(*args, **kwargs):
            finish(*args, **kwargs)
            self.queue.stop()
        self.queue._finish = wrapped

    def assert_finished(self, rid):
        self.queue.join(3)
        self.assertFalse(self.queue.is_alive(), 'worker waited for its 30-second fallback')
        self.assertEqual(self.queue.status(rid)['state'], STATE_DONE)
        self.commit.assert_called_once()

    def test_idle_worker_checks_once_then_uses_long_fallback(self):
        waits = []
        def wait(timeout):
            waits.append(timeout)
            self.queue.stop_event.set()
        with patch.object(self.queue.wake_event, 'wait', side_effect=wait), patch.object(
            self.queue, 'process_once', wraps=self.queue.process_once
        ) as claim:
            self.queue.run()
        self.assertEqual(claim.call_count, 1)
        self.assertEqual(waits, [30.0])

    def test_admission_after_empty_claim_before_wait_wakes_immediately(self):
        claim = self.queue._claim_next
        admitted = False
        def wrapped():
            nonlocal admitted
            result = claim()
            if result is None and not admitted:
                admitted = True
                self.queue.enqueue_correct('root:a', 'between-claim-wait')
            return result
        self.queue._claim_next = wrapped
        self.finish_then_stop()
        self.queue.start()
        self.assert_finished('between-claim-wait')

    def test_admission_while_worker_is_waiting_wakes_immediately(self):
        waiting = threading.Event()
        original_wait = self.queue.wake_event.wait
        def wait(timeout):
            waiting.set()
            return original_wait(timeout)
        self.queue.wake_event.wait = wait
        self.finish_then_stop()
        self.queue.start()
        self.assertTrue(waiting.wait(3))
        self.queue.enqueue_correct('root:a', 'while-idle')
        self.assert_finished('while-idle')

    def test_restart_replays_processing_request_without_admission_wake(self):
        self.queue.enqueue_correct('root:a', 'recovered')
        with self.store.conn() as c:
            c.execute('UPDATE agent_workflow_requests SET state=?', (STATE_PROCESSING,))
        self.queue = WorkflowRequestQueue(self.queue.manager, start_worker=False)
        self.assertFalse(self.queue.wake_event.is_set())
        self.finish_then_stop()
        self.queue.start()
        self.assert_finished('recovered')

    def test_stop_during_empty_claim_does_not_enter_long_wait(self):
        claim = self.queue._claim_next
        def wrapped():
            result = claim()
            self.queue.stop()
            return result
        self.queue._claim_next = wrapped
        self.queue.start()
        self.queue.join(3)
        self.assertFalse(self.queue.is_alive())


if __name__ == '__main__':
    unittest.main()
