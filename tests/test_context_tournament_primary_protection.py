import unittest
from types import SimpleNamespace

from support import *
from context_tournament_primary_protection import (
    primary_feature_ids,
    primary_replacement_gain,
    replacement_plan,
)
from settings import DEFAULT_OPTIONS


class PrimaryProtectionTests(unittest.TestCase):
    def _policy(self, active, **meta):
        return SimpleNamespace(
            schema=SimpleNamespace(entities=list(active)),
            selection_meta=dict(meta),
        )

    def _agent(self):
        return agent(input_entities=['*'])

    def _tournament(self, active, challenger='binary_sensor.challenger'):
        scores = {eid: 0.2 + i * 0.05 for i, eid in enumerate(active)}
        scores[challenger] = 0.95
        return {'feature_scores': scores}

    def test_default_primary_replacement_gain_is_seven_points(self):
        self.assertAlmostEqual(DEFAULT_OPTIONS['context_primary_replacement_gain'], 0.07)
        self.assertAlmostEqual(primary_replacement_gain(DEFAULT_OPTIONS), 0.07)

    def test_primary_feature_set_covers_existing_primary_metadata(self):
        active = [
            'binary_sensor.occupancy',
            'binary_sensor.local',
            'binary_sensor.local_2',
            'sensor.behaviour',
            'sensor.other',
        ]
        policy = self._policy(
            active,
            primary_occupancy_sensor='binary_sensor.occupancy',
            primary_local_sensor='binary_sensor.local',
            primary_local_sensors=['binary_sensor.local', 'binary_sensor.local_2'],
            primary_behavioural_drivers=['sensor.behaviour', 'sensor.not_active'],
        )
        self.assertEqual(
            primary_feature_ids(policy),
            {
                'binary_sensor.occupancy',
                'binary_sensor.local',
                'binary_sensor.local_2',
                'sensor.behaviour',
            },
        )

    def test_primary_sensor_is_not_replaced_by_six_point_gain(self):
        active = [f'binary_sensor.primary_{i}' for i in range(8)]
        policy = self._policy(
            active,
            primary_occupancy_sensor=active[0],
            primary_local_sensor=active[1],
            primary_local_sensors=active[:4],
            primary_behavioural_drivers=active[4:],
        )
        plan = replacement_plan(
            self._agent(), policy, 'binary_sensor.challenger',
            self._tournament(active), 0.84, 0.90, DEFAULT_OPTIONS,
        )
        self.assertEqual(plan['action'], 'blocked')
        self.assertEqual(plan['reason'], 'primary_replacement_gain')
        self.assertFalse(plan['passes'])
        self.assertAlmostEqual(plan['required_gain'], 0.07)

    def test_exact_seven_point_boundary_is_still_not_enough(self):
        active = [f'binary_sensor.primary_{i}' for i in range(8)]
        policy = self._policy(active, primary_occupancy_sensor=active[0],
                              primary_local_sensors=active,
                              primary_behavioural_drivers=active)
        plan = replacement_plan(
            self._agent(), policy, 'binary_sensor.challenger',
            self._tournament(active), 0.84, 0.91, DEFAULT_OPTIONS,
        )
        self.assertEqual(plan['action'], 'blocked')

    def test_primary_sensor_can_be_replaced_after_more_than_seven_points(self):
        active = [f'binary_sensor.primary_{i}' for i in range(8)]
        policy = self._policy(active, primary_occupancy_sensor=active[0],
                              primary_local_sensors=active,
                              primary_behavioural_drivers=active)
        plan = replacement_plan(
            self._agent(), policy, 'binary_sensor.challenger',
            self._tournament(active), 0.84, 0.92, DEFAULT_OPTIONS,
        )
        self.assertEqual(plan['action'], 'replace')
        self.assertTrue(plan['replacement_is_primary'])
        self.assertAlmostEqual(plan['required_gain'], 0.07)
        self.assertIn('binary_sensor.challenger', plan['entities'])

    def test_marginally_better_challenger_uses_non_primary_slot_instead(self):
        active = [f'binary_sensor.active_{i}' for i in range(8)]
        primary = active[0]
        ordinary = active[1]
        policy = self._policy(
            active,
            primary_occupancy_sensor=primary,
            primary_local_sensor=primary,
            primary_local_sensors=[primary],
            primary_behavioural_drivers=[primary],
        )
        tournament = self._tournament(active)
        # Make the primary sensor discovery-ranked weakest. The next weakest ordinary
        # feature should be sacrificed when gain clears 3pp but not the 7pp primary gate.
        tournament['feature_scores'][primary] = 0.01
        tournament['feature_scores'][ordinary] = 0.02
        plan = replacement_plan(
            self._agent(), policy, 'binary_sensor.challenger',
            tournament, 0.84, 0.89, DEFAULT_OPTIONS,
        )
        self.assertEqual(plan['action'], 'replace')
        self.assertEqual(plan['replaced'], ordinary)
        self.assertFalse(plan['replacement_is_primary'])
        self.assertIn(primary, plan['entities'])
        self.assertIn(primary, plan['skipped_primary_features'])
        self.assertAlmostEqual(plan['required_gain'], 0.03)

    def test_primary_threshold_never_weakens_a_stricter_global_margin(self):
        options = dict(DEFAULT_OPTIONS)
        options['context_tournament_min_gain'] = 0.10
        options['context_primary_replacement_gain'] = 0.07
        self.assertAlmostEqual(primary_replacement_gain(options), 0.10)


if __name__ == '__main__':
    unittest.main()
