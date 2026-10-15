"""Compact cold comparisons and pending foreground events take priority."""
from contextlib import contextmanager
import copy
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from episode_evaluator import EpisodeEvaluator
from settings import OPTIONS
from storage_budget import StoragePressure
import shadow_observation_scheduler as scheduler


class CompactComparisonTests(unittest.TestCase):
    def setUp(self):
        from test_release_203_episode_comparison_cache import EpisodeComparison203Tests
        self.f = EpisodeComparison203Tests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.add()

    def cold(self):
        self.f.e._comparison_cache.entries.clear()
        self.f.e._comparison_cache.bytes = 0
        return self.f.e.compare_policies('a', 'p', 'c')

    def test_full_result_matches_complete_reference_reader(self):
        self.assertEqual(self.cold(), self.f.ref.compare_policies('a', 'p', 'c'))
        self.assertTrue(self.f.e.comparison_cache_snapshot()['compact_comparison_indexes'])

    def test_explain_proves_no_source_table_column_or_json_validation_function_reads(self):
        trace = []
        with self.f.store.connection_session() as c:
            c.set_trace_callback(trace.append)
            self.cold()
            c.set_trace_callback(None)
            query = next(sql for sql in trace if 'AS observation_valid' in sql)
            bytecode = c.execute('EXPLAIN ' + query).fetchall()
            tables = {r[0] for r in c.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name IN ('episode_evaluator_episodes','episode_evaluator_policy_results')")}
            source_cursors = {r[2] for r in bytecode if r[1] == 'OpenRead' and r[3] in tables}
            self.assertFalse([r for r in bytecode if r[1] == 'Column' and r[2] in source_cursors])
            self.assertFalse([r for r in bytecode if r[1] in ('Function', 'PureFunc') and 'json_valid' in str(r[5])])
            self.assertIn('idx_episode_compare_pair_206', query)

    def test_external_validity_update_invalidates_and_malformed_paired_audit_still_fails(self):
        self.cold()
        with sqlite3.connect(self.f.store.path) as c:
            c.execute("UPDATE episode_evaluator_policy_results SET evidence_json='bad-json' WHERE policy_key='c'")
        with self.assertRaises(json.JSONDecodeError):
            self.f.e.compare_policies('a', 'p', 'c')

    def test_rollback_restores_both_validity_and_metadata(self):
        expected = self.cold()
        with self.f.store.conn() as c:
            c.execute('BEGIN')
            c.execute("UPDATE episode_evaluator_policy_results SET evidence_json='bad-json'")
            c.execute("UPDATE episode_evaluator_episodes SET labels_json='{}'")
            c.rollback()
        self.assertEqual(self.cold(), expected)

    def test_empty_null_and_large_valid_audits_retain_parity(self):
        with self.f.store.conn() as c:
            c.execute("UPDATE episode_evaluator_episodes SET observability_json=''")
            c.execute('UPDATE episode_evaluator_policy_results SET evidence_json=?',
                      (json.dumps({'large': 'x' * 300000}),))
        self.assertEqual(self.cold(), self.f.ref.compare_policies('a', 'p', 'c'))

    def test_dropped_optional_index_falls_back_to_fresh_native_reader(self):
        expected = self.cold()
        with self.f.store.conn() as c:
            c.execute('DROP INDEX idx_episode_compare_pair_206')
        self.assertEqual(self.cold(), expected)
        self.assertFalse(self.f.e._comparison_compact_sql)

    def test_index_storage_pressure_keeps_runtime_and_full_reader_available(self):
        original = self.f.store.conn
        class Connection:
            def __init__(self, wrapped): self.wrapped = wrapped
            def __getattr__(self, name): return getattr(self.wrapped, name)
            def execute(self, sql, *args):
                if 'CREATE INDEX' in sql and 'idx_episode_compare_pair_206' in sql:
                    raise StoragePressure('test optional index pressure')
                return self.wrapped.execute(sql, *args)
        @contextmanager
        def pressure():
            with original() as c:
                yield Connection(c)
        with patch.object(self.f.store, 'conn', pressure):
            fallback = EpisodeEvaluator(self.f.store)
        self.assertFalse(fallback._comparison_compact_sql)
        self.assertIn('pressure', fallback._comparison_index_error)
        self.assertEqual(fallback.compare_policies('a', 'p', 'c'), self.cold())

    def test_unpaired_corrupt_audit_is_not_an_error_for_unrelated_comparison(self):
        expected = self.cold()
        self.f.add('unpaired', policies=('unrelated',))
        with self.f.store.conn() as c:
            c.execute("UPDATE episode_evaluator_policy_results SET evidence_json='invalid' WHERE policy_key='unrelated'")
        self.assertEqual(self.cold(), expected)


class ForegroundDeferralTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.q = scheduler.ShadowObservationScheduler(lambda: self.now)
        self.a = support.agent()
        self.states = {'light.kitchen': support.state('light.kitchen', 'off')}
        self.p = SimpleNamespace(schema=SimpleNamespace(entities=['sensor.a'], feature_contract_version=4))
        patcher = patch.dict(OPTIONS, {'runtime_shadow_observation_interval_ms': 1000,
                                     'runtime_shadow_max_deferral_ms': 5000})
        patcher.start(); self.addCleanup(patcher.stop)
        self.q.finish(self.admit(), success=True)

    def admit(self, changed=(), pending=True):
        return self.q.admit(self.a, self.states, changed, self.p, pending_realtime=pending)

    def test_due_optional_work_yields_to_pending_foreground_without_advancing_deadline(self):
        self.now = 11.
        self.assertIsNone(self.admit())
        self.now = 14.9
        self.assertIsNone(self.admit())
        self.assertEqual(self.q.deferred_for_realtime, 2)
        self.assertEqual(self.q.completed[self.a['id']][0], 10.)

    def test_busy_home_still_admits_at_maximum_deferral(self):
        self.now = 15.
        self.assertIsNotNone(self.admit())

    def test_quiet_frame_admits_immediately_after_normal_interval(self):
        self.now = 11.
        self.assertIsNotNone(self.admit(pending=False))

    def test_target_label_and_own_ack_event_bypass_pending_foreground(self):
        self.now = 10.01
        self.assertIsNotNone(self.admit({'light.kitchen'}))
        self.states['light.kitchen']['state'] = 'on'
        self.assertIsNotNone(self.admit({'sensor.a'}))

    def test_new_model_and_schema_bypass_pending_foreground(self):
        self.p.schema.entities.append('sensor.b')
        self.assertIsNotNone(self.admit())

    def test_failed_work_retries_immediately_even_with_foreground_pressure(self):
        self.now = 11.
        ticket = self.admit(pending=False)
        self.q.finish(ticket, success=False)
        self.assertIsNotNone(self.admit())

    def test_zero_cadence_or_zero_deferral_retains_normal_due_behavior(self):
        self.now = 11.
        OPTIONS['runtime_shadow_max_deferral_ms'] = 0
        self.assertIsNotNone(self.admit())
        OPTIONS['runtime_shadow_observation_interval_ms'] = 0
        self.now = 10.01
        self.assertIsNotNone(self.admit())

    def test_pending_reader_keeps_all_triggers_and_ignores_unrouted_dirty_entities(self):
        engine = SimpleNamespace(lock=threading.RLock(), pending_target_changes={},
            ready_target_changes={}, dirty_entities={'sensor.unrelated'}, dependency_agents={'sensor.a': {'a'}})
        self.assertFalse(scheduler.realtime_pending(engine))
        engine.dirty_entities.add('sensor.a')
        before = copy.deepcopy(engine.dirty_entities)
        self.assertTrue(scheduler.realtime_pending(engine))
        self.assertEqual(engine.dirty_entities, before)
        engine.dirty_entities.clear()
        for name in ('pending_target_changes', 'ready_target_changes'):
            setattr(engine, name, {'light.kitchen': {'sensor.a'}})
            self.assertTrue(scheduler.realtime_pending(engine))
            self.assertEqual(getattr(engine, name), {'light.kitchen': {'sensor.a'}})
            setattr(engine, name, {})

    def test_invalid_and_extreme_deferral_are_bounded(self):
        for value, expected in [('invalid',5.), (float('nan'),5.), (-1,0.), (999999,30.)]:
            OPTIONS['runtime_shadow_max_deferral_ms'] = value
            self.assertEqual(scheduler.max_deferral_seconds(), expected)


class TournamentBackpressureIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_release_205_control_priority import ObserverIntegrationTests
        self.f = ObserverIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        patcher = patch.dict(OPTIONS, {'runtime_shadow_max_deferral_ms': 5000})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_real_wrapper_yields_due_work_without_consuming_next_target_trigger(self):
        f = self.f
        engine, service = f.fixture.engine, f.fixture.service
        f.run_agent()
        engine.pending_target_changes = {'light.next': {'binary_sensor.next'}}
        original = copy.deepcopy(engine.pending_target_changes)
        with patch.object(service, 'observe_shadow', wraps=service.observe_shadow) as observe:
            f.now = 11.
            self.assertEqual(f.run_agent({f.fixture.active}), 'production-result')
            observe.assert_not_called()
            self.assertEqual(engine.pending_target_changes, original)
            f.now = 15.
            self.assertEqual(f.run_agent({f.fixture.active}), 'production-result')
            self.assertEqual(observe.call_count, 1)
        self.assertEqual(service._observation_scheduler.deferred_for_realtime, 1)

    def test_real_wrapper_scores_all_target_edges_despite_other_pending_targets(self):
        f = self.f
        f.run_agent()
        f.fixture.engine.pending_target_changes = {'light.next': {'binary_sensor.next'}}
        for value in ('on', 'off', 'on'):
            f.now += .01
            f.fixture.states[f.fixture.target]['state'] = value
            self.assertEqual(f.run_agent({f.fixture.target}), 'production-result')
        model = f.fixture.service._load_shadow_model(f.fixture.agent['id'], f.fixture.challenger, 2)
        self.assertEqual(model['samples'], 3)
        self.assertEqual(f.fixture.service._observation_scheduler.target_forced, 3)


if __name__ == '__main__':
    unittest.main()
