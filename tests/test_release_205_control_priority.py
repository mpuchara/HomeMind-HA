"""Fast foreground decisions with bounded candidate cadence and complete labels."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from settings import OPTIONS
import settings
import observation_contract as observation
import shadow_observation_scheduler as scheduler
from shared_observation_features import shared_entity_observations


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.q = scheduler.ShadowObservationScheduler(lambda: self.now)
        self.a = support.agent()
        self.states = {'light.kitchen': support.state('light.kitchen', 'off')}
        self.p = SimpleNamespace(schema=SimpleNamespace(entities=['sensor.a'], feature_contract_version=4))
        patcher = patch.dict(OPTIONS, {'runtime_shadow_observation_interval_ms': 1000})
        patcher.start()
        self.addCleanup(patcher.stop)

    def admit(self, changed=()):
        return self.q.admit(self.a, self.states, changed, self.p)

    def run_observer(self, changed=(), success=True):
        ticket = self.admit(changed)
        self.q.finish(ticket, success=success)
        return ticket

    def test_first_then_due_interval_and_idle_skip(self):
        self.assertIsNotNone(self.run_observer())
        self.now += .2
        self.assertIsNone(self.admit({'sensor.a'}))
        self.now += .8
        self.assertIsNotNone(self.run_observer())
        self.assertEqual((self.q.executed, self.q.skipped), (2, 1))

    def test_zero_preserves_every_observation(self):
        OPTIONS['runtime_shadow_observation_interval_ms'] = 0
        for _ in range(3):
            self.assertIsNotNone(self.run_observer())

    def test_target_event_with_same_value_is_always_admitted(self):
        self.run_observer()
        self.assertIsNotNone(self.run_observer({'light.kitchen'}))
        self.assertEqual(self.q.target_forced, 1)

    def test_changed_value_without_trigger_set_still_forces_every_edge(self):
        self.run_observer()
        for value in ('on', 'off', 'on', 'off'):
            self.now += .001
            self.states['light.kitchen'] = support.state('light.kitchen', value)
            self.assertIsNotNone(self.run_observer({'sensor.a'}))
        self.assertEqual(self.q.target_forced, 4)

    def test_availability_change_is_not_suppressed(self):
        self.run_observer()
        for value in ('unavailable', 'off'):
            self.states['light.kitchen'] = support.state('light.kitchen', value)
            self.assertIsNotNone(self.run_observer())

    def test_failed_observation_never_starts_cooldown(self):
        self.assertIsNotNone(self.run_observer(success=False))
        self.assertIsNotNone(self.run_observer())
        self.assertEqual((self.q.errors, self.q.executed), (1, 1))

    def test_new_schema_policy_or_semantic_contract_forces_observation(self):
        self.run_observer()
        self.p.schema.entities.append('sensor.b')
        self.assertIsNotNone(self.run_observer())
        self.p.schema.feature_contract_version = 3
        self.assertIsNotNone(self.run_observer())
        self.p = SimpleNamespace(schema=self.p.schema)
        self.assertIsNotNone(self.run_observer())

    def test_clock_rewind_is_fresh(self):
        self.run_observer()
        self.now = 1.
        self.assertIsNotNone(self.run_observer())

    def test_memory_bound_evicts_old_schedules_and_keeps_new_agents_immediate(self):
        with patch.object(scheduler, 'MAX_AGENTS', 2):
            for i in range(5):
                self.a['id'] = str(i)
                self.assertIsNotNone(self.run_observer())
                self.assertLessEqual(len(self.q.completed), 2)
            self.a['id'] = '0'
            self.assertIsNotNone(self.run_observer())

    def test_bad_options_are_finite_and_bounded(self):
        for value, expected in [('bad', 1.), (float('nan'), 1.), (float('inf'), 1.),
                                (-1, 0.), (99999, 5.), (0, 0.)]:
            OPTIONS['runtime_shadow_observation_interval_ms'] = value
            self.assertEqual(scheduler.interval_seconds(), expected)


class ObserverIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_context_tournament_shadow import ContextTournamentShadowTests
        self.fixture = ContextTournamentShadowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.now = 10.
        self.fixture.service._observation_scheduler.clock = lambda: self.now
        self.fixture.engine._inference_tls = threading.local()
        self.fixture.engine._inference_tls.state_revision = 1
        patcher = patch.dict(OPTIONS, {'runtime_shadow_observation_interval_ms': 1000})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_agent(self, changed=()):
        f = self.fixture
        return f.engine.process_agent(f.agent, f.states, set(changed))

    def test_foreground_runs_on_every_sensor_event_while_optional_work_is_sampled(self):
        f = self.fixture
        original = f.service.observe_shadow
        with patch.object(f.service, 'observe_shadow', wraps=original) as observe:
            for _ in range(8):
                self.assertEqual(self.run_agent({f.active}), 'production-result')
                self.now += .1
            self.assertEqual(observe.call_count, 1)
        self.assertEqual(f.service._observation_scheduler.snapshot()['skipped'], 7)

    def test_every_independent_target_label_is_scored_inside_cooldown(self):
        f = self.fixture
        self.run_agent({f.active})
        self.now += .01
        self.run_agent({f.active})
        for value in ('on', 'off', 'on'):
            f.states[f.target]['state'] = value
            self.now += .01
            self.run_agent({f.target})
        model = f.service._load_shadow_model(f.agent['id'], f.challenger, 2)
        self.assertEqual(model['samples'], 3)

    def test_own_command_ack_is_processed_but_never_becomes_independent_label(self):
        f = self.fixture
        self.run_agent()
        f.engine.next_origin = 'own_command'
        f.states[f.target]['state'] = 'on'
        self.run_agent({f.target})
        model = f.service._load_shadow_model(f.agent['id'], f.challenger, 2)
        self.assertEqual(model['samples'], 0)
        self.assertEqual(f.service._observation_scheduler.executed, 2)

    def test_explicit_direct_calls_are_not_throttled(self):
        f = self.fixture
        del f.engine._inference_tls.state_revision
        with patch.object(f.service, 'observe_shadow', wraps=f.service.observe_shadow) as observe:
            for _ in range(3):
                self.run_agent({f.active})
        self.assertEqual(observe.call_count, 3)

    def test_failed_observer_does_not_delay_foreground_or_the_next_attempt(self):
        f = self.fixture
        with patch.object(f.service, 'observe_shadow', side_effect=ValueError('observer failed')):
            self.assertEqual(self.run_agent(), 'production-result')
        self.assertEqual(self.run_agent(), 'production-result')
        self.assertEqual(f.service._observation_scheduler.errors, 1)
        self.assertEqual(f.service._observation_scheduler.executed, 1)


class ProductionFeatureIdentityTests(unittest.TestCase):
    def test_cached_candidate_with_older_agent_config_object_shares_entity_descriptors(self):
        from test_release_204_shared_entity_features import SharedEntityFeaturesTests
        f = SharedEntityFeaturesTests()
        f.setUp()
        candidate_agent = dict(f.agent)
        candidate_agent['name'] = 'older routing snapshot'
        with shared_entity_observations(f.agent, f.states, f.temporal, 1_700_000_100.), \
             patch.object(observation, '_entity_observation', wraps=observation._entity_observation) as compute:
            expected = f.build()
            self.assertEqual(f.build(agent=candidate_agent), expected)
            self.assertEqual(compute.call_count, len(f.entities))

    def test_real_warm_candidates_with_refreshed_agent_config_keep_full_sha(self):
        from contextlib import ExitStack
        from tools.benchmark_shadow_context import Fixture, STAMP, training_contract
        from storage import Store
        import context_tournament_policy_candidate as candidates
        import context_tournament_promotion as promotion
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            stack.enter_context(patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion))
            stack.enter_context(patch.object(promotion, '_migrate_schema', promotion._migrate_schema))
            stack.enter_context(training_contract())
            stack.enter_context(patch('time.time', return_value=STAMP))
            f = Fixture(Store(Path(folder) / 'fixture.db'))
            f.observe(); f.observe()
            f.agent = dict(f.agent)
            with patch.object(candidates, '_verify_candidate_checksum', wraps=candidates._verify_candidate_checksum) as sha, \
                 patch.object(observation, '_feature_observation', wraps=observation._feature_observation) as compute:
                self.assertEqual(len(f.observe()['predictions']), 4)
            self.assertEqual(sha.call_count, 4)
            self.assertEqual(compute.call_count, 44)


class ImmediateEventDefaultsTests(unittest.TestCase):
    def test_old_default_delays_migrate_but_custom_delay_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'options.json'
            with patch.object(settings, 'OPTIONS_PATH', path):
                for value, expected in [(25, 0), (75, 0), (0, 0), (150, 150)]:
                    path.write_text(json.dumps({'realtime_inference_debounce_ms': value}))
                    self.assertEqual(settings.load_options()['realtime_inference_debounce_ms'], expected)
        self.assertEqual(settings.DEFAULT_OPTIONS['realtime_inference_debounce_ms'], 0)


if __name__ == '__main__':
    unittest.main()
