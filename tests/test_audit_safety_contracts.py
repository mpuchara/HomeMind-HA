import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import agent_candidate_preference_metrics as pref
from test_candidate_preference_metrics import CandidatePreferenceMetricTests
from test_experiments import ExperimentTests


class CandidatePromotionSafetyRegressionTests(unittest.TestCase):
    def test_preference_layer_cannot_remove_false_early_veto(self):
        fixture = CandidatePreferenceMetricTests()
        fixture.setUp()
        try:
            for i in range(40):
                fixture._pair(200 + i * 200, i % 2, True, True, 1, 1)

            base = {
                'promotable': False,
                'fresh_feedback_revision': True,
                'offline_gate_passed': True,
                'candidate_false_early': 1000,
                'live_false_early': 0,
            }

            class Handler:
                def do_GET(self):
                    pass

                def static(self, *args):
                    pass

            fixture.store.get_model = lambda aid: {'version': 10}
            manager = SimpleNamespace(
                store=fixture.store,
                _comparison_summary=lambda *a, **kw: dict(base),
                status=Mock(return_value=None),
                list_status=Mock(return_value=[]),
                core=SimpleNamespace(Handler=Handler),
            )
            pref.install(manager)

            result = manager._comparison_summary(fixture.row, fixture.parent, fixture.child)

            self.assertGreaterEqual(result['preference_confidence'], result['preference_confidence_threshold'])
            self.assertTrue(result['per_action_ready'])
            self.assertTrue(result['accuracy_safety_passed'])
            self.assertFalse(result['false_early_safety_passed'])
            self.assertFalse(result['promotable'])
        finally:
            fixture.store.db.close()

    def test_explicit_false_early_contract_field_is_authoritative(self):
        self.assertFalse(pref._false_early_safety({'false_early_safety_passed': False}))
        self.assertTrue(pref._false_early_safety({'false_early_safety_passed': True}))


class ExperimentOutcomeSafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ExperimentTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_background_presence_sensor_cannot_confirm_local_trial(self):
        f = self.fixture
        f.states['binary_sensor.other_room'] = {
            'entity_id': 'binary_sensor.other_room',
            'state': 'off',
            'attributes': {'device_class': 'motion'},
        }
        f.policy.schema = SimpleNamespace(entities=['binary_sensor.other_room'])

        trial = f.start()
        self.assertIn('binary_sensor.pir', trial['outcome_confirmers'])
        self.assertNotIn('binary_sensor.other_room', trial['outcome_confirmers'])
        f.ack()

        f.states['binary_sensor.other_room']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)
        result = f.e.status(f.a['id'])['last_outcome']

        self.assertIsNone(result['reward'])
        self.assertEqual(result['reason'], 'context changed; ordinary control resumes')

    def test_selected_presence_sensor_from_wrong_area_cannot_confirm(self):
        f = self.fixture
        registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            'binary_sensor.pir': {'area_id': 'hall'},
        }

        trial = f.start(registry=registry)
        self.assertNotIn('binary_sensor.pir', trial['outcome_confirmers'])
        f.ack()

        f.states['binary_sensor.pir']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)
        result = f.e.status(f.a['id'])['last_outcome']

        self.assertIsNone(result['reward'])
        self.assertEqual(result['reason'], 'context changed; ordinary control resumes')

    def test_local_selected_presence_sensor_still_confirms_trial(self):
        f = self.fixture
        registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            'binary_sensor.pir': {'area_id': 'kitchen'},
        }

        trial = f.start(registry=registry)
        self.assertIn('binary_sensor.pir', trial['outcome_confirmers'])
        f.ack()

        f.states['binary_sensor.pir']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)
        result = f.e.status(f.a['id'])['last_outcome']

        self.assertEqual(result['reward'], .6)
        self.assertEqual(result['reason'], 'presence confirmed after decision')


if __name__ == '__main__':
    unittest.main()
