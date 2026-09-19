import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import test_experiments as experiment_fixture


class PresenceOutcomeAttributionTests(unittest.TestCase):
    def setUp(self):
        self.f = experiment_fixture.ExperimentTests()
        self.f.setUp()
        self.registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            'binary_sensor.pir': {'area_id': 'kitchen'},
        }

    def tearDown(self):
        self.f.tearDown()

    def test_trial_separates_prediction_background_and_outcome_roles(self):
        f = self.f
        f.states['binary_sensor.helper'] = {
            'entity_id': 'binary_sensor.helper', 'state': 'off', 'attributes': {},
        }
        f.policy.schema = SimpleNamespace(entities=['binary_sensor.helper'])

        trial = f.propose(registry=self.registry)

        self.assertIn('binary_sensor.pir', trial['prediction_inputs'])
        self.assertIn('sensor.radar_presence', trial['prediction_inputs'])
        self.assertIn('binary_sensor.helper', trial['background_dependencies'])
        self.assertIn('binary_sensor.pir', trial['outcome_sources'])
        self.assertNotIn('sensor.radar_presence', trial['outcome_sources'])
        self.assertNotIn('binary_sensor.helper', trial['outcome_sources'])
        self.assertEqual(trial['outcome_sources']['binary_sensor.pir']['area_id'], 'kitchen')
        self.assertEqual(trial['outcome_sources']['binary_sensor.pir']['role'], 'binary')
        self.assertEqual(trial['outcome_sources']['binary_sensor.pir']['verified_role'], 'motion')

    def test_kitchen_presence_confirms_kitchen_probe_and_is_bound_to_trial_window(self):
        f = self.f
        f.start(registry=self.registry)
        active = f.e.status(f.a['id'])['active']
        self.assertIsNotNone(active['trial_id'])
        self.assertIsNotNone(active['action_at'])
        self.assertEqual(active['observation_start'], active['action_at'])
        self.assertGreater(active['observation_end'], active['observation_start'])

        f.ack()
        f.now += 1
        f.states['binary_sensor.pir']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)
        outcome = f.e.status(f.a['id'])['last_outcome']

        self.assertEqual(outcome['reward'], .6)
        self.assertEqual(outcome['trial_id'], active['trial_id'])
        self.assertEqual(outcome['action_at'], active['action_at'])
        self.assertLessEqual(outcome['observation_start'], outcome['at'])
        self.assertLessEqual(outcome['at'], outcome['observation_end'])

    def test_other_room_presence_is_predictor_but_not_kitchen_outcome_source(self):
        f = self.f
        f.states['binary_sensor.hall'] = {
            'entity_id': 'binary_sensor.hall', 'state': 'off',
            'attributes': {'device_class': 'motion'},
        }
        labels = dict(f.labels)
        labels[8] = ['binary_sensor.hall:value']
        features = {**f.features, 8: -1.0}
        registry = self.registry | {'binary_sensor.hall': {'area_id': 'hall'}}

        trial = f.start(labels=labels, features=features, registry=registry)
        self.assertIn('binary_sensor.hall', trial['prediction_inputs'])
        self.assertNotIn('binary_sensor.hall', trial['outcome_sources'])
        f.ack()
        f.states['binary_sensor.hall']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)

        outcome = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(outcome['reward'])
        self.assertEqual(outcome['reason'], 'context changed; ordinary control resumes')

    def test_plain_binary_helper_cannot_confirm_presence(self):
        f = self.f
        f.states['binary_sensor.helper'] = {
            'entity_id': 'binary_sensor.helper', 'state': 'off', 'attributes': {},
        }
        f.policy.schema = SimpleNamespace(entities=['binary_sensor.helper'])

        trial = f.start(registry=self.registry)
        self.assertIn('binary_sensor.helper', trial['background_dependencies'])
        self.assertNotIn('binary_sensor.helper', trial['outcome_sources'])
        f.ack()
        f.states['binary_sensor.helper']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)

        outcome = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(outcome['reward'])

    def test_name_only_presence_helper_can_predict_but_cannot_confirm(self):
        f = self.f
        f.states['binary_sensor.presence_helper'] = {
            'entity_id': 'binary_sensor.presence_helper', 'state': 'off',
            'attributes': {'friendly_name': 'Presence helper'},
        }
        labels = dict(f.labels)
        labels[8] = ['binary_sensor.presence_helper:value']
        features = {**f.features, 8: -1.0}
        f.policy.heads[1].b[1][8] = 1.0
        registry = self.registry | {
            'binary_sensor.presence_helper': {'area_id': 'kitchen', 'platform': 'template'},
        }

        trial = f.start(labels=labels, features=features, registry=registry)
        self.assertIn('binary_sensor.presence_helper', trial['prediction_inputs'])
        self.assertNotIn('binary_sensor.presence_helper', trial['outcome_sources'])
        f.ack()
        f.now += 1
        f.states['binary_sensor.presence_helper']['state'] = 'on'
        f.states['binary_sensor.presence_helper']['last_changed'] = f.now
        f.e.observe(f.a, f.states, 1.)

        outcome = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(outcome['reward'])
        self.assertEqual(outcome['reason'], 'context changed; ordinary control resumes')

    def test_change_before_trial_is_rebased_and_never_confirms_arrival(self):
        f = self.f
        trial = f.propose(registry=self.registry)
        self.assertEqual(trial['outcome_sources']['binary_sensor.pir']['before'], -1.0)

        f.states['binary_sensor.pir']['state'] = 'on'
        intent = SimpleNamespace(
            experiment_token=trial['token'], desired_value=trial['value'], model_revision='test')
        self.assertTrue(f.e.begin(f.a, intent, f.states))
        f.e.dispatched(f.a, intent, f.states)
        f.ack()

        outcome = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNotNone(outcome)
        self.assertIsNone(outcome['reward'])
        self.assertNotEqual(outcome['reason'], 'presence confirmed after decision')

    def test_missing_area_mapping_stays_unknown_and_does_not_get_weak_reward(self):
        f = self.f
        trial = f.start(registry={})
        self.assertEqual(trial['outcome_sources'], {})
        f.ack()
        f.now += trial['window']
        f.e.observe(f.a, f.states, 1.)

        outcome = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(outcome['reward'])
        self.assertEqual(outcome['reason'], 'presence outcome unobservable: no verified local source')

    def test_unavailable_local_outcome_source_never_rewards(self):
        f = self.f
        f.start(registry=self.registry)
        f.ack()
        f.states['binary_sensor.pir']['state'] = 'unavailable'
        f.e.observe(f.a, f.states, 1.)

        outcome = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(outcome['reward'])
        self.assertEqual(outcome['reason'], 'context unavailable')

    def _simultaneous_arrival(self, hall_first):
        f = experiment_fixture.ExperimentTests()
        f.setUp()
        try:
            f.states['binary_sensor.hall'] = {
                'entity_id': 'binary_sensor.hall', 'state': 'off',
                'attributes': {'device_class': 'motion'},
            }
            base = [(6, ['binary_sensor.pir:value']), (8, ['binary_sensor.hall:value'])]
            if hall_first:
                base.reverse()
            labels = {0: ['bias'], 5: ['sensor.radar_presence:value'],
                      **{index: value for index, value in base}, 7: ['sensor.lux:value']}
            features = {**f.features, 8: -1.0}
            registry = {
                'light.kitchen': {'area_id': 'kitchen'},
                'binary_sensor.pir': {'area_id': 'kitchen'},
                'binary_sensor.hall': {'area_id': 'hall'},
            }
            f.start(labels=labels, features=features, registry=registry)
            f.ack()
            f.now += 1
            f.states['binary_sensor.pir']['state'] = 'on'
            f.states['binary_sensor.hall']['state'] = 'on'
            f.e.observe(f.a, f.states, 1.)
            return f.e.status(f.a['id'])['last_outcome']['reward']
        finally:
            f.tearDown()

    def test_entity_order_does_not_change_reward(self):
        self.assertEqual(self._simultaneous_arrival(False), .6)
        self.assertEqual(self._simultaneous_arrival(True), .6)

    def test_reference_arrival_without_comfort_loss_is_not_a_penalty(self):
        f = self.f
        f.e.rng = Mock(random=lambda: 0.)
        self.assertIsNone(f.propose(registry=self.registry))
        active = f.e.status(f.a['id'])['active']
        self.assertEqual(active['kind'], 'reference')

        f.now += 1
        f.states['binary_sensor.pir']['state'] = 'on'
        f.e.observe(f.a, f.states, 0.)
        outcome = f.e.status(f.a['id'])['last_outcome']

        self.assertIsNone(outcome['reward'])
        self.assertEqual(outcome['reason'],
                         'reference arrival observed; no comfort-loss outcome defined')

    def test_outcome_is_settled_once(self):
        f = self.f
        f.start(registry=self.registry)
        f.ack()
        f.now += 1
        f.states['binary_sensor.pir']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)
        first = f.e.status(f.a['id'])
        self.assertEqual(first['counts'], [0, 1, 0])

        f.now += 1
        f.e.observe(f.a, f.states, 1.)
        second = f.e.status(f.a['id'])
        self.assertEqual(second['counts'], [0, 1, 0])
        self.assertEqual(second['last_outcome'], first['last_outcome'])


if __name__ == '__main__':
    unittest.main()
