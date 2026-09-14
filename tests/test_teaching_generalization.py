import time
import unittest
from unittest.mock import patch

from context import ExplicitFeatureSchema
from support import state
import test_executor as fixtures


class TeachingGeneralizationTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    tearDown = fixtures.ExecutorTests.tearDown

    def prepare(self):
        self.store.update_agent(self.a['id'], {'mode': 'shadow'})
        self.a = self.store.get_agent_config(self.a['id'])
        self.e.state_map['binary_sensor.motion'] = state(
            'binary_sensor.motion', 'on', device_class='motion'
        )
        self.e.state_map['sensor.room_lux'] = state(
            'sensor.room_lux', '100', unit_of_measurement='lx', device_class='illuminance'
        )
        self.model.schema = ExplicitFeatureSchema(
            self.model.dims, ['binary_sensor.motion', 'sensor.room_lux']
        )
        self.e.runtime[self.a['id']] = {'last_prediction': 0}
        return self.e.teaching

    def test_teach_is_reused_when_analogue_context_drifts(self):
        teaching = self.prepare()
        result = teaching.teach(self.e, self.a, 1)
        self.assertEqual(result['desired_value'], 1)

        # Real rooms do not reproduce an identical feature vector.  A daylight change
        # must not make the agent forget a user correction while occupancy is unchanged.
        self.e.state_map['sensor.room_lux'] = state(
            'sensor.room_lux', '400', unit_of_measurement='lx', device_class='illuminance'
        )
        desired, teaching_id = teaching.predict(
            self.a, self.model, self.e.state_map, self.e.temporal_history, time.time()
        )
        self.assertEqual(desired, 1)
        self.assertEqual(teaching_id, result['label_id'])

    def test_teach_does_not_cross_a_motion_state_flip(self):
        teaching = self.prepare()
        teaching.teach(self.e, self.a, 1)

        self.e.state_map['binary_sensor.motion'] = state(
            'binary_sensor.motion', 'off', device_class='motion'
        )
        self.assertIsNone(
            teaching.match(self.a, self.model, self.e.state_map, self.e.temporal_history, time.time())
        )


if __name__ == '__main__':
    unittest.main()
