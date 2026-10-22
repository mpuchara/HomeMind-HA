"""Causal observation reuse and truthful sensor-to-service timing boundaries."""
from contextlib import ExitStack
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import support
from decision_observation import decision_observation, signature_features
from shared_inference_context import shared_inference_temporal
from observation_contract import _latest_communication
import action_latency


class DecisionObservationTests(unittest.TestCase):
    def setUp(self):
        self.policy = SimpleNamespace(schema=object(), model_revision='one', features=Mock(return_value=('fresh',)))
        self.states, self.temporal = {}, object()
        self.observation = ({1: .2}, {1: ['motion']}, {'reconstruction_complete': True})

    def frame(self):
        return decision_observation(self.policy, self.states, self.temporal, 10., self.observation)

    def read(self, **changes):
        args = dict(policy=self.policy, states=self.states, temporal=self.temporal, timestamp=10.)
        args.update(changes)
        return signature_features(**args)

    def test_matching_decision_reuses_tuple_without_feature_rebuild(self):
        with self.frame():
            self.assertIs(self.read(), self.observation)
            self.assertIs(self.read(), self.observation)
        self.policy.features.assert_not_called()
        self.assertEqual(self.read(), ('fresh',))

    def test_time_state_temporal_policy_and_schema_changes_fall_back(self):
        with self.frame():
            for kw in ({'timestamp': 11}, {'states': {}}, {'temporal': object()}):
                self.assertEqual(self.read(**kw), ('fresh',))
            self.policy.model_revision = 'two'
            self.assertEqual(self.read(), ('fresh',))
            self.policy.model_revision = 'one'
            self.policy.schema = object()
            self.assertEqual(self.read(), ('fresh',))
        self.assertEqual(self.policy.features.call_count, 5)

    def test_exception_nested_frame_and_thread_do_not_leak(self):
        with self.frame():
            with self.assertRaises(ValueError):
                with decision_observation(self.policy, self.states, self.temporal, 11., ('nested',)):
                    raise ValueError('exit')
            self.assertIs(self.read(), self.observation)
            results = []
            worker = threading.Thread(target=lambda: results.append(self.read()))
            worker.start(); worker.join(3)
            self.assertEqual(results, [('fresh',)])
        self.assertEqual(self.read(), ('fresh',))

    def test_actual_v12_and_legacy_signatures_equal_and_owned(self):
        import teaching, observation_contract
        self.states['sensor.a'] = support.state('sensor.a', 'on')
        p = SimpleNamespace(schema=SimpleNamespace(entities=['sensor.a'], VERSION=12),
                            model_revision='one', VERSION=12, dims=32,
                            agent={'target_property':'power'}, features=Mock(return_value=self.observation))
        for builder in (teaching.signature, observation_contract.teaching_signature):
            expected = builder(p, self.states, self.temporal, 10.)
            p.features.reset_mock()
            with decision_observation(p, self.states, self.temporal, 10., self.observation):
                actual = builder(p, self.states, self.temporal, 10.)
                self.assertEqual(actual, expected)
                if actual is not None:
                    actual['changed'] = 1
                    self.assertNotIn('changed', builder(p, self.states, self.temporal, 10.))
            p.features.assert_not_called()


class CommunicationTests(unittest.TestCase):
    def test_cutoff_replacement_missing_and_new_frame_are_fresh(self):
        current = {'attributes': {'__hm_received_time': 8}}
        history = SimpleNamespace(samples={'sensor.a': [(7., current), (12., {'attributes': {'__hm_received_time':12}})]})
        shared = shared_inference_temporal(history, 10.)
        self.assertEqual(_latest_communication(shared, 'sensor.a', 10., current), 8.)
        self.assertEqual(_latest_communication(shared, 'sensor.a', 10., current), 8.)
        self.assertEqual(shared.communication_hits, 1)
        replacement = {'attributes': {'__hm_received_time': 9}}
        self.assertEqual(_latest_communication(shared, 'sensor.a', 10., replacement), 9.)
        self.assertEqual(_latest_communication(shared, 'sensor.missing', 10.), None)
        self.assertEqual(_latest_communication(shared, 'sensor.a', 12., current), 12.)
        history.samples['sensor.a'].append((9., replacement))
        fresh = shared_inference_temporal(history, 10.)
        self.assertEqual(_latest_communication(fresh, 'sensor.a', 10., current), 9.)


class ActionLatencyTests(unittest.TestCase):
    def setUp(self):
        self.runtime = {}
        self.engine = SimpleNamespace(_inference_tls=SimpleNamespace(event_received_perf={'sensor.a':1.,'sensor.b':1.05}))
        self.observe = patch.object(action_latency.TELEMETRY, 'observe').start()
        self.addCleanup(patch.stopall)

    def test_dispatch_response_and_real_state_ack_are_separate(self):
        with patch.object(action_latency.time, 'perf_counter', side_effect=[1.1, 1.2, 1.3]):
            started = action_latency.service_started(self.engine, self.runtime, 100.)
            action_latency.service_returned(self.runtime, started, True)
            action_latency.acknowledged(self.runtime, {'started_ts':100., 'no_service':False})
        values = dict(self.observe.call_args_list[i].args for i in range(self.observe.call_count))
        for key,value in [('event_to_service_dispatch',100),('ha_service_request',100),
                          ('event_to_service_return',200),('service_to_state_ack',200),('event_to_state_ack',300)]:
            self.assertAlmostEqual(values[key], value)
        self.assertNotIn('_service_timing', self.runtime)

    def test_timer_has_no_sensor_latency_and_failed_service_has_no_ack(self):
        self.engine._inference_tls.event_received_perf = {}
        started = action_latency.service_started(self.engine, self.runtime, 100.)
        action_latency.service_returned(self.runtime, started, False)
        action_latency.acknowledged(self.runtime, {'started_ts':100.})
        self.assertEqual([x.args[0] for x in self.observe.call_args_list], ['ha_service_request'])

    def test_shadow_and_stale_pending_never_fabricate_ack(self):
        for pending in ({'started_ts':100.,'no_service':True},{'started_ts':99.}):
            self.runtime['_service_timing'] = {'started_ts':100.,'started_perf':1.,'received_perf':.9}
            action_latency.acknowledged(self.runtime, pending)
        self.observe.assert_not_called()


class RetryAdmissionTests(unittest.TestCase):
    def test_ready_retry_does_not_wait_an_extra_debounce(self):
        from test_release_172_target_retries import TargetRetries172Tests, A, SHARED
        import engine as engine_module
        fixture = TargetRetries172Tests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        r = fixture.real_engine()
        r.state_map = dict(fixture.r.state_map)
        r.ready_target_changes[A] = {SHARED}
        r.inference_enabled.set()
        r.startup_inference_not_before = 0.
        r.initial_inference_pending = False
        r.refresh_states = Mock()
        r.wake_event.wait = Mock(return_value=True)
        r.stop_event.wait = Mock()
        r._startup_warmup_step = Mock(return_value=0)
        r._schedule_housekeeping = Mock()
        r._maybe_schedule_state_resync = Mock()
        r.process = Mock(side_effect=lambda *a,**kw: r.stop_event.set())
        # The independent trend sampler also waits on the lifecycle stop event.
        # Isolate it so this assertion observes scheduler debounce only.
        with patch.dict(engine_module.OPTIONS, {'realtime_inference_debounce_ms':250}), patch.object(engine_module.TELEMETRY, 'start_trend_sampling') as sampler:
            r.run()
        sampler.assert_called_once_with(r.stop_event)
        r.process.assert_called_once()
        r.stop_event.wait.assert_not_called()


class EngineObservationIntegrationTests(unittest.TestCase):
    def test_actual_engine_composer_uses_one_feature_pass_and_causal_clock(self):
        from test_release_118_event_decision_hotpath import EventDecisionHotpath118Tests
        import time
        fixture = EventDecisionHotpath118Tests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        engine = fixture.engine
        agent = fixture._qualified_shadow_agent()
        engine.state_map = {agent['target_entity']: support.state(agent['target_entity'], 'off'),
                            'binary_sensor.motion': support.state('binary_sensor.motion', 'on')}
        engine.context.configure(engine.state_map)
        policy = engine.policy(agent)
        observations = []
        def compose(**kw):
            result = signature_features(kw['policy'],kw['state_map'],kw['temporal'],kw['timestamp'])
            self.assertIs(result[0], kw['features'])
            self.assertIs(result[1], kw['labels'])
            self.assertEqual(kw['timestamp'], engine.runtime[agent['id']]['last_inference_ts'])
            observations.append(result)
            return dict(chosen=kw['chosen'], confidence=kw['confidence'], arms=kw['arms'],
                        horizon=kw['horizon'],support=kw['support'],novelty=kw['novelty'],
                        baseline_value=kw['chosen']['value'],teaching=None,preference=None,
                        instruction=None,trial=None,source='historical_policy_bootstrap')
        engine.decision_composer = SimpleNamespace(compose=compose)
        with patch.object(policy, 'features', wraps=policy.features) as features, \
             patch.object(engine.executor, 'submit', return_value={'status':'SHADOW'}) as submit:
            engine.process_target([agent], {'binary_sensor.motion'},
                                  event_received_perf={'binary_sensor.motion':time.perf_counter()})
        self.assertEqual(len(observations),1)
        self.assertEqual(features.call_count,1)
        submit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
