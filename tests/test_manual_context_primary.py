import unittest
from types import SimpleNamespace
from support import state, agent
import manual_context_learning as m
from manual_context_primary import install


class ManualPrimaryTests(unittest.TestCase):
    def test_manual_score_can_replace_wrong_primary(self):
        install(SimpleNamespace())
        wrong = "binary_sensor.wrong_presence"; right = "binary_sensor.bath_presence"
        states = {wrong: state(wrong, "on", device_class="presence"),
                  right: state(right, "on", device_class="presence")}
        meta = {"primary_occupancy_sensor": wrong, "primary_local_sensors": [wrong],
                "selection_reasons": {wrong: ["local-primary"], right: []}}
        selected, meta = m._promote_manual_entities(agent(), states, [wrong, right], meta, {right: .9})
        self.assertEqual(meta["primary_occupancy_sensor"], right)
        self.assertIn("manual-primary", meta["selection_reasons"][right])


if __name__ == "__main__": unittest.main()
