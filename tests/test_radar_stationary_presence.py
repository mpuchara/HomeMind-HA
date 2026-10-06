import unittest

from context import entity_capability_tags, is_discrete_occupancy_entity, context_scalar, select_context_entities
from radar_context import radar_role, retain_observed_on_dwell
from training_quality import sensor_snapshot, light_dwell_reward


class RadarStationaryTests(unittest.TestCase):
    agent = {"target_entity": "light.bathroom"}
    still = "binary_sensor.bathroom_has_still_target"
    moving = "binary_sensor.bathroom_has_moving_target"
    energy = "sensor.bathroom_still_energy"
    distance = "sensor.bathroom_still_distance"
    entities = [still, moving, energy, distance]

    def snapshot(self, still, moving, energy="40", distance="150"):
        states = {e: {"state": v, "attributes": {}} for e, v in zip(
            self.entities, [still, moving, energy, distance])}
        registry = {e: {"area_id": "bathroom", "device_id": "radar"} for e in self.entities}
        registry["light.bathroom"] = {"area_id": "bathroom"}
        return sensor_snapshot(self.agent, self.entities, states, registry)

    def test_still_target_without_device_class_is_persistent_presence(self):
        self.assertTrue(is_discrete_occupancy_entity(self.still, {"state": "on"}))
        self.assertIn("occupancy", entity_capability_tags(self.still, {}))
        snapshot = self.snapshot("on", "off")
        self.assertIn(self.still, snapshot["active"])
        self.assertIn(self.still, snapshot["reliable"])
        self.assertEqual(light_dwell_reward(0, 1, snapshot, snapshot, False)[0], -1)

    def test_entry_still_washbasin_exit_preserves_need_until_exit(self):
        for still, moving in [("off", "on"), ("on", "off"), ("on", "off"), ("off", "on")]:
            snapshot = self.snapshot(still, moving)
            self.assertTrue(retain_observed_on_dwell(snapshot))
            self.assertEqual(light_dwell_reward(1, 1, snapshot, snapshot, True)[0], 1)
            self.assertEqual(light_dwell_reward(0, 1, snapshot, snapshot, True)[0], -1)
        exited = self.snapshot("off", "off", "0", "0")
        self.assertFalse(retain_observed_on_dwell(exited))
        self.assertEqual(light_dwell_reward(0, 1, exited, exited, False)[0], 1)

    def test_unknown_moving_channel_does_not_certify_absence(self):
        snapshot = self.snapshot("off", "unavailable")
        self.assertNotIn(self.still, snapshot["absent"])
        self.assertTrue(retain_observed_on_dwell(snapshot))
        self.assertEqual(light_dwell_reward(1, 1, snapshot, snapshot, False)[0], 1)

    def test_numeric_signal_does_not_create_boolean_presence(self):
        for eid in (self.energy, self.distance):
            self.assertFalse(is_discrete_occupancy_entity(eid, {"state": "200"}))
        snapshot = self.snapshot("unknown", "unknown", "80", "150")
        self.assertFalse(snapshot["active"])
        self.assertFalse(snapshot["absent"])
        self.assertEqual(radar_role(self.distance), "distance")

    def test_room_distances_are_distinguishable_and_units_equivalent(self):
        def value(raw, unit):
            return context_scalar(self.distance, {"state": str(raw), "attributes": {
                "unit_of_measurement": unit, "device_class": "distance"}}, self.agent)
        self.assertLess(value(150, "cm"), value(300, "cm"))
        self.assertGreater(value(300, "cm") - value(150, "cm"), .1)
        self.assertAlmostEqual(value(150, "cm"), value(1.5, "m"))
        self.assertAlmostEqual(value(150, "cm"), value(1500, "mm"))
        self.assertEqual(radar_role("sensor.radar_g0_still_energy"), "gate_energy")

    def test_foreign_room_cannot_extend_light_evidence(self):
        registry = {e: {"area_id": "hall"} for e in self.entities}
        registry["light.bathroom"] = {"area_id": "bathroom"}
        snapshot = sensor_snapshot(self.agent, self.entities,
                                   {e: {"state": "on"} for e in self.entities}, registry)
        self.assertFalse(retain_observed_on_dwell(snapshot))

    def test_selection_reserves_core_radar_before_gate_energies(self):
        states = {e: {"state": "on" if e.startswith("binary_sensor") else "40"}
                  for e in self.entities}
        for gate in range(9):
            states[f"sensor.bathroom_g{gate}_still_energy"] = {"state": "50"}
        states["light.bathroom"] = {"state": "off"}
        registry = {e: {"area_id": "bathroom"} for e in states}
        agent = dict(self.agent, target_property="power", input_entities=["*"])
        selected, _ = select_context_entities(agent, states, registry, [], max_entities=4)
        self.assertEqual(len(selected), 4)
        self.assertEqual(set(selected), set(self.entities))


if __name__ == "__main__":
    unittest.main()
