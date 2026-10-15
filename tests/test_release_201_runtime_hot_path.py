"""Promotion SQL admission, fresh audit phases, warm candidates and scheduling."""
from contextlib import ExitStack, closing
from collections.abc import Mapping
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import support
import context_tournament_promotion as promotion
from context_schema_history import install_schema_history
from runtime_threading import configure_runtime_threads, runtime_threading_snapshot
from storage import Store
from telemetry import RuntimeDebugTrace
from test_context_schema_history import HistoryOnlyService
import test_context_tournament_promotion as promotion_fixture


class PromotionAdmission201Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = promotion_fixture.PromotionIntegrationTests('test_ready_challenger_is_promoted_without_exceeding_fast_limit')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_insufficient_evidence_skips_cooldown_sql_without_changing_policy(self):
        f = self.fixture
        f.service._model.update(samples=0, class_totals=[0, 0], active_correct_by_class=[0, 0],
                                shadow_correct_by_class=[0, 0], promotion_consecutive_wins=0)
        with patch.object(promotion, '_promotion_state', wraps=promotion._promotion_state) as read:
            f.service.observe_shadow(f.agent, {}, set())
        self.assertEqual(read.call_count, 0)
        self.assertNotIn(f.challenger, f.policy.schema.entities)
        self.assertIsNone(f.store.get_model(f.agent['id']))

    def test_otherwise_ready_candidate_reads_fresh_durable_cooldown(self):
        f = self.fixture
        import time
        promotion._save_promotion_state(f.service, f.agent['id'], 'sensor.previous', None, 1, {}, time.time())
        with patch.object(promotion, '_promotion_state', wraps=promotion._promotion_state) as read:
            f.service.observe_shadow(f.agent, {}, set())
        self.assertEqual(read.call_count, 1)
        self.assertNotIn(f.challenger, f.policy.schema.entities)
        self.assertIsNone(f.store.get_model(f.agent['id']))

    def test_qualified_candidate_still_promotes_after_fresh_cooldown_read(self):
        f = self.fixture
        with patch.object(promotion, '_promotion_state', wraps=promotion._promotion_state) as read:
            f.service.observe_shadow(f.agent, {}, set())
        self.assertEqual(read.call_count, 1)
        self.assertIn(f.challenger, f.policy.schema.entities)
        self.assertIsNotNone(f.store.get_model(f.agent['id']))


class HistoryPhases201Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='audit-phases-201-')
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'history.db')
        self.service = HistoryOnlyService(self.store)
        install_schema_history(self.service)

    def record(self):
        return self.service.record_schema_history(agent_id='a', old_schema=['sensor.old'],
                    new_schema=['sensor.new'], reason='sensor_tournament_promotion', created_ts=100)

    def read(self, phase):
        return self.service.schema_history_observation('a', phase)

    def test_two_consumers_share_two_independently_fresh_phase_reads(self):
        with self.service.schema_history_frame():
            self.assertEqual(self.read('before'), [])
            self.assertEqual(self.read('before'), [])
            self.assertEqual(self.read('after'), [])
            self.assertEqual(self.read('after'), [])
        snapshot = self.service.schema_history_cache_snapshot()
        self.assertEqual((snapshot['reads'], snapshot['hits']), (2, 2))

    def test_external_change_between_phases_is_seen_and_new_frame_refreshes(self):
        history_id = self.record()
        with self.service.schema_history_frame():
            self.assertEqual(self.read('before')[0]['status'], 'promoted')
            with closing(sqlite3.connect(self.store.path)) as external, external:
                external.execute("UPDATE context_schema_history SET status='accepted' WHERE id=?", (history_id,))
            self.assertEqual(self.read('after')[0]['status'], 'accepted')
        with self.service.schema_history_frame():
            self.assertEqual(self.read('before')[0]['status'], 'accepted')

    def test_committed_insert_and_status_change_invalidate_phase_rows(self):
        with self.service.schema_history_frame():
            self.assertEqual(self.read('after'), [])
            history_id = self.record()
            self.assertEqual(self.read('after')[0]['id'], history_id)
            self.service.set_schema_history_status(history_id, 'rolled_back')
            self.assertEqual(self.read('after')[0]['status'], 'rolled_back')

    def test_returned_nested_rows_do_not_mutate_cached_audit(self):
        self.record()
        with self.service.schema_history_frame():
            self.read('before')[0]['new_schema'].append('sensor.corrupt')
            self.assertEqual(self.read('before')[0]['new_schema'], ['sensor.new'])

    def test_exception_discards_frame_and_outside_reads_remain_fresh(self):
        history_id = self.record()
        with self.assertRaises(ValueError), self.service.schema_history_frame():
            self.read('before')
            raise ValueError('observation failed')
        with closing(sqlite3.connect(self.store.path)) as external, external:
            external.execute("UPDATE context_schema_history SET status='accepted' WHERE id=?", (history_id,))
        self.assertEqual(self.read('before')[0]['status'], 'accepted')
        with self.service.schema_history_frame():
            self.assertEqual(self.read('before')[0]['status'], 'accepted')
        self.assertEqual(self.service.schema_history_cache_snapshot()['hits'], 0)

    def test_committed_change_on_another_thread_invalidates_active_frame(self):
        errors = []
        def write():
            try:
                self.record()
            except BaseException as exc:
                errors.append(exc)
        with self.service.schema_history_frame():
            self.assertEqual(self.read('before'), [])
            worker = threading.Thread(target=write)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(self.read('before')), 1)

    def test_snapshot_is_ram_only(self):
        with patch.object(self.store, 'conn', side_effect=AssertionError('debug export ran SQL')):
            self.assertEqual(self.service.schema_history_cache_snapshot()['persistent_entries'], 0)


class ThreadBudget201Tests(unittest.TestCase):
    def setUp(self):
        self.old = sys.getswitchinterval()
        self.addCleanup(sys.setswitchinterval, self.old)
        state = patch('runtime_threading._requested_ms', None)
        state.start()
        self.addCleanup(state.stop)

    def test_runtime_default_sets_one_ms_and_exports_actual_interval(self):
        result = configure_runtime_threads({})
        self.assertAlmostEqual(sys.getswitchinterval(), .001, places=6)
        self.assertAlmostEqual(result['actual_ms'], 1, places=4)
        self.assertEqual(result['configured_ms'], 1)

    def test_zero_preserves_interpreter_setting_and_limits_are_bounded(self):
        sys.setswitchinterval(.007)
        configure_runtime_threads({'runtime_thread_switch_ms': 0})
        self.assertAlmostEqual(sys.getswitchinterval(), .007, places=6)
        configure_runtime_threads({'runtime_thread_switch_ms': 100000})
        self.assertAlmostEqual(sys.getswitchinterval(), .010, places=6)

    def test_invalid_option_uses_finite_runtime_default(self):
        configure_runtime_threads({'runtime_thread_switch_ms': 'bad'})
        self.assertAlmostEqual(runtime_threading_snapshot()['actual_ms'], 1, places=4)

    def test_trace_distinguishes_thread_cpu_from_all_off_cpu_waits(self):
        trace = RuntimeDebugTrace()
        trace.set_enabled(True)
        with patch('telemetry.time.monotonic', side_effect=[10., 10.08]), \
             patch('telemetry.time.thread_time', side_effect=[1., 1.03]):
            token = trace.begin('sqlite_probe')
            trace.end(token)
        fields = trace.export()['entries'][-1]['fields']
        self.assertEqual(fields['thread_cpu_ms'], 30)
        self.assertEqual(fields['off_cpu_ms'], 50)

    def test_cross_thread_trace_end_does_not_compare_unrelated_cpu_clocks(self):
        trace = RuntimeDebugTrace()
        trace.set_enabled(True)
        token = trace.begin('handoff')
        worker = threading.Thread(target=trace.end, args=(token,))
        worker.start()
        worker.join(3)
        self.assertNotIn('thread_cpu_ms', trace.export()['entries'][-1].get('fields', {}))


class WarmCandidate201Tests(unittest.TestCase):
    def test_warm_four_candidates_keep_full_sha_without_copying_global_maps(self):
        import os
        with patch.dict(os.environ):
            from tools.benchmark_shadow_context import Fixture, STAMP, training_contract
        from policy import MultiHorizonPolicy
        import context_tournament_policy_candidate as candidates
        with tempfile.TemporaryDirectory(prefix='candidate-warm-201-') as directory, ExitStack() as stack:
            stack.enter_context(patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion))
            stack.enter_context(patch.object(promotion, '_migrate_schema', promotion._migrate_schema))
            stack.enter_context(training_contract())
            stack.enter_context(patch('time.time', return_value=STAMP))
            fixture = Fixture(Store(Path(directory) / 'test.db'))
            fixture.observe()
            fixture.observe()
            class CountingMap(Mapping):
                def __init__(self, values):
                    self.values, self.iterations = values, 0
                def __getitem__(self, key):
                    return self.values[key]
                def __len__(self):
                    return len(self.values)
                def __iter__(self):
                    self.iterations += 1
                    return iter(self.values)
            states = fixture.engine.state_map = CountingMap(fixture.engine.state_map)
            registry = fixture.engine.entity_registry = CountingMap(fixture.engine.entity_registry)
            with patch.object(candidates, '_verify_candidate_checksum', wraps=candidates._verify_candidate_checksum) as checks:
                result = fixture.observe()
            self.assertEqual(len(result['predictions']), 4)
            self.assertEqual(checks.call_count, 4)
            self.assertEqual((states.iterations, registry.iterations), (0, 0))
