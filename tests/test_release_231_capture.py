"""Single authoritative scheduler copy and bounded detached-map accounting."""
from types import SimpleNamespace
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import support
import engine as engine_module
from detached_mapping_snapshots import DetachedMappingSnapshots
from tools.benchmark_target_retries import fixture


class IngressSnapshotTests(unittest.TestCase):
    def run_once(self, *, gate=True, event=True, timer=False):
        runtime = fixture(SimpleNamespace())
        class CountedStates(dict):
            copies = 0
            def __iter__(self):
                return super().__iter__()
            def keys(self):
                self.copies += 1
                return super().keys()
        runtime.state_map = CountedStates(runtime.state_map)
        runtime.refresh_states = Mock()
        runtime.context.home.expire = Mock()
        runtime.inference_enabled = threading.Event()
        if gate:
            runtime.inference_enabled.set()
        runtime.startup_inference_not_before = 0.
        runtime.initial_inference_pending = False
        runtime.inference_scheduler = dict(event_passes=0, timer_passes=0, idle_skips=0)
        runtime._startup_warmup_step = Mock(return_value=0)
        runtime._due_inference_targets = Mock(return_value={'light.target_0'} if timer else set())
        runtime._schedule_housekeeping = runtime.stop_event.set
        runtime._maybe_schedule_state_resync = Mock()
        runtime.dirty_entities = {'binary_sensor.shared'}
        runtime.wake_event = Mock(wait=Mock(return_value=event))
        with patch.object(engine_module.STORE, 'event'), \
             patch.object(engine_module.TELEMETRY, 'start_trend_sampling'):
            runtime.run()
        self.assertFalse(getattr(runtime, 'error', None))
        return runtime

    def test_event_pass_copies_states_once_and_shares_atomic_revisions(self):
        runtime = self.run_once()
        self.assertEqual(runtime.state_map.copies, 1)
        frames = [task[2][2] for task in runtime.control_workers.tasks]
        self.assertEqual(len(frames), 2)
        self.assertIs(frames[0], frames[1])
        runtime.state_map['binary_sensor.shared'] = {'state': 'on'}
        runtime.entity_revisions['binary_sensor.shared'] = 99
        self.assertEqual(frames[0][0]['binary_sensor.shared']['state'], 'off')
        self.assertEqual(frames[0][2]['binary_sensor.shared'], 0)
        self.assertFalse(runtime.dirty_entities)

    def test_closed_gate_retains_events_without_copying_house(self):
        runtime = self.run_once(gate=False)
        self.assertEqual(runtime.state_map.copies, 0)
        self.assertEqual(runtime.dirty_entities, {'binary_sensor.shared'})
        self.assertFalse(runtime.control_workers.tasks)

    def test_timer_copies_once_and_never_borrows_event_clock(self):
        runtime = self.run_once(event=False, timer=True)
        self.assertEqual(runtime.state_map.copies, 1)
        task = runtime.control_workers.tasks[0][2]
        self.assertEqual(task[3], {})
        self.assertEqual(task[1], {'light.target_0'})
        self.assertEqual(runtime.dirty_entities, {'binary_sensor.shared'})


class CaptureAccountingTests(unittest.TestCase):
    def test_complete_accounting_covers_actual_containers_and_changed_nested_data(self):
        cache = DetachedMappingSnapshots(reuse_complete=True)
        mapping = {str(i): {'attributes': {'labels': [str(i)]}} for i in range(64)}
        first, _, parts = cache.capture(mapping, shared=True)
        actual = sum(map(sys.getsizeof, (cache.complete, cache.complete_keys,
            cache.complete_ids, cache.complete_parts)))
        actual += sum(map(sys.getsizeof, cache.complete_ids))
        actual += sum(map(sys.getsizeof, cache.complete_parts))
        self.assertGreaterEqual(cache.complete_bytes, actual)
        mapping['2']['attributes']['labels'].append('late')
        second, _, _ = cache.capture(mapping, shared=True)
        self.assertEqual(first['2']['attributes']['labels'], ['2'])
        self.assertEqual(second['2']['attributes']['labels'], ['2', 'late'])
        self.assertIs(first['1'], second['1'])
        self.assertLessEqual(cache.bytes, cache.max_bytes)

    def test_eviction_or_oversized_record_cannot_be_retained_by_complete_map(self):
        for budget, count in ((100000, 2), (600, 64), (0, 0)):
            with self.subTest(budget=budget, count=count):
                cache = DetachedMappingSnapshots(max_bytes=budget, max_entries=count,
                                                 reuse_complete=True)
                source = {str(i): {'value': i} for i in range(8)}
                frozen, _ = cache.capture(source)
                self.assertEqual(frozen, source)
                self.assertIsNone(cache.complete)
                self.assertLessEqual(cache.bytes, budget)
                self.assertLessEqual(len(cache.entries), count)
                source['large'] = {'value': 'x'*100000}
                self.assertEqual(cache.capture(source)[0], source)
                self.assertIsNone(cache.complete)
                self.assertLessEqual(cache.bytes, budget)
