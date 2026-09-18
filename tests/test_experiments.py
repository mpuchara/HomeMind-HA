import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import *
from experiments import Experiments, activity_scalar
from storage import Store
import test_executor as executor_fixture


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'test.db')
        self.now = 100000.
        self.e = Experiments(self.store, clock=lambda: self.now, rng=Mock(random=lambda: .9))
        self.a = agent()
        self.states = {'light.kitchen': state('light.kitchen'),
            'sensor.radar_presence': state('sensor.radar_presence', 10, unit_of_measurement='%'),
            'binary_sensor.pir': state('binary_sensor.pir', 'off', device_class='motion'),
            'sensor.lux': state('sensor.lux', 200, device_class='illuminance', unit_of_measurement='lx')}
        self.features = {0:1, 5:.1, 6:-1., 7:.6}
        self.labels = {0:['bias'], 5:['sensor.radar_presence:value'],
                       6:['binary_sensor.pir:value'], 7:['sensor.lux:value']}
        head = SimpleNamespace(a=[[1.]*16 for _ in range(2)], b=[[0.]*16 for _ in range(2)])
        head.b[1][5], head.b[1][7] = 1., -1.
        self.policy = SimpleNamespace(heads={1:head}, lock=threading.RLock(), VERSION=10, model_revision='test')
        self.arms = [dict(index=0, value=0., mean=.5, support=.8, novelty=.1),
                     dict(index=1, value=1., mean=.48, support=.8, novelty=.1)]
        self.e.configure(self.a, dict(enabled=True, interval=300, observation_seconds=10))

    def tearDown(self):
        self.temp.cleanup()

    def propose(self, **kwargs):
        registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            'binary_sensor.pir': {'area_id': 'kitchen'},
        }
        defaults = dict(agent=self.a, policy=self.policy, states=self.states, registry=registry, features=self.features,
            labels=self.labels, chosen=self.arms[0], confidence=.9, arms=self.arms, horizon=1, rt={})
        return self.e.propose(**(defaults | kwargs))

    def start(self, **kwargs):
        trial = self.propose(**kwargs)
        self.assertIsNotNone(trial)
        intent = SimpleNamespace(experiment_token=trial['token'], desired_value=trial['value'], model_revision='test')
        self.assertTrue(self.e.begin(self.a, intent, self.states))
        self.e.dispatched(self.a, intent, self.states)
        return trial

    def ack(self):
        self.states['light.kitchen']['state'] = 'on'
        self.e.observe(self.a, self.states, 1.)

    def test_presence_perturbs_only_presence_boundary(self):
        trial = self.propose()
        self.assertEqual(trial['value'], 1.)
        self.assertIn('sensor.radar_presence:value', trial['x'])
        self.assertNotIn('sensor.lux:value', trial['x'])
        self.assertEqual(self.states['light.kitchen']['state'], 'off')

    def test_environment_can_try_on_in_brighter_context(self):
        self.e.configure(self.a, dict(focus='environment'))
        trial = self.propose()
        self.assertEqual(trial['value'], 1.)
        self.assertIn('sensor.lux:value', trial['x'])
        self.assertNotIn('sensor.radar_presence:value', trial['x'])
        self.assertEqual(self.states['sensor.lux']['state'], '200')

    def test_no_presence_signal_or_far_boundary_does_not_probe(self):
        self.assertIsNone(self.propose(features={0:1, 5:0, 6:-1, 7:.6}))
        arms = copy.deepcopy(self.arms); arms[1]['mean'] = -1
        self.assertIsNone(self.propose(arms=arms))

    def test_baseline_action_and_existing_pending_take_priority(self):
        self.assertIsNone(self.propose(chosen=self.arms[1]))
        self.assertIsNone(self.propose(rt={'pending': {'action_value': 0}}))
        self.assertIsNone(self.propose(confidence=.5))
        self.assertIsNone(self.propose(rt={'manual_override_until': self.now+50}))

    def test_disabled_shadow_and_old_micro_flag_never_start_trials(self):
        self.assertIsNone(self.propose(agent=self.a | {'mode':'shadow'}))
        self.e.configure(self.a, dict(enabled=False))
        self.assertIsNone(self.propose(agent=self.a | {'micro_exploration':True}))
        self.assertEqual(self.e.status(self.a['id'])['trials_today'], 0)

    def test_actual_ack_is_not_reward(self):
        self.start(); self.ack()
        self.assertEqual(self.e.status(self.a['id'])['counts'], [0,0,0])
        self.now += 10
        self.e.observe(self.a, self.states, 1.)
        result = self.e.status(self.a['id'])
        self.assertEqual(result['counts'], [0,1,0])
        self.assertEqual(result['last_outcome']['reward'], .02)

    def test_manual_correction_updates_bandit_and_survives_restart(self):
        trial = self.start(); self.ack()
        before = self.e._score(self.e._learner(self.e._get(self.a['id'])), 1, trial['x'])
        self.now += 2
        self.e.observe(self.a, self.states, 0., manual=True)
        after = self.e._score(self.e._learner(self.e._get(self.a['id'])), 1, trial['x'])
        self.assertLess(after, before)
        fresh = Experiments(self.store, clock=lambda:self.now)
        result = fresh.status(self.a['id'])
        self.assertEqual(result['counts'], [0,1,0])
        self.assertEqual(result['last_outcome']['reward'], -1.)
        self.assertGreaterEqual(result['next_trial_after'], self.now+3600)

    def test_reference_control_and_adaptive_selection(self):
        self.e.rng = Mock(random=lambda:0.)
        self.assertIsNone(self.propose())
        self.now += 10
        self.e.observe(self.a, self.states, 0.)
        result = self.e.status(self.a['id'])
        self.assertEqual(result['counts'], [1,0,0])
        self.assertEqual(result['last_outcome']['reward'], .05)

    def test_negative_reward_changes_next_choice_to_reference(self):
        self.start(); self.ack()
        self.e.observe(self.a, self.states, 0, manual=True)
        self.now += 3601
        self.states['light.kitchen']['state'] = 'off'
        self.assertIsNone(self.propose())
        self.assertEqual(self.e.status(self.a['id'])['active']['kind'], 'reference')

    def test_presence_confirmation_rewards_preemptive_action(self):
        self.start(); self.ack()
        self.states['binary_sensor.pir']['state'] = 'on'
        self.e.observe(self.a, self.states, 1.)
        self.assertEqual(self.e.status(self.a['id'])['last_outcome']['reward'], .6)

    def test_context_change_releases_trial_without_inventing_label(self):
        self.e.configure(self.a, dict(focus='environment'))
        self.start(); self.ack()
        self.states['sensor.lux']['state'] = '0'
        self.e.observe(self.a, self.states, 1.)
        result = self.e.status(self.a['id'])
        self.assertIsNone(result['active'])
        self.assertIsNone(result['last_outcome']['reward'])

    def test_missing_ack_and_restart_do_not_learn_acceptance(self):
        self.start()
        self.now += 11
        self.e.observe(self.a, self.states, 0.)
        self.assertEqual(self.e.status(self.a['id'])['counts'], [0,0,0])
        self.now += 301; self.start()
        fresh = Experiments(self.store, clock=lambda:self.now)
        result = fresh.status(self.a['id'])
        self.assertIsNone(result['active'])
        self.assertEqual(result['counts'], [0,0,0])
        self.assertEqual(result['trials_today'], 2)

    def test_budget_and_interval_persist(self):
        self.e.configure(self.a, dict(daily_budget=1))
        self.start(); self.e.cancel(self.a['id'], 'test transport failure')
        self.now += 301
        self.assertIsNone(self.propose())
        self.e = Experiments(self.store, clock=lambda:self.now)
        self.assertIsNone(self.propose())

    def test_config_revision_revokes_prepared_command(self):
        trial = self.propose()
        intent = SimpleNamespace(experiment_token=trial['token'], desired_value=1., model_revision='test')
        self.assertTrue(self.e.valid(self.a, intent))
        self.e.configure(self.a, dict(enabled=False))
        self.assertFalse(self.e.valid(self.a, intent))

    def test_two_concurrent_candidates_cannot_both_reserve(self):
        first = self.propose()
        other = self.a | dict(id='second', target_entity='light.second')
        self.states['light.second'] = state('light.second')
        self.e.configure(other, dict(enabled=True))
        second = self.propose(agent=other)
        def intent(t):
            return SimpleNamespace(experiment_token=t['token'], desired_value=t['value'], model_revision='test')
        self.assertTrue(self.e.begin(self.a, intent(first)))
        self.assertFalse(self.e.begin(other, intent(second)))

    def test_devices_scan_excludes_self_and_has_named_bounded_features(self):
        self.e.configure(self.a, dict(focus='devices'))
        for n in range(50):
            self.states[f'switch.other_{n:02}'] = state(f'switch.other_{n:02}', 'on')
        registry = {'light.kitchen':{'device_id':'own'}, 'switch.other_00':{'device_id':'own'}}
        old_features, old_labels = dict(self.features), copy.deepcopy(self.labels)
        trial = self.propose(registry=registry)
        self.assertEqual(len(trial['snapshot']), 32)
        self.assertNotIn('light.kitchen', trial['snapshot'])
        self.assertNotIn('switch.other_00', trial['snapshot'])
        self.assertIn('device:switch.other_01', trial['x'])
        self.assertEqual(self.features, old_features)
        self.assertEqual(self.labels, old_labels)

    def test_hvac_activity_not_just_thermostat_mode(self):
        self.assertEqual(activity_scalar(state('climate.room', 'heat', hvac_action='idle')), -1.)
        self.assertEqual(activity_scalar(state('climate.room', 'heat', hvac_action='heating')), 1.)

    def test_slow_devices_wait_for_physical_settling(self):
        self.a = self.a | dict(target_entity='climate.room', target_property='temperature',
                             min_value=16., max_value=28., deadband=.1)
        self.states['climate.room'] = state('climate.room', 'heat', temperature=20., min_temp=16., max_temp=28., target_temp_step=.5)
        self.arms[0]['value'], self.arms[1]['value'] = 20., 21.
        self.e.configure(self.a, dict(focus='environment', max_step=.5))
        trial = self.start()
        self.assertEqual(trial['value'], 20.5)
        self.assertEqual(trial['window'], 900.)
        self.e.observe(self.a, self.states, 20.5)
        self.now += 10
        self.e.observe(self.a, self.states, 20.5)
        self.assertEqual(self.e.status(self.a['id'])['counts'], [0,0,0])
        self.now += 60
        self.policy.model_revision = 'ordinary-age-decay'
        held = self.propose()
        self.assertEqual(held['model_revision'], 'ordinary-age-decay')
        self.assertIsNotNone(self.e.status(self.a['id'])['active'])

    def test_device_features_influence_learned_contextual_choice(self):
        self.e.configure(self.a, dict(focus='devices'))
        self.states['switch.other'] = state('switch.other', 'on')
        trial = self.start(); self.ack()
        self.e.observe(self.a, self.states, 0, manual=True)
        status = self.e.status(self.a['id'])
        influence = next(x for x in status['device_influences'] if x['entity_id'] == 'switch.other')
        self.assertLess(influence['contribution'], 0)
        learner = self.e._learner(self.e._get(self.a['id']))
        on = dict(trial['x']); off = dict(on, **{'device:switch.other':-1})
        self.assertLess(self.e._score(learner, 1, on), self.e._score(learner, 1, off))

    def test_no_experimental_power_off_and_numeric_budget_respects_current(self):
        self.states['light.kitchen']['state'] = 'on'
        self.assertIsNone(self.propose(chosen=self.arms[1]))
        self.a = self.a | dict(target_property='brightness_pct', min_value=0., max_value=100., deadband=3.)
        self.states['light.kitchen'] = state('light.kitchen', 'on', brightness=27*255/100)
        self.arms[0]['value'], self.arms[1]['value'] = 30., 33.333
        self.e.configure(self.a, dict(max_step=5))
        self.assertIsNone(self.propose())  # baseline +/- deadband cannot expand the trial step

    def test_invalid_configuration_is_rejected_without_partial_write(self):
        before = self.e.status(self.a['id'])['revision']
        for payload in ({'enabled':'true'}, {'focus':[]}, {'focus':'random'}, {'max_step':float('nan')},
                        {'interval':1}, {'daily_budget':1.5}, {'daily_budget':1000}, {'unknown':True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.e.configure(self.a, payload)
        self.assertEqual(self.e.status(self.a['id'])['revision'], before)


class ExperimentIntegrationTests(unittest.TestCase):
    setUp = executor_fixture.ExecutorTests.setUp
    tearDown = executor_fixture.ExecutorTests.tearDown
    intent = executor_fixture.ExecutorTests.intent

    def test_forged_experiment_token_never_bypasses_executor(self):
        result = self.e.executor.submit(self.intent(experiment_token='not-prepared'), {0:1}, 1)
        self.assertEqual(result['status'], 'REJECTED')
        self.assertIn('experiment:', result['reason'])
        self.service.assert_not_called()

    def test_configuration_http_does_not_send_services_or_retrain(self):
        import main
        handler = main.Handler.__new__(main.Handler)
        handler.path = '/api/agents/'+self.a['id']+'/experiments'
        handler.read_json = lambda: dict(enabled=True, focus='environment')
        handler.send_json = Mock()
        with patch.object(main, 'STORE', self.store), patch.object(main, 'ENGINE', self.e), \
             patch.object(main, 'runtime_available', return_value=True), \
             patch.object(main, 'startup_snapshot', return_value={'ready':True}):
            handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], 200)
        self.assertTrue(self.e.experiments.status(self.a['id'])['config']['enabled'])
        self.assertEqual(self.store.get_agent_config(self.a['id'])['training_state'], 'qualified')
        self.service.assert_not_called()

    def prepare_real_engine(self):
        self.e.experiments.configure(self.a, dict(enabled=True, observation_seconds=10))
        self.e.experiments.rng = Mock(random=lambda:.9)
        self.e.state_map['sensor.radar_presence'] = state('sensor.radar_presence', 10, unit_of_measurement='%')
        self.model.schema.entities = ['sensor.radar_presence']
        h = min(self.model.horizons)
        self.model.heads[h].b[1][5] = 100
        chosen = dict(index=0, value=0., mean=.5, uncertainty=.1, support=.8, novelty=.1)
        arms = [chosen, dict(chosen, index=1, value=1., mean=.48)]
        return patch.object(self.model, 'predict', return_value=(chosen, .9, arms, h, .8, .1))

    def test_end_to_end_probe_ack_hold_and_manual_feedback(self):
        from fast_runtime import install
        import engine as engine_module
        core = SimpleNamespace(STORE=self.store, ENGINE=self.e, default_action_interval=lambda *args:1, runtime_available=lambda:True)
        with patch('history.default_action_interval', lambda *args:1):
            install(core)
        self.a = self.store.get_agent_config(self.a['id'])
        with self.prepare_real_engine():
            result = self.e.process_agent(self.a, self.e.state_map, {'sensor.radar_presence'})
            self.assertEqual(result['status'], 'ACCEPTED')
            self.service.assert_called_once_with('light', 'turn_on', {'entity_id':'light.kitchen'})
            self.e.state_map['light.kitchen'] = state('light.kitchen', 'on')
            result = self.e.process_agent(self.a, self.e.state_map, {'light.kitchen'})
            self.assertEqual(result['status'], 'REJECTED')  # correct state; no repeat command
            self.assertEqual(self.service.call_count, 1)
            self.assertEqual(self.e.experiments.status(self.a['id'])['counts'], [0,0,0])
            self.e.state_map['light.kitchen'] = state('light.kitchen', 'off') | {'context':{'user_id':'human', 'id':'manual'}}
            with patch.object(self.e, 'set_manual_hold', wraps=self.e.set_manual_hold):
                self.e.process_agent(self.a, self.e.state_map, {'light.kitchen'})
            outcome = self.e.experiments.status(self.a['id'])
            self.assertEqual(outcome['last_outcome']['reward'], -1.)
            self.assertEqual(self.service.call_count, 1)
            self.assertEqual(self.e.runtime[self.a['id']]['manual_override_until'], 0.)
            self.assertGreater(outcome['next_trial_after'], engine_module.now_ts()+3500)

    def test_trial_transport_failure_is_unlabelled_and_consumes_budget(self):
        with self.prepare_real_engine():
            self.service.side_effect = RuntimeError('lost response')
            result = self.e.process_agent(self.a, self.e.state_map, {'sensor.radar_presence'})
        self.assertEqual(result['status'], 'REJECTED')
        status = self.e.experiments.status(self.a['id'])
        self.assertIsNone(status['active'])
        self.assertEqual(status['counts'], [0,0,0])
        self.assertEqual(status['trials_today'], 1)
