"""No peer replay, no lost busy-target triggers, correct REST/WS latency provenance."""
from concurrent.futures import Future
from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import support
import engine as engine_module
from engine import Engine
from storage import Store
with patch.dict(os.environ):
    from tools.benchmark_target_retries import fixture, drain, run_mode, ControlledWorkers


SHARED = 'binary_sensor.shared'
A, B = 'light.target_0', 'light.target_1'


class TargetRetries172Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='target-retries-172-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.patch = patch.object(engine_module, 'STORE', self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.r = fixture(self.store)

    def event(self, value='on'):
        self.r.state_revision += 1
        self.r.entity_revisions[SHARED] = self.r.state_revision
        self.r.state_map[SHARED] = {'state': value}
        self.r.entity_event_received_perf[SHARED] = time.perf_counter()
        self.r.process(self.r.state_map, {SHARED})

    def real_engine(self):
        r = Engine()
        for name in ('control_workers', 'poll_worker', 'registry_worker', 'housekeeping_worker'):
            self.addCleanup(getattr(r, name).shutdown, wait=False, cancel_futures=True)
        return r

    def test_retry_of_one_busy_target_never_replays_event_to_peer(self):
        first = Future()
        self.r.in_flight[A] = first
        self.event()
        self.assertEqual(self.r.control_workers.tasks[0][2][0][0]['target_entity'], B)
        first.set_result(None)
        self.assertEqual(self.r.ready_target_changes, {A: {SHARED}})
        self.assertEqual(self.r.dirty_entities, set())
        drain(self.r)
        self.assertEqual([t[2][0][0]['target_entity'] for t in self.r.control_workers.tasks], [B, A])
        self.assertEqual(self.r.pending_target_changes, {})

    def test_two_busy_targets_deliver_two_real_events_then_settle(self):
        self.r.process(self.r.state_map, {SHARED})
        self.event()
        for _ in range(4):
            self.assertTrue(self.r.control_workers.complete_next())
            drain(self.r)
        self.assertFalse(self.r.control_workers.complete_next())
        self.assertEqual(self.r.outputs, [('0', 'off'), ('1', 'off'), ('0', 'on'), ('1', 'on')])
        self.assertEqual(self.r.ready_target_changes, {})
        self.assertEqual(self.r.pending_target_changes, {})

    def test_new_event_and_ready_retry_coalesce_once_with_latest_snapshot(self):
        self.r.ready_target_changes[A] = {SHARED}
        self.event('newest')
        self.assertEqual(len(self.r.control_workers.tasks), 2)
        for task in self.r.control_workers.tasks:
            args = task[2]
            self.assertEqual(args[1], {SHARED})
            self.assertEqual(args[2][0][SHARED]['state'], 'newest')
            self.assertEqual(args[2][1], 1)
            self.assertEqual(args[3], {SHARED: self.r.entity_event_received_perf[SHARED]})

    def test_ready_trigger_is_not_added_to_other_targets_fresh_event(self):
        other = 'sensor.other'
        self.r.dependency_agents[other] = {'1'}
        self.r.entity_event_received_perf[other] = time.perf_counter()
        self.r.ready_target_changes[A] = {SHARED}
        self.r.process(self.r.state_map, {other})
        changes = {t[2][0][0]['target_entity']: (t[2][1], t[2][3]) for t in self.r.control_workers.tasks}
        self.assertEqual(changes[A][0], {SHARED})
        self.assertEqual(changes[B][0], {other})
        self.assertEqual(set(changes[B][1]), {other})

    def test_retry_revalidates_schema_without_global_fanout(self):
        self.r.ready_target_changes[A] = {SHARED}
        self.r.dependency_agents[SHARED] = {'1'}
        drain(self.r)
        self.assertEqual(self.r.control_workers.tasks, [])
        self.assertEqual(self.r.ready_target_changes, {})

    def test_disabled_or_deleted_retry_owner_is_not_dispatched(self):
        self.r.ready_target_changes[A] = {SHARED}
        self.r.active_agents_by_target.pop(A)
        self.r.agent_configs.pop('0')
        drain(self.r)
        self.assertEqual(self.r.control_workers.tasks, [])

    def test_routing_failure_retains_target_retry_for_next_pass(self):
        self.r.ready_target_changes[A] = {SHARED}
        self.r._refresh_agent_index = Mock(side_effect=RuntimeError('database temporarily busy'))
        with self.assertRaises(RuntimeError):
            self.r.process(self.r.state_map, set())
        self.assertEqual(self.r.ready_target_changes, {A: {SHARED}})
        self.assertTrue(self.r.wake_event.is_set())
        self.r._refresh_agent_index = Mock()
        drain(self.r)
        self.assertEqual(len(self.r.control_workers.tasks), 1)

    def test_submission_failure_keeps_only_undispatched_retry_owners(self):
        self.r.ready_target_changes = {A: {SHARED}, B: {SHARED}}
        submit = self.r.control_workers.submit
        calls = []
        def dispatch(fn, *args):
            calls.append(args[0][0]['target_entity'])
            if len(calls) == 2:
                raise RuntimeError('temporary submit failure')
            return submit(fn, *args)
        with patch.object(self.r.control_workers, 'submit', side_effect=dispatch):
            with self.assertRaisesRegex(RuntimeError, 'temporary submit'):
                self.r.process(self.r.state_map, set())
        self.assertEqual(self.r.ready_target_changes, {calls[1]: {SHARED}})
        self.assertNotIn(calls[0], self.r.ready_target_changes)
        self.assertTrue(self.r.wake_event.is_set())

    def test_timer_does_not_consume_retry_or_reuse_event_timestamp(self):
        self.r.ready_target_changes[A] = {SHARED}
        self.r.entity_event_received_perf[B] = 1.
        self.r.process(self.r.state_map, {B}, event_driven=False)
        self.assertEqual(self.r.ready_target_changes, {A: {SHARED}})
        self.assertEqual(self.r.control_workers.tasks[0][2][3], {})
        drain(self.r)
        self.assertEqual(len(self.r.control_workers.tasks), 2)

    def test_event_batch_coalesces_to_latest_revision_without_losing_owner(self):
        self.r.process(self.r.state_map, {SHARED})
        for i in range(30):
            self.event(str(i))
        self.assertEqual(self.r.pending_target_changes, {A: {SHARED}, B: {SHARED}})
        for future, fn, args in self.r.control_workers.tasks[:2]:
            self.assertEqual(len(future._done_callbacks), 1)
        self.r.control_workers.complete_next()
        drain(self.r)
        args = self.r.control_workers.tasks[-1][2]
        self.assertEqual(args[2][1], 30)
        self.assertEqual(args[2][0][SHARED]['state'], '29')
        self.assertEqual(args[3][SHARED], self.r.entity_event_received_perf[SHARED])

    def test_future_completing_while_callback_is_added_keeps_real_trigger(self):
        class RacingFuture(Future):
            def add_done_callback(f, callback):
                f.set_result(None)
                super().add_done_callback(callback)
        self.r.in_flight[A] = RacingFuture()
        self.event()
        self.assertEqual(self.r.ready_target_changes, {A: {SHARED}})
        drain(self.r)
        self.assertEqual([t[2][0][0]['target_entity'] for t in self.r.control_workers.tasks], [B, A])

    def test_cancelled_worker_retries_pending_event_only_for_its_target(self):
        old = Future()
        self.r.in_flight[A] = old
        self.event()
        self.assertTrue(old.cancel())
        self.assertEqual(self.r.ready_target_changes, {A: {SHARED}})
        drain(self.r)
        self.assertEqual(self.r.pending_target_changes, {})

    def test_ready_retry_becomes_pending_again_when_owner_is_busy(self):
        busy = Future()
        self.r.in_flight[A] = busy
        self.r.ready_target_changes[A] = {SHARED}
        drain(self.r)
        self.assertEqual(self.r.pending_target_changes, {A: {SHARED}})
        self.assertEqual(self.r.control_workers.tasks, [])
        busy.set_result(None)
        drain(self.r)
        self.assertEqual(len(self.r.control_workers.tasks), 1)

    def test_engine_loop_keeps_retry_during_gate_then_dispatches_without_new_event(self):
        r = self.real_engine()
        r.state_map = dict(self.r.state_map)
        r.agent_configs = self.r.agent_configs
        r.active_agents_by_target = self.r.active_agents_by_target
        r.dependency_agents = self.r.dependency_agents
        r._active_agents_for_changes = self.r._active_agents_for_changes
        r._refresh_agent_index = Mock()
        r.ready_target_changes[A] = {SHARED}
        r.initial_inference_pending = False
        r.startup_inference_not_before = 0.
        r.inference_enabled.clear()
        r.refresh_states = Mock()
        r._startup_warmup_step = Mock(return_value=0)
        r._due_inference_targets = Mock(return_value=set())
        r._schedule_housekeeping = Mock()
        r._maybe_schedule_state_resync = Mock()
        waits = []
        def wait(_timeout):
            waits.append(True)
            if len(waits) == 2:
                self.assertEqual(r.ready_target_changes, {A: {SHARED}})
                r.inference_enabled.set()
                return False
            return True
        r.wake_event.wait = wait
        original_submit = self.r.control_workers.submit
        def submit(fn, *args):
            future = original_submit(fn, *args)
            r.stop_event.set()
            return future
        r.control_workers = type('StopPool', (), {'submit': staticmethod(submit)})()
        with patch.dict(engine_module.OPTIONS, {'realtime_inference_debounce_ms': 0}):
            r.run()
        self.assertEqual(len(waits), 2)
        self.assertEqual(self.r.control_workers.tasks[0][2][0][0]['target_entity'], A)
        self.assertEqual(r.ready_target_changes, {})
        self.assertEqual(r.dirty_entities, set())

    def rest_engine(self):
        r = self.real_engine()
        r.state_map = {SHARED: support.state(SHARED, 'off', last_updated='2026-10-10T20:00:00Z')}
        # support.state treats kwargs as attributes, so set the HA envelope explicitly.
        r.state_map[SHARED]['last_updated'] = '2026-10-10T20:00:00Z'
        r.state_map[SHARED]['attributes'] = {}
        r._queue_archive_state = Mock()
        r.flush_archive = Mock()
        r.entity_event_received_perf[SHARED] = time.perf_counter() - 150.
        return r

    def test_rest_change_invalidates_old_ws_timestamp_but_keeps_reaction(self):
        r = self.rest_engine()
        new = dict(r.state_map[SHARED], state='on', last_updated='2026-10-10T20:00:05Z')
        with patch.object(engine_module.HA, 'states', return_value=[new]):
            r.refresh_states()
        self.assertEqual(r.state_map[SHARED]['state'], 'on')
        self.assertIn(SHARED, r.dirty_entities)
        self.assertNotIn(SHARED, r.entity_event_received_perf)
        self.assertTrue(r.wake_event.is_set())

    def test_rest_does_not_clear_newer_ws_event_arriving_during_request(self):
        r = self.rest_engine()
        live = dict(r.state_map[SHARED], state='on', last_updated='2026-10-10T20:00:20Z')
        seen = []
        def response():
            r.on_state_changed({'entity_id': SHARED, 'new_state': live})
            seen.append(r.entity_event_received_perf[SHARED])
            return [dict(live, state='off', last_updated='2026-10-10T20:00:05Z')]
        with patch.object(engine_module.HA, 'states', side_effect=response):
            r.refresh_states()
        self.assertEqual(r.state_map[SHARED], live)
        self.assertEqual(r.entity_event_received_perf[SHARED], seen[0])

    def test_paired_four_target_regression_terminates_with_same_real_results(self):
        previous = run_mode('previous', completions=32, targets=4)
        current = run_mode('current', completions=32, targets=4)
        self.assertEqual(previous['final_desired'], current['final_desired'])
        self.assertEqual(current['decisions'], 8)
        self.assertEqual(current['unresolved'], 0)
        self.assertEqual(previous['decisions'], 32)
        self.assertEqual(previous['unresolved'], 4)
        self.assertTrue(current['every_real_event_delivered'])


if __name__ == '__main__':
    unittest.main()
