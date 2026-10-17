"""Realtime return, causal FIFO frames and fail-closed deferred Tournament work."""
import copy
from contextlib import nullcontext
import pickle
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from context import TemporalHistory
from context_tournament import install
from settings import now_ts
from tournament_observation_clock import observation_clock
from tournament_observation_queue import DeferredTournamentObservations
import test_context_tournament_shadow as base_fixture


class DeferredTournamentTests(unittest.TestCase):
    def setUp(self):
        self.f = base_fixture.ContextTournamentShadowTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.engine, self.service = self.f.engine, self.f.service
        self.engine.models[self.f.agent['id']] = self.f.policy
        self.engine._inference_tls = threading.local()
        self.engine._inference_tls.state_revision = 12
        self.engine.stop_event = threading.Event()
        self.queue = self.service._deferred_observations
        self.addCleanup(self.queue.close)

    def idle(self):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            status = self.queue.snapshot()
            if not status['pending'] and not status['active']:
                self.assertEqual(status['errors'], 0, status)
                return
            time.sleep(.001)
        self.fail(str(self.queue.snapshot()))

    def test_shadow_and_control_return_next_decision_while_observer_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.service.observe_shadow
        def slow(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args)
        self.service.observe_shadow = slow
        self.f.agent['mode'] = 'shadow'
        self.assertEqual(self.f._process('off'), 'production-result')
        self.assertTrue(entered.wait(1))
        try:
            for mode in ('control', 'shadow'):
                self.f.agent['mode'] = mode
                self.engine.active_prediction = 1.
                started = time.perf_counter()
                self.assertEqual(self.f._process('on'), 'production-result')
                self.assertLess(time.perf_counter()-started, .2)
                self.assertEqual(self.engine.runtime['agent-1']['last_prediction'], 1.)
            self.assertEqual(self.engine.executor_calls, 0)
        finally:
            release.set()
        self.idle()
        self.assertEqual(self.queue.snapshot()['processed'], 3)

    def test_exact_event_context_is_detached_from_newer_runtime_history_and_policy(self):
        f = self.f
        self.engine.temporal_history = TemporalHistory()
        self.engine.temporal_history.add(f.target, 99., f.states[f.target])
        self.engine.temporal_history.add(f.target, 101., base_fixture.st(f.target, 'on'))
        self.engine.runtime['agent-1'] = dict(last_prediction=0., last_inference_ts=100.,
            last_change_origin='manual_user', last_intent={'decision_id':'old'},
            context_meta={'home_forecast': {'occupancy_now': .2}}, automation_priors=[{'enabled':True}])
        self.f.policy.weights = [1., 2.]
        job = self.queue._capture(f.agent, f.states, {f.target}, None)
        self.engine.runtime['agent-1'].update(last_prediction=1., last_intent={'decision_id':'new'})
        self.engine.temporal_history.samples.clear()
        self.f.policy.weights[0] = 99.
        f.states[f.target]['state'] = 'on'
        seen = []
        def execute(agent, states, changed, ticket):
            view = self.service.engine
            seen.append((states[f.target]['state'], view.runtime['agent-1']['last_prediction'],
                view.runtime['agent-1']['last_intent']['decision_id'], view.state_revision,
                view.models['agent-1'].weights[:], now_ts(),
                list(view.temporal_history.samples[f.target]),
                view.context.forecast(f.target, 999), view.runtime['agent-1']['automation_priors']))
            view.models['agent-1'].weights[1] = -1.
        self.service._execute_observation = execute
        self.queue._execute(job)
        row = seen[0]
        self.assertEqual(row[:6], ('off', 0., 'old', 12, [1.,2.], 100.))
        self.assertEqual([ts for ts,_ in row[6]], [99.])
        self.assertEqual(row[7], {'occupancy_now':.2})
        self.assertEqual(row[8], [{'enabled':True}])
        self.assertEqual(self.f.policy.weights, [99.,2.])
        self.assertIs(self.service.engine.models['agent-1'], self.f.policy)

    def test_overflow_breaks_pairing_and_preserves_accepted_models(self):
        self.queue.thread = SimpleNamespace(is_alive=lambda: False)
        self.queue.max_jobs = 2
        self.f._process('off'); self.f._process('on'); self.f._process('off')
        self.assertLessEqual(self.queue.snapshot()['pending'], 2)
        self.assertGreater(self.queue.snapshot()['dropped'], 0)
        self.assertLessEqual(self.queue.snapshot()['bytes'], self.queue.snapshot()['max_bytes'])
        for job in list(self.queue.pending):
            self.queue._execute(job)
        model = self.service._load_shadow_model('agent-1', self.f.challenger, 2)
        self.assertEqual(model['samples'], 0)
        self.assertGreater(self.queue.snapshot()['gaps'], 0)
        self.queue.thread = None

    def test_storage_pressure_never_falls_back_to_foreground_observation(self):
        status = {'level':'critical', 'reason':'database_budget'}
        with patch('tournament_observation_queue.storage_status', return_value=status), \
             patch.object(self.service, 'observe_shadow', side_effect=AssertionError('fit under pressure')):
            self.assertEqual(self.f._process('on'), 'production-result')
        self.assertEqual(self.queue.snapshot()['storage_skips'], 1)
        self.assertEqual(self.queue.snapshot()['queued'], 0)
        self.f._process('off'); self.idle()
        self.assertEqual(self.queue.snapshot()['gaps'], 1)
        self.assertEqual(self.service._load_shadow_model('agent-1', self.f.challenger, 2)['samples'], 0)

    def test_redundant_sensor_refreshes_coalesce_without_erasing_target_edges(self):
        from settings import OPTIONS
        self.queue.thread = SimpleNamespace(is_alive=lambda: False)
        try:
            with patch.dict(OPTIONS, runtime_shadow_observation_interval_ms=0):
                for _ in range(5):
                    self.engine.process_agent(self.f.agent, self.f.states, {self.f.active})
                self.assertEqual(len(self.queue.pending), 1)
                self.assertEqual(self.queue.snapshot()['coalesced'], 4)
                self.f._process('on'); self.f._process('off')
                self.assertEqual(len(self.queue.pending), 3)
                for job in list(self.queue.pending):
                    self.queue._execute(job)
            self.assertEqual(self.service._load_shadow_model('agent-1', self.f.challenger, 2)['samples'], 2)
            self.assertEqual(self.queue.snapshot()['gaps'], 0)
        finally:
            self.queue.thread = None

    def test_held_root_keeps_independent_target_labels_at_the_new_observation_time(self):
        self.engine.runtime['agent-1'] = dict(last_inference_ts=123.)
        self.assertEqual(self.f._process('off'), 'production-result')
        self.idle()
        self.assertEqual(self.f._process('on'), 'production-result')
        self.idle()
        self.assertEqual(self.service._load_shadow_model('agent-1', self.f.challenger, 2)['samples'], 1)
        self.assertGreater(self.service._shadow_runtime['agent-1']['last_scored_ts'], 123.)

    def test_model_replacement_and_generation_reset_drop_stale_work(self):
        job = self.queue._capture(self.f.agent, self.f.states, set(), None)
        with patch.object(self.service, '_execute_observation') as observe:
            self.engine.models['agent-1'] = SimpleNamespace(schema=self.f.policy.schema)
            self.queue._execute(job)
            observe.assert_not_called()
            self.engine.models['agent-1'] = self.f.policy
            self.f.store._provenance_generation_revision = 1
            self.queue._execute(job)
            observe.assert_not_called()
        self.assertEqual(self.queue.snapshot()['stale'], 2)

    def test_probation_requires_fresh_synchronous_context_and_never_waits_on_fit(self):
        job = self.queue._capture(self.f.agent, self.f.states, set(), None)
        self.service.schema_probation_active = lambda aid: True
        with patch.object(self.service, '_execute_observation') as observe:
            self.queue._execute(job)
            observe.assert_not_called()
        self.assertIn('agent-1', self.queue.sync_required)
        with self.queue.observation_lock:
            started = time.perf_counter()
            self.assertEqual(self.f._process('on'), 'production-result')
            self.assertLess(time.perf_counter()-started, .1)
        self.f._process('off')
        self.assertEqual(self.queue.snapshot()['sync_observations'], 1)

    def test_memory_budget_respects_container_headroom(self):
        self.queue.memory_sample_ts = 0
        with patch('tournament_observation_queue.memory_headroom', return_value=(512, 140)):
            self.queue._sample_memory()
        self.assertLessEqual(self.queue.max_bytes, 3*1024**2)
        self.queue.memory_sample_ts = 0
        with patch('tournament_observation_queue.memory_headroom', return_value=(512, 64)):
            self.queue._sample_memory()
        self.assertEqual(self.queue.max_bytes, 0)
        self.f._process('on')
        self.assertEqual(self.queue.snapshot()['queued'], 0)

    def test_observation_clock_does_not_change_foreground_or_deadlines(self):
        seen = []
        with observation_clock(123.):
            before = time.monotonic()
            worker = threading.Thread(target=lambda: seen.append(now_ts()))
            worker.start(); worker.join(1)
            self.assertEqual(now_ts(), 123.)
            self.assertGreaterEqual(time.monotonic(), before)
        self.assertNotEqual(seen[0], 123.)


class PromotionDeferralTests(unittest.TestCase):
    def test_ready_challenger_rechecks_original_gates_on_fresh_pass(self):
        import test_context_tournament_promotion as fixture
        f = fixture.PromotionIntegrationTests()
        f.setUp()
        try:
            deferred = SimpleNamespace(is_deferred=lambda: True, require_sync=lambda aid: requested.append(aid))
            requested = []
            f.service._deferred_observations = deferred
            before = list(f.policy.schema.entities)
            f.service.observe_shadow(f.agent, {}, set())
            self.assertEqual(f.policy.schema.entities, before)
            self.assertEqual(requested, [f.agent['id']])
            self.assertIsNone(f.store.get_model(f.agent['id']))
            deferred.is_deferred = lambda: False
            f.service.observe_shadow(f.agent, {}, set())
            self.assertIn(f.challenger, f.policy.schema.entities)
            self.assertIn(f.challenger, f.store.get_model(f.agent['id'])['schema']['entities'])
        finally:
            f.tearDown()


class CandidateParityTests(unittest.TestCase):
    def test_deferred_timing_feedback_updates_live_champion_and_its_checkpoint_identity(self):
        import sys
        sys.path.insert(0, str(support.ROOT/'tools'))
        import benchmark_shadow_context as fixture
        import context_tournament_promotion as promotion
        import context_tournament_metrics as metrics
        import fast_light_objective as objective
        from rewards import RewardEngine
        with tempfile.TemporaryDirectory() as directory, fixture.training_contract(), \
             patch.object(promotion, '_migrate_schema', promotion._migrate_schema), \
             patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion), \
             patch.object(metrics, 'metric_row', metrics.metric_row), \
             patch.object(objective, '_patch_promotion_for_timing'), \
             patch('time.time', return_value=fixture.STAMP):
            f = fixture.Fixture(fixture.Store(Path(directory)/'timing.db'))
            f.engine.runtime_for = lambda agent: {}
            f.engine.stop_event = threading.Event()
            self.addCleanup(f.engine.stop_event.set)
            f.engine.executor = SimpleNamespace(reward_engine=RewardEngine())
            objective.install(f.service)
            f.service._execute_observation = lambda agent, states, changed, ticket: f.service.observe_shadow(agent, states, changed)
            queue = f.service._deferred_observations
            baseline = f.live.total_updates
            for step, value in enumerate(('off', 'on')):
                stamp = fixture.STAMP+step*2
                target = f.agent['target_entity']
                f.states[target] = fixture.state(target, value, stamp)
                f.engine.runtime[f.agent['id']] = dict(last_prediction=1., last_inference_ts=stamp,
                    automation_priors=[{'enabled':True}], last_change_origin='manual_user')
                with observation_clock(stamp):
                    job = queue._capture(f.agent, f.states, {target}, None)
                    queue._execute(job)
            self.assertGreater(f.live.total_updates, baseline)
            self.assertEqual(f.service.fast_light_benchmark(f.agent)['samples'], 1)
            self.assertGreater(f.engine.runtime[f.agent['id']]['last_reward'], 0)
            self.assertEqual(f.service.fast_light_persistence_snapshot()['pending_models'], 1)

    def test_four_real_candidate_policies_keep_features_scores_and_training_in_fifo(self):
        import sys
        import uuid
        sys.path.insert(0, str(support.ROOT/'tools'))
        import benchmark_shadow_context as fixture
        import context_tournament_promotion as promotion
        from policy import MultiHorizonPolicy

        def run(deferred):
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(promotion, '_migrate_schema', promotion._migrate_schema), \
                 patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion), \
                 fixture.training_contract(), patch('uuid.uuid4', return_value=uuid.UUID(int=7)), \
                 patch('time.time', return_value=fixture.STAMP):
                f = fixture.Fixture(fixture.Store(Path(directory)/'parity.db'))
                if deferred:
                    # The shipped Root policy has this diagnostics-only closure.
                    # It must neither be pickled nor invoked by detached candidates.
                    def root_features(*args, **kwargs):
                        raise AssertionError('Root diagnostics callback used by background clone')
                    f.live.features = root_features
                    f.live._candidate_shadow_exact_context = True
                f.service._execute_observation = lambda agent, states, changed, ticket: f.service.observe_shadow(agent, states, changed)
                queue = f.service._deferred_observations
                rows, jobs = [], []
                original = MultiHorizonPolicy.features
                def features(policy, *args, **kwargs):
                    result = original(policy, *args, **kwargs)
                    rows.append(result)
                    return result
                for step in range(6):
                    stamp = fixture.STAMP+step
                    target = f.agent['target_entity']
                    f.states[target] = fixture.state(target, 'on' if step%2 else 'off', stamp)
                    f.states[f.challengers[0]] = fixture.state(f.challengers[0], 20+step, stamp)
                    f.engine.runtime[f.agent['id']] = dict(last_prediction=float(step%2),
                        last_change_origin='manual_user', last_inference_ts=stamp)
                    f.engine.state_revision = step
                    f.temporal.add(target, stamp, f.states[target])
                    with observation_clock(stamp):
                        if deferred:
                            jobs.append(queue._capture(f.agent, f.states, {target}, None))
                        else:
                            with patch.object(MultiHorizonPolicy, 'features', new=features):
                                f.service.observe_shadow(f.agent, f.states, {target})
                if deferred:
                    f.engine.runtime[f.agent['id']]['last_prediction'] = 999.
                    f.temporal.samples.clear()
                    with patch.object(MultiHorizonPolicy, 'features', new=features):
                        for job in jobs:
                            queue._execute(job)
                models = [f.service._load_shadow_model(f.agent['id'], eid, 2) for eid in f.challengers]
                return rows, [dict(samples=m['samples'], heads=m['candidate_policy']['heads']) for m in models]
        direct, direct_models = run(False)
        deferred, deferred_models = run(True)
        self.assertEqual(len(direct), 24)
        self.assertEqual(direct, deferred)
        self.assertEqual(direct_models, deferred_models)
        self.assertTrue(all(m['samples'] == 5 for m in direct_models))


if __name__ == '__main__':
    unittest.main()
