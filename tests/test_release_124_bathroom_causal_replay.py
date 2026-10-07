"""Regression coverage for numeric historical rule replay and false occupancy priors."""
import unittest

from support import ROOT  # noqa: F401 - make src importable during discovery
from context import (
    entity_capability_tags, is_discrete_occupancy_entity,
    select_context_entities,
)
from fast_automation_replay import (
    numeric_threshold_edges, paired_numeric_baseline, threshold_event,
)


def reading(ts, value, entity="sensor.espen4_stationary_energy"):
    return {
        "id": int(ts) + 1,
        "ts": float(ts),
        "state": str(value),
        "entity_id": entity,
        "attributes_json": "{}",
    }


def bathroom_rules():
    return {
        "automation_baseline_automations": [
            {
                "enabled": True,
                "action_services": ["switch.turn_on"],
                "baseline_rules": [
                    {"source": "trigger", "kind": "numeric_state",
                     "entity_id": "sensor.espen4_stationary_energy",
                     "above": 22, "below": None, "for_seconds": None}
                ],
            },
            {
                "enabled": True,
                "action_services": ["switch.turn_off"],
                "baseline_rules": [
                    {"source": "trigger", "kind": "numeric_state",
                     "entity_id": "sensor.espen4_stationary_energy",
                     "above": None, "below": 12, "for_seconds": 3}
                ],
            },
        ]
    }


class FakeTracker:
    def __init__(self, values):
        self.rows = sorted(
            (reading(ts, val) for ts, val in values), key=lambda x: x["ts"]
        )

    def _before(self, eid, ts, count):
        return [row for row in self.rows if row["ts"] < ts][-count:]

    def _base_interval_rows(self, entities, lo, hi, per_entity_limit=None):
        return [
            row for row in self.rows if
            row["entity_id"] in entities and lo <= row["ts"] <= hi
        ]


class BathroomCausalReplayTests(unittest.TestCase):
    def test_detects_real_numeric_baseline_without_hardcoded_thresholds(self):
        pair = paired_numeric_baseline(bathroom_rules())
        self.assertEqual(pair["sensor"], "sensor.espen4_stationary_energy")
        self.assertEqual(pair["on_threshold"], 22)
        self.assertEqual(pair["off_threshold"], 12)
        self.assertEqual(pair["off_hold"], 3)

    def test_ignores_ambiguous_or_incomplete_automation_pairs(self):
        rules = bathroom_rules()
        rules["automation_baseline_automations"].pop()
        self.assertIsNone(paired_numeric_baseline(rules))
        rules = bathroom_rules()
        rules["automation_baseline_automations"][1]["baseline_rules"][0]["below"] = 25
        self.assertIsNone(paired_numeric_baseline(rules))

    def test_off_requires_three_seconds_of_continuous_low_energy(self):
        values = [(0, 25), (10, 11), (12, 14), (15, 10), (17, 11), (18, 9)]
        edges = numeric_threshold_edges(
            [reading(ts, val) for ts, val in values],
            12, above=False, hold_seconds=3, end_ts=20,
        )
        self.assertEqual(edges, [18.0])

    def test_edge_features_start_only_after_the_signal_was_received(self):
        rows = [reading(0, 8), {**reading(10, 55), "received_ts": 10.25}]
        self.assertEqual(numeric_threshold_edges(rows, 22, above=True, end_ts=11), [10.25])
        self.assertEqual(numeric_threshold_edges(rows, 22, above=True, end_ts=10.1), [])

    def test_late_old_event_does_not_replace_newer_known_signal(self):
        rows = [reading(0, 8), reading(10, 8), {**reading(1, 55), "received_ts": 20}]
        self.assertEqual(numeric_threshold_edges(rows, 22, above=True, end_ts=25), [])

    def test_zero_crossing_of_unrelated_distance_is_not_a_presence_edge(self):
        kitchen = {
            "entity_id": "sensor.kitchen_presence_still_distance",
            "state": "257",
            "attributes": {"unit_of_measurement": "cm"},
        }
        self.assertNotIn(
            "occupancy", entity_capability_tags(kitchen["entity_id"], kitchen)
        )
        self.assertFalse(is_discrete_occupancy_entity(
            kitchen["entity_id"], kitchen
        ))
        radar = {
            "entity_id": "sensor.espen4_stationary_energy",
            "state": "32",
            "attributes": {},
        }
        self.assertFalse(is_discrete_occupancy_entity(
            radar["entity_id"], radar
        ))

    def test_context_selection_keeps_radar_but_not_kitchen_as_primary(self):
        target = "switch.shellyplus1pm_441793a613bc_switch_0"
        radar = "sensor.espen4_stationary_energy"
        kitchen_distance = "sensor.kitchen_presence_still_distance"
        kitchen_energy = "sensor.kitchen_presence_move_energy"
        battery = "sensor.sonoff_snzb_02wd_bateria"
        firmware = "update.sonoff_snzb_02wd_firmware"
        states = {
            target: {"entity_id": target, "state": "off", "attributes": {}},
            radar: {"entity_id": radar, "state": "19", "attributes": {
                "friendly_name": "ESPEN4 Stationary Energy",
                "unit_of_measurement": "%"}},
            kitchen_distance: {"entity_id": kitchen_distance, "state": "450",
                               "attributes": {"unit_of_measurement": "cm"}},
            kitchen_energy: {"entity_id": kitchen_energy, "state": "38",
                             "attributes": {"unit_of_measurement": "%"}},
            battery: {"entity_id": battery, "state": "70",
                      "attributes": {"unit_of_measurement": "%"}},
            firmware: {"entity_id": firmware, "state": "off",
                       "attributes": {}},
        }
        registry = {
            target: {"area_id": "lazienka", "device_id": "light-0"},
            radar: {"area_id": "lazienka", "device_id": "radar-0"},
            kitchen_distance: {"area_id": "kuchnia", "device_id": "radar-1"},
            kitchen_energy: {"area_id": "kuchnia", "device_id": "radar-1"},
            battery: {"area_id": "lazienka", "device_id": "battery-0"},
            firmware: {"area_id": "lazienka", "device_id": "battery-0"},
        }
        agent = {"id": "bathroom", "target_entity": target,
                 "target_property": "power", "input_entities": ["*"]}
        selected, meta = select_context_entities(
            agent, states, registry, {radar},
            relevance_scores={
                radar: 0.80, kitchen_distance: 1.0, kitchen_energy: 0.78,
            },
        )
        self.assertIn(radar, selected)
        self.assertNotIn(battery, selected)
        self.assertNotIn(firmware, selected)
        self.assertIsNone(meta["primary_occupancy_sensor"])
        self.assertEqual(meta["primary_behavioural_drivers"][0], radar)

    def test_binary_presence_remains_eligible(self):
        occupancy = {
            "entity_id": "binary_sensor.bathroom_presence",
            "state": "on",
            "attributes": {"device_class": "occupancy"},
        }
        self.assertTrue(is_discrete_occupancy_entity(
            occupancy["entity_id"], occupancy
        ))

    def test_historical_on_and_off_edges_follow_actual_thresholds(self):
        tracker = FakeTracker([
            (0, 7), (2, 12), (4, 23), (5, 24), (6, 20),
            (7, 11), (8, 15), (10, 11), (12, 9), (13, 9),
            (15, 8), (17, 23),
        ])
        self.assertEqual(threshold_event(
            tracker, "sensor.espen4_stationary_energy", 0.1, 6,
            22, above=True, latest=True,
        ), 4.0)
        self.assertEqual(threshold_event(
            tracker, "sensor.espen4_stationary_energy", 6, 16,
            12, above=False, hold_seconds=3,
        ), 13.0)
        self.assertEqual(threshold_event(
            tracker, "sensor.espen4_stationary_energy", 16, 18,
            22, above=True,
        ), 17.0)

    def test_never_use_a_constant_positive_distance_as_an_off_onset(self):
        tracker = FakeTracker([(0, 600), (1, 601), (2, 550)])
        self.assertIsNone(threshold_event(
            tracker, "sensor.espen4_stationary_energy", 0.5, 2,
            12, above=False, hold_seconds=3,
        ))


if __name__ == "__main__":
    unittest.main()
