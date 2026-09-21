"""0.14.63 Stage-1 Correct data foundation regression tests."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import ROOT, state

from correct_data_foundation import (
    ROLE_BOUNDARY,
    ROLE_LOCAL,
    ROLE_RELIABILITY,
    ROLE_TRAJECTORY,
    _eligible_entities,
    _residual_diagnostics,
    deduplicate_supervision_rows,
    semantic_source_role,
    supervision_event_id,
)
from ha import automation_baseline_rules


SRC = ROOT / "adaptive_ai" / "src"


class _Context:
    def __init__(self, mapping=None, metadata=None):
        self.mapping = dict(mapping or {})
        self.metadata = dict(metadata or {})

    def area_for(self, entity_id):
        return self.mapping.get(entity_id)

    def evidence_metadata(self, entity_id):
        return dict(self.metadata.get(entity_id) or {})


class _Engine:
    def __init__(self, states=None, registry=None, mapping=None, metadata=None):
        self.state_map = dict(states or {})
        self.entity_registry = dict(registry or {})
        self.context = _Context(mapping, metadata)
        self.options = {}
        class _Lock:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
        self.lock = _Lock()


class _Policy:
    actions = [0.0, 1.0]

    def predict(self, features):
        value = float(features.get(99, 0.0))
        return ({"value": value}, 1.0, [], "now", 1.0, 0.0)


class CorrectDataFoundationTests(unittest.TestCase):
    def test_real_problem_fixture_has_46_nonconflicting_points_and_overlapping_primary_signal(self):
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "correct_46_nonconflicting.json").read_text(
                encoding="utf-8"
            )
        )
        labels = fixture["labels"]
        self.assertEqual(len(labels), 46)
        self.assertEqual(sum(1 for row in labels if row["desired"] == 0), 29)
        self.assertEqual(sum(1 for row in labels if row["desired"] == 1), 17)
        event_ids = {row["supervision_event_id"] for row in labels}
        self.assertEqual(len(event_ids), 46)
        by_value = {}
        for row in labels:
            value = row["schema"]["sensor.fixture_stationary_energy"]
            by_value.setdefault(value, set()).add(row["desired"])
        self.assertTrue(any(values == {0, 1} for values in by_value.values()))

    def test_supervision_identity_is_stable_across_lineage_rows(self):
        event = supervision_event_id("target-fingerprint", 1234.56789123, 1.0)
        copied = supervision_event_id("target-fingerprint", 1234.56789124, 1.0)
        self.assertEqual(event, copied)
        rows = [
            {
                "id": 1, "created_ts": 1.0, "sample_ts": 1234.56789123,
                "desired": 1.0, "fingerprint": "target-fingerprint",
                "supervision_event_id": event, "undone_ts": None,
            },
            {
                "id": 9, "created_ts": 2.0, "sample_ts": 1234.56789124,
                "desired": 1.0, "fingerprint": "target-fingerprint",
                "supervision_event_id": event, "undone_ts": None,
            },
        ]
        deduped = deduplicate_supervision_rows(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["id"], 9)

    def test_humidity_is_reliability_context_not_presence(self):
        agent = {"target_entity": "switch.bathroom_light"}
        engine = _Engine(mapping={
            "switch.bathroom_light": "bathroom",
            "sensor.bathroom_humidity": "bathroom",
        })
        role = semantic_source_role(
            engine, agent, "sensor.bathroom_humidity",
            state("sensor.bathroom_humidity", "86", device_class="humidity"),
        )
        self.assertEqual(role, ROLE_RELIABILITY)

    def test_local_radar_and_remote_radar_have_different_roles(self):
        agent = {"target_entity": "switch.bathroom_light"}
        engine = _Engine(mapping={
            "switch.bathroom_light": "bathroom",
            "sensor.espen4_stationary_energy": "bathroom",
            "binary_sensor.kitchen_radar": "kitchen",
        })
        local = semantic_source_role(
            engine, agent, "sensor.espen4_stationary_energy",
            state("sensor.espen4_stationary_energy", "18"),
        )
        remote = semantic_source_role(
            engine, agent, "binary_sensor.kitchen_radar",
            state("binary_sensor.kitchen_radar", "on", device_class="motion"),
        )
        self.assertEqual(local, ROLE_LOCAL)
        self.assertEqual(remote, ROLE_TRAJECTORY)

    def test_boundary_role_requires_explicit_boundary_evidence(self):
        agent = {"target_entity": "switch.bathroom_light"}
        entity = "binary_sensor.kitchen_to_bathroom"
        engine = _Engine(mapping={
            "switch.bathroom_light": "bathroom",
            entity: "kitchen",
        })
        role = semantic_source_role(
            engine, agent, entity,
            state(entity, "on", device_class="motion"),
            registry={entity: {"boundary_for": "bathroom"}},
        )
        self.assertEqual(role, ROLE_BOUNDARY)

    def test_broad_context_candidate_pool_preserves_actuator_and_electrical_exclusions(self):
        target = "switch.bathroom_light"
        motion = "binary_sensor.bathroom_motion"
        states = {
            target: state(target, "off"),
            "switch.other_actuator": state("switch.other_actuator", "off"),
            "sensor.mains_power": state(
                "sensor.mains_power", "400", unit_of_measurement="W"
            ),
            motion: state(motion, "on", device_class="motion"),
        }
        engine = _Engine(
            states=states,
            registry={},
            mapping={target: "bathroom", motion: "bathroom"},
        )
        entities, _states, _registry = _eligible_entities(
            engine, {"target_entity": target}
        )
        self.assertIn(motion, entities)
        self.assertNotIn(target, entities)
        self.assertNotIn("switch.other_actuator", entities)
        self.assertNotIn("sensor.mains_power", entities)

    def test_baseline_parser_keeps_thresholds_and_hold_without_hardcoding(self):
        trigger = [
            {
                "trigger": "numeric_state",
                "entity_id": ["sensor.espen4_stationary_energy"],
                "above": 22,
            },
            {
                "trigger": "numeric_state",
                "entity_id": "sensor.espen4_stationary_energy",
                "below": 12,
                "for": {"hours": 0, "minutes": 0, "seconds": 3},
            },
        ]
        rules = automation_baseline_rules(trigger)
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["above"], 22.0)
        self.assertIsNone(rules[0]["below"])
        self.assertEqual(rules[1]["below"], 12.0)
        self.assertEqual(rules[1]["for_seconds"], 3.0)

    def test_residual_diagnostics_use_supervision_ids_and_class_counts(self):
        samples = [
            {
                "label": {
                    "id": 10, "fingerprint": "fp", "sample_ts": 1.0,
                    "desired": 1.0,
                    "supervision_event_id": "event-on",
                },
                "features": {99: 0.0},
                "desired_idx": 1,
            },
            {
                "label": {
                    "id": 11, "fingerprint": "fp", "sample_ts": 2.0,
                    "desired": 0.0,
                    "supervision_event_id": "event-off",
                },
                "features": {99: 0.0},
                "desired_idx": 0,
            },
        ]
        result = _residual_diagnostics(_Policy(), samples)
        self.assertEqual(result["current_schema_fit_count"], 1)
        self.assertEqual(result["current_schema_fit_total"], 2)
        self.assertEqual(result["unresolved_correct_ids"], ["event-on"])
        self.assertEqual(result["residual_class_distribution"], {"1": 1})

    def test_broad_capture_is_not_wired_into_realtime_engine_hot_path(self):
        engine_source = (SRC / "engine.py").read_text(encoding="utf-8")
        foundation = (SRC / "correct_data_foundation.py").read_text(encoding="utf-8")
        runtime = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertNotIn("capture_broad_context", engine_source)
        self.assertIn("capture_broad_context(", foundation)
        self.assertIn("install_correct_data_foundation", runtime)
        self.assertIn(
            "explicit_feedback_only_not_event_intent_hot_path", foundation
        )

    def test_debug_export_surfaces_broad_context_and_supervision_summary(self):
        source = (SRC / "correct_learning_debug.py").read_text(encoding="utf-8")
        self.assertIn('"supervision": _supervision_summary(labels)', source)
        self.assertIn('row["broad_context"] = _manual_context_for_label', source)
        self.assertIn('"latest_role_counts"', source)
        self.assertIn('"latest_room_belief"', source)


if __name__ == "__main__":
    unittest.main()
