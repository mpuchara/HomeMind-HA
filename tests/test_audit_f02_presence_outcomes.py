import unittest
from unittest.mock import Mock

import test_experiments as experiment_fixture
from support import state


class PresenceOutcomeAttributionContractTests(unittest.TestCase):
    """F02 regressions: prediction context is not automatically outcome evidence."""

    def setUp(self):
        self.fixture = experiment_fixture.ExperimentTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_local_kitchen_presence_confirms_kitchen_trial(self):
        f = self.fixture
        trial = f.start()
        self.assertIn('binary_sensor.pir', trial['prediction_inputs'])
        self.assertIn('binary_sensor.pir', trial['outcome_sources'])
        self.assertEqual(trial['outcome_sources']['binary_sensor.pir']['role'], 'binary:motion')

        f.ack()
        f.now += 1
        f.states['binary_sensor.pir']['state'] = 'on'
        f.states['binary_sensor.pir']['last_changed'] = f.now
        f.e.observe(f.a, f.states, 1.0)

        result = f.e.status(f.a['id'])['last_outcome']
        self.assertEqual(result['trial_id'], trial['trial_id'])
        self.assertEqual(result['reward'], 0.6)
        self.assertEqual(result['reason'], 'presence confirmed after decision')

    def test_other_area_presence_can_predict_but_cannot_confirm_target_area(self):
        f = self.fixture
        f.states['binary_sensor.hall_motion'] = state(
            'binary_sensor.hall_motion', 'off', device_class='motion'
        )
        features = dict(f.features)
        features[8] = -1.0
        labels = dict(f.labels)
        labels[8] = ['binary_sensor.hall_motion:value']
        f.policy.heads[1].b[1][8] = 1.0
        registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            'binary_sensor.pir': {'area_id': 'kitchen'},
            'binary_sensor.hall_motion': {'area_id': 'hall'},
        }

        trial = f.start(features=features, labels=labels, registry=registry)
        self.assertIn('binary_sensor.hall_motion', trial['prediction_inputs'])
        self.assertNotIn('binary_sensor.hall_motion', trial['outcome_sources'])
        f.ack()

        f.now += 1
        f.states['binary_sensor.hall_motion']['state'] = 'on'
        f.states['binary_sensor.hall_motion']['last_changed'] = f.now
        f.e.observe(f.a, f.states, 1.0)

        result = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(result['reward'])
        self.assertEqual(result['reason'], 'context changed; ordinary control resumes')

    def test_name_only_binary_helper_is_not_verified_outcome_source(self):
        f = self.fixture
        f.states['binary_sensor.presence_helper'] = state(
            'binary_sensor.presence_helper', 'off', friendly_name='Presence helper'
        )
        features = dict(f.features)
        features[8] = -1.0
        labels = dict(f.labels)
        labels[8] = ['binary_sensor.presence_helper:value']
        f.policy.heads[1].b[1][8] = 1.0
        registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            'binary_sensor.pir': {'area_id': 'kitchen'},
            'binary_sensor.presence_helper': {'area_id': 'kitchen', 'platform': 'template'},
        }

        trial = f.start(features=features, labels=labels, registry=registry)
        self.assertIn('binary_sensor.presence_helper', trial['prediction_inputs'])
        self.assertNotIn('binary_sensor.presence_helper', trial['outcome_sources'])
        f.ack()

        f.now += 1
        f.states['binary_sensor.presence_helper']['state'] = 'on'
        f.states['binary_sensor.presence_helper']['last_changed'] = f.now
        f.e.observe(f.a, f.states, 1.0)

        result = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(result['reward'])
        self.assertEqual(result['reason'], 'context changed; ordinary control resumes')

    def test_transition_timestamp_before_action_cannot_confirm_trial(self):
        f = self.fixture
        trial = f.start()
        f.ack()
        active = f.e._get(f.a['id'])['active']
        self.assertIsNotNone(active['action_at'])
        self.assertIsNotNone(active['observation_start'])

        f.now += 1
        f.states['binary_sensor.pir']['state'] = 'on'
        f.states['binary_sensor.pir']['last_changed'] = active['action_at'] - 0.5
        f.e.observe(f.a, f.states, 1.0)

        result = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(result['reward'])
        self.assertEqual(result['trial_id'], trial['trial_id'])
        self.assertEqual(result['reason'], 'presence transition outside observable trial window')

    def test_missing_area_or_unavailable_source_never_creates_positive_reward(self):
        f = self.fixture
        registry = {
            'light.kitchen': {},
            'binary_sensor.pir': {'area_id': 'kitchen'},
        }
        trial = f.start(registry=registry)
        self.assertEqual(trial['outcome_sources'], {})
        f.ack()
        f.now += 10
        f.e.observe(f.a, f.states, 1.0)
        result = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(result['reward'])
        self.assertEqual(result['reason'], 'presence outcome unobservable: no verified local source')

        f.now += 301
        f.states['light.kitchen']['state'] = 'off'
        trial2 = f.start()
        f.ack()
        f.states['binary_sensor.pir']['state'] = 'unavailable'
        f.e.observe(f.a, f.states, 1.0)
        result2 = f.e.status(f.a['id'])['last_outcome']
        self.assertIsNone(result2['reward'])
        self.assertEqual(result2['trial_id'], trial2['trial_id'])
        self.assertEqual(result2['reason'], 'context unavailable')

    def test_batch_entity_order_does_not_change_presence_reward(self):
        rewards = []
        for reverse in (False, True):
            f = experiment_fixture.ExperimentTests()
            f.setUp()
            try:
                f.states['binary_sensor.hall_motion'] = state(
                    'binary_sensor.hall_motion', 'off', device_class='motion'
                )
                features = dict(f.features)
                features[8] = -1.0
                if reverse:
                    labels = {
                        0: ['bias'],
                        8: ['binary_sensor.hall_motion:value'],
                        6: ['binary_sensor.pir:value'],
                        5: ['sensor.radar_presence:value'],
                        7: ['sensor.lux:value'],
                    }
                else:
                    labels = {
                        0: ['bias'],
                        6: ['binary_sensor.pir:value'],
                        8: ['binary_sensor.hall_motion:value'],
                        5: ['sensor.radar_presence:value'],
                        7: ['sensor.lux:value'],
                    }
                f.policy.heads[1].b[1][8] = 1.0
                registry = {
                    'light.kitchen': {'area_id': 'kitchen'},
                    'binary_sensor.pir': {'area_id': 'kitchen'},
                    'binary_sensor.hall_motion': {'area_id': 'hall'},
                }
                f.start(features=features, labels=labels, registry=registry)
                f.ack()
                f.now += 1
                for eid in ('binary_sensor.pir', 'binary_sensor.hall_motion'):
                    f.states[eid]['state'] = 'on'
                    f.states[eid]['last_changed'] = f.now
                f.e.observe(f.a, f.states, 1.0)
                rewards.append(f.e.status(f.a['id'])['last_outcome']['reward'])
            finally:
                f.tearDown()

        self.assertEqual(rewards, [0.6, 0.6])

    def test_presence_outcome_is_settled_exactly_once(self):
        f = self.fixture
        trial = f.start()
        f.ack()
        f.now += 1
        f.states['binary_sensor.pir']['state'] = 'on'
        f.states['binary_sensor.pir']['last_changed'] = f.now
        f.e.observe(f.a, f.states, 1.0)

        first = f.e.status(f.a['id'])
        first_counts = list(first['counts'])
        first_rewards = list(first['rewards'])
        self.assertEqual(first['last_outcome']['trial_id'], trial['trial_id'])

        f.now += 1
        f.e.observe(f.a, f.states, 1.0)
        second = f.e.status(f.a['id'])
        self.assertEqual(second['counts'], first_counts)
        self.assertEqual(second['rewards'], first_rewards)
        self.assertEqual(second['last_outcome']['trial_id'], trial['trial_id'])

    def test_reference_off_arrival_is_unknown_without_defined_comfort_loss(self):
        f = self.fixture
        f.e.rng = Mock(random=lambda: 0.0)
        self.assertIsNone(f.propose())
        active = f.e._get(f.a['id'])['active']
        self.assertEqual(active['kind'], 'reference')
        self.assertEqual(active['baseline'], 0.0)

        f.now += 1
        f.states['binary_sensor.pir']['state'] = 'on'
        f.states['binary_sensor.pir']['last_changed'] = f.now
        f.e.observe(f.a, f.states, 0.0)

        status = f.e.status(f.a['id'])
        self.assertEqual(status['counts'], [0, 0, 0])
        self.assertIsNone(status['last_outcome']['reward'])
        self.assertEqual(
            status['last_outcome']['reason'],
            'reference arrival observed; no comfort-loss outcome defined',
        )


if __name__ == '__main__':
    unittest.main()
