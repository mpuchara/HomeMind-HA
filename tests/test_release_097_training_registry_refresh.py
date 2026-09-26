"""0.14.97 regressions for long isolated training across HA registry refreshes."""
import unittest

from settings import DEFAULT_OPTIONS
from training_process import (
    _expand_topology_scope,
    runtime_topology_fingerprint,
    topology_changed_entities,
    training_options_fingerprint,
    training_relevant_entities,
)


class Release097TrainingRegistryRefreshTests(unittest.TestCase):
    def _states(self):
        return {
            "switch.target": {
                "entity_id": "switch.target",
                "state": "off",
                "attributes": {"friendly_name": "Target"},
            },
            "binary_sensor.motion": {
                "entity_id": "binary_sensor.motion",
                "state": "off",
                "attributes": {
                    "device_class": "motion",
                    "friendly_name": "Motion",
                },
            },
            "sensor.unrelated": {
                "entity_id": "sensor.unrelated",
                "state": "20",
                "attributes": {
                    "device_class": "temperature",
                    "friendly_name": "Unrelated",
                },
            },
            "sensor.same_device": {
                "entity_id": "sensor.same_device",
                "state": "1",
                "attributes": {
                    "device_class": "signal_strength",
                    "friendly_name": "Sibling",
                },
            },
        }

    def _registry(self):
        return {
            "switch.target": {
                "device_id": "dev-target", "area_id": "room", "platform": "shelly",
            },
            "binary_sensor.motion": {
                "device_id": "dev-motion", "area_id": "room", "platform": "esphome",
            },
            "sensor.same_device": {
                "device_id": "dev-motion", "area_id": "room", "platform": "esphome",
            },
            "sensor.unrelated": {
                "device_id": "dev-other", "area_id": "outside", "platform": "met",
            },
        }

    def test_unrelated_entity_registry_refresh_does_not_stale_selected_model(self):
        states = self._states()
        before = self._registry()
        after = self._registry()
        after["sensor.unrelated"] = {
            **after["sensor.unrelated"],
            "area_id": "garden",
        }
        agent = {
            "target_entity": "switch.target",
            "input_entities": ["*"],
        }
        model = {"schema": {"entities": ["binary_sensor.motion"]}}
        relevant = training_relevant_entities(agent, model=model)
        scope = _expand_topology_scope(relevant, before, after)
        self.assertNotIn("sensor.unrelated", scope)
        self.assertEqual(
            runtime_topology_fingerprint(states, before, scope),
            runtime_topology_fingerprint(states, after, scope),
        )
        self.assertEqual(
            topology_changed_entities(states, before, states, after, scope),
            [],
        )

    def test_selected_entity_registry_change_still_invalidates_training(self):
        states = self._states()
        before = self._registry()
        after = self._registry()
        after["binary_sensor.motion"] = {
            **after["binary_sensor.motion"],
            "area_id": "hall",
        }
        scope = _expand_topology_scope(
            {"switch.target", "binary_sensor.motion"}, before, after
        )
        self.assertNotEqual(
            runtime_topology_fingerprint(states, before, scope),
            runtime_topology_fingerprint(states, after, scope),
        )
        self.assertIn(
            "binary_sensor.motion",
            topology_changed_entities(states, before, states, after, scope),
        )

    def test_device_sibling_topology_is_in_validation_scope(self):
        states = self._states()
        before = self._registry()
        after = self._registry()
        after["sensor.same_device"] = {
            **after["sensor.same_device"],
            "platform": "mqtt",
        }
        scope = _expand_topology_scope(
            {"binary_sensor.motion"}, before, after
        )
        self.assertIn("sensor.same_device", scope)
        self.assertNotEqual(
            runtime_topology_fingerprint(states, before, scope),
            runtime_topology_fingerprint(states, after, scope),
        )

    def test_ram_and_cpu_knob_changes_are_not_semantic_staleness(self):
        before = dict(DEFAULT_OPTIONS)
        after = dict(before)
        after.update({
            "training_cpu_duty_cycle": 0.70,
            "training_worker_memory_limit_mb": 700,
            "training_replay_ram_cache_rows": 8192,
            "training_home_context_cache_entries": 8,
            "training_sqlite_cache_mb": 8,
        })
        self.assertEqual(
            training_options_fingerprint(before),
            training_options_fingerprint(after),
        )

    def test_learning_option_change_still_invalidates_training(self):
        before = dict(DEFAULT_OPTIONS)
        after = dict(before)
        after["feature_dimensions"] = int(before["feature_dimensions"]) + 1
        self.assertNotEqual(
            training_options_fingerprint(before),
            training_options_fingerprint(after),
        )


if __name__ == "__main__":
    unittest.main()
