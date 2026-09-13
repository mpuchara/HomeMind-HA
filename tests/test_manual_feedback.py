import unittest

from support import agent
from manual_feedback import _manual_value, _physical_manual_snapshot


class FakeEngine:
    def __init__(self, runtime, own=False):
        self.runtime = runtime
        self._own = own

    def own_command_echo(self, agent, state, current):
        return self._own


class ManualFeedbackTests(unittest.TestCase):
    def test_binary_user_value_is_normalized(self):
        a = agent(target_property="power", min_value=0, max_value=1)
        self.assertEqual(_manual_value(a, {"attributes": {}}, 0), 0.0)
        self.assertEqual(_manual_value(a, {"attributes": {}}, 0.8), 1.0)

    def test_numeric_user_value_is_clamped_and_quantized(self):
        a = agent(target_entity="number.test", target_property="value", min_value=0, max_value=10)
        state = {"attributes": {"min": 2, "max": 8, "step": 0.5}}
        self.assertEqual(_manual_value(a, state, 9), 8.0)
        self.assertEqual(_manual_value(a, state, 3.26), 3.5)

    def test_percentage_user_value_respects_hardware_step(self):
        a = agent(target_entity="fan.test", target_property="percentage", min_value=0, max_value=100)
        state = {"attributes": {"percentage_step": 25}}
        self.assertEqual(_manual_value(a, state, 61), 50.0)

    def test_physical_manual_change_detects_wrong_shadow_prediction(self):
        a = agent()
        engine = FakeEngine({a["id"]: {"previous_target": 0.0, "last_prediction": 0.0, "pending": None}})
        state_map = {
            a["target_entity"]: {
                "entity_id": a["target_entity"],
                "state": "on",
                "attributes": {},
                "context": {"user_id": "user-1", "parent_id": None},
            }
        }
        snap = _physical_manual_snapshot(engine, a, state_map)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["desired"], 1.0)
        self.assertEqual(snap["predicted"], 0.0)
        self.assertFalse(snap["had_pending"])

    def test_own_service_echo_is_not_physical_manual_feedback(self):
        a = agent()
        engine = FakeEngine({a["id"]: {"previous_target": 0.0, "last_prediction": 0.0, "pending": None}}, own=True)
        state_map = {
            a["target_entity"]: {
                "entity_id": a["target_entity"],
                "state": "on",
                "attributes": {},
                "context": {"user_id": "user-1", "parent_id": None},
            }
        }
        self.assertIsNone(_physical_manual_snapshot(engine, a, state_map))


if __name__ == "__main__":
    unittest.main()
