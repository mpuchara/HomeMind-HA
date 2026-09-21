"""0.14.66 Stage-4 RoomBelief source-semantics regression tests."""

import unittest
from types import SimpleNamespace

from support import state

import correct_data_foundation as foundation
from context import entity_capability_tags
from context_engine import ContextEngine
from engine import Engine
from home_sources import select_sources, target_relative_role
from home_state import RoomBeliefModel
from settings import DEFAULT_OPTIONS


def _context(states, registry, devices=None):
    context = ContextEngine(dict(DEFAULT_OPTIONS))
    areas = sorted({
        row.get("area_id")
        for row in registry.values()
        if row.get("area_id")
    })
    context.configure(
        states,
        entities=registry,
        devices=list(devices or []),
        areas=[{"area_id": area, "name": area.title()} for area in areas],
    )
    return context


class RoomBeliefStage4SemanticsTests(unittest.TestCase):
    def test_espen4_stationary_energy_is_local_raw_evidence_not_occupancy_truth(self):
        raw = "sensor.espen4_stationary_energy"
        target = "switch.bathroom_light"
        states = {
            raw: state(
                raw, "90", unit_of_measurement="%",
                friendly_name="ESPEN4 Stationary Energy",
            ),
            target: state(target, "off"),
        }
        registry = {
            raw: {"area_id": "bathroom", "device_id": "espen4"},
            target: {"area_id": "bathroom", "device_id": "light"},
        }
        context = _context(states, registry)
        self.assertIn(raw, context.admitted)
        self.assertEqual(context.source_details[raw]["role"], "radar_activity")
        self.assertFalse(context.source_details[raw]["occupancy_authority"])

        context.observe(raw, states[raw], 100.0)
        forecast = context.forecast(target, 100.0)
        self.assertTrue(forecast["known"])
        self.assertGreater(forecast["observability"], 0.0)
        self.assertLess(forecast["occupancy_now"], 0.5)
        self.assertIn(
            raw,
            [row["entity_id"] for row in forecast["evidence_sources"]],
        )
        self.assertEqual(
            forecast["presence_capability"]["mode"],
            "virtual_threshold",
        )
        self.assertIn(raw, forecast["presence_capability"]["raw_sources"])

    def test_stationary_energy_role_does_not_encode_bathroom_22_12_thresholds(self):
        raw = "sensor.espen4_stationary_energy"
        registry = {raw: {"area_id": "bathroom"}}
        for value in (5, 11, 12, 18, 22, 23, 90):
            st = state(
                raw, str(value), unit_of_measurement="%",
                friendly_name="ESPEN4 Stationary Energy",
            )
            details = select_sources(
                {raw: st}, registry, {raw: "bathroom"}, set()
            )[1]
            self.assertEqual(details[raw]["role"], "radar_activity")
            self.assertFalse(details[raw]["occupancy_authority"])

    def test_stationary_target_distance_is_context_but_not_presence_authority(self):
        distance = "sensor.espen4_stationary_target_distance"
        target = "switch.bathroom_light"
        states = {
            distance: state(
                distance, "135", unit_of_measurement="cm",
                friendly_name="ESPEN4 Stationary Target Distance",
            ),
            target: state(target, "off"),
        }
        registry = {
            distance: {"area_id": "bathroom", "device_id": "espen4"},
            target: {"area_id": "bathroom", "device_id": "light"},
        }
        context = _context(states, registry)
        self.assertIn(distance, context.admitted)
        self.assertEqual(context.source_details[distance]["role"], "radar_distance")
        self.assertFalse(context.source_details[distance]["occupancy_authority"])
        context.observe(distance, states[distance], 100.0)
        forecast = context.forecast(target, 100.0)
        self.assertLessEqual(forecast["occupancy_now"], 0.5)
        self.assertNotIn(
            distance,
            forecast["presence_capability"]["raw_sources"],
        )

    def test_detection_score_is_raw_auxiliary_not_calibrated_probability(self):
        score = "sensor.camera_ai_detection_score"
        states = {
            score: state(
                score, "75", unit_of_measurement="%",
                friendly_name="Camera AI Detection Score",
            )
        }
        registry = {score: {"area_id": "garden"}}
        context = _context(states, registry)
        self.assertEqual(context.source_details[score]["role"], "auxiliary")
        self.assertFalse(context.source_details[score]["calibrated_probability"])
        self.assertFalse(context.source_details[score]["occupancy_authority"])
        context.observe(score, states[score], 100.0)
        forecast = context.home.forecast("garden", 100.0)
        self.assertLess(forecast["occupancy_now"], 0.5)

    def test_target_relative_roles_are_shared_and_do_not_promote_remote_truth(self):
        bathroom_raw = "sensor.espen4_stationary_energy"
        kitchen_boundary = "binary_sensor.kitchen_radar"
        hall_pir = "binary_sensor.hall_pir"
        humidity = "sensor.bathroom_humidity"
        target = "switch.bathroom_light"
        mapping = {
            bathroom_raw: "bathroom",
            kitchen_boundary: "kitchen",
            hall_pir: "hall",
            humidity: "bathroom",
            target: "bathroom",
        }
        states = {
            bathroom_raw: state(
                bathroom_raw, "50", unit_of_measurement="%",
                friendly_name="Stationary Energy",
            ),
            kitchen_boundary: state(
                kitchen_boundary, "off", device_class="motion",
                boundary_for="bathroom",
            ),
            hall_pir: state(hall_pir, "off", device_class="motion"),
            humidity: state(
                humidity, "70", unit_of_measurement="%",
                device_class="humidity",
            ),
        }
        registry = {
            bathroom_raw: {"area_id": "bathroom"},
            kitchen_boundary: {
                "area_id": "kitchen",
                "boundary_for": "bathroom",
            },
            hall_pir: {"area_id": "hall"},
            humidity: {"area_id": "bathroom"},
        }
        details = select_sources(states, registry, mapping, set())[1]
        self.assertEqual(
            target_relative_role(
                bathroom_raw, states[bathroom_raw], registry[bathroom_raw],
                mapping, target, "bathroom", details.get(bathroom_raw),
            ),
            foundation.ROLE_LOCAL,
        )
        self.assertEqual(
            target_relative_role(
                kitchen_boundary, states[kitchen_boundary],
                registry[kitchen_boundary], mapping, target, "bathroom",
                details.get(kitchen_boundary),
            ),
            foundation.ROLE_BOUNDARY,
        )
        self.assertEqual(
            target_relative_role(
                hall_pir, states[hall_pir], registry[hall_pir],
                mapping, target, "bathroom", details.get(hall_pir),
            ),
            foundation.ROLE_TRAJECTORY,
        )
        self.assertEqual(
            target_relative_role(
                humidity, states[humidity], registry[humidity],
                mapping, target, "bathroom", details.get(humidity),
            ),
            foundation.ROLE_RELIABILITY,
        )

    def test_explicit_boundary_raises_arrival_without_setting_occupancy(self):
        boundary = "binary_sensor.kitchen_radar"
        target = "switch.bathroom_light"
        states = {
            boundary: state(
                boundary, "off", friendly_name="Kitchen Radar",
                boundary_for="bathroom",
            ),
            target: state(target, "off"),
        }
        registry = {
            boundary: {
                "area_id": "kitchen",
                "boundary_for": "bathroom",
            },
            target: {"area_id": "bathroom"},
        }
        context = _context(states, registry)
        self.assertEqual(context.source_details[boundary]["role"], "boundary_signal")
        self.assertFalse(context.source_details[boundary]["occupancy_authority"])

        context.observe(boundary, states[boundary], 1.0)
        context.observe(
            boundary,
            state(
                boundary, "on", friendly_name="Kitchen Radar",
                boundary_for="bathroom",
            ),
            2.0,
        )
        forecast = context.forecast(target, 2.0)
        self.assertEqual(forecast["occupancy_now"], 0.5)
        self.assertFalse(forecast["known"])
        self.assertGreater(
            forecast["boundary_arrival_probability_by_horizon"]["3s"], 0.0
        )
        self.assertGreater(forecast["arrival_probability"], 0.0)
        self.assertTrue(forecast["boundary_evidence"])

        expired = context.forecast(target, 20.0)
        self.assertEqual(
            expired["boundary_arrival_probability_by_horizon"]["5s"], 0.0
        )
        self.assertEqual(expired["occupancy_now"], 0.5)

    def test_remote_pir_cannot_certify_target_room_occupancy(self):
        model = RoomBeliefModel()
        model.observe(
            "binary_sensor.hall_pir", "hall", 1.0, 1.0,
            evidence={"role": "pir", "value_semantics": "event_presence"},
        )
        bathroom = model.forecast("bathroom", 1.0)
        self.assertEqual(bathroom["occupancy_now"], 0.5)
        self.assertFalse(bathroom["known"])
        self.assertEqual(bathroom["evidence_sources"], [])

    def test_boundary_runtime_state_is_cleared_and_never_serialized(self):
        model = RoomBeliefModel()
        model.observe(
            "binary_sensor.kitchen_radar", "kitchen", 0.0, 1.0,
            evidence={
                "role": "boundary_signal",
                "boundary_for": ["bathroom"],
            },
        )
        model.observe(
            "binary_sensor.kitchen_radar", "kitchen", 1.0, 2.0,
            evidence={
                "role": "boundary_signal",
                "boundary_for": ["bathroom"],
            },
        )
        self.assertTrue(model.boundary_hints)
        exported = model.export()
        self.assertNotIn("boundary_hints", exported)
        model.reset_movement_state()
        self.assertEqual(list(model.boundary_hints), [])

    def test_explicit_boundary_dependency_wakes_target_without_whole_house_fanout(self):
        boundary = "binary_sensor.kitchen_radar"
        unrelated = "binary_sensor.hall_pir"
        target = "switch.bathroom_light"
        states = {
            boundary: state(boundary, "off", boundary_for="bathroom"),
            unrelated: state(unrelated, "off", device_class="motion"),
            target: state(target, "off"),
        }
        registry = {
            boundary: {"area_id": "kitchen", "boundary_for": "bathroom"},
            unrelated: {"area_id": "hall"},
            target: {"area_id": "bathroom"},
        }
        context = _context(states, registry)
        dummy = SimpleNamespace(
            context=context,
            experiments=SimpleNamespace(watches=lambda _aid: ()),
        )
        agent = {
            "id": "bathroom-agent",
            "target_entity": target,
            "input_entities": ["*"],
        }
        deps = Engine.event_dependencies(dummy, agent, policy=None)
        self.assertIn(boundary, deps)
        self.assertNotIn(unrelated, deps)

    def test_capability_tags_recognize_stationary_energy_and_target_distance(self):
        stationary = state(
            "sensor.espen4_stationary_energy", "35",
            unit_of_measurement="%",
            friendly_name="ESPEN4 Stationary Energy",
        )
        distance = state(
            "sensor.espen4_moving_target_distance", "120",
            unit_of_measurement="cm",
            friendly_name="ESPEN4 Moving Target Distance",
        )
        self.assertIn(
            "activity",
            entity_capability_tags(
                "sensor.espen4_stationary_energy", stationary
            ),
        )
        self.assertIn(
            "activity",
            entity_capability_tags(
                "sensor.espen4_moving_target_distance", distance
            ),
        )


if __name__ == "__main__":
    unittest.main()
