"""0.14.67 Stage-5 Correct-driven semantic reliability regressions."""

import json
import unittest

from support import state

from context_engine import ContextEngine
from semantic_reliability import (
    SemanticReliabilityModel,
    SYNTHETIC_DISAGREEMENT,
)
from settings import DEFAULT_OPTIONS


RADAR = "sensor.bathroom_stationary_energy"
HUMIDITY = "sensor.bathroom_humidity"
SECONDARY = "sensor.bathroom_camera_score"
TARGET = "light.bathroom"


def _snapshot(source_value, desired, humidity=None, secondary=None):
    snap = {
        RADAR: {
            "role": "LOCAL_EVIDENCE",
            "source_role": "radar_activity",
            "normalized_value": float(source_value),
        },
    }
    if humidity is not None:
        snap[HUMIDITY] = {
            "role": "RELIABILITY_CONTEXT",
            "source_role": "reliability_context",
            "normalized_value": float(humidity),
        }
    if secondary is not None:
        snap[SECONDARY] = {
            "role": "LOCAL_EVIDENCE",
            "source_role": "auxiliary",
            "normalized_value": float(secondary),
        }
    return snap


def _correct_source_value(desired):
    return .8 if int(desired) else .2


class SemanticReliabilityModelTests(unittest.TestCase):
    def test_no_supported_correct_history_is_neutral(self):
        model = SemanticReliabilityModel()
        result = model.evaluate("bathroom", RADAR, {}, 1.0)
        self.assertEqual(result["factor"], 1.0)
        self.assertFalse(result["calibrated"])

    def test_humidity_only_modulates_after_correct_proves_effect(self):
        model = SemanticReliabilityModel()
        for idx in range(16):
            desired = idx % 2
            high_humidity_error = idx >= 12
            source = _correct_source_value(desired)
            if high_humidity_error:
                source = 1.0 - source
            humidity = .9 if high_humidity_error else .3
            result = model.record_feedback(
                "bathroom", desired,
                _snapshot(source, desired, humidity=humidity),
                f"event-{idx}", float(idx),
            )
            self.assertTrue(result["recorded"])

        profile = model.profiles["bathroom"][RADAR]
        context = profile["reliability_context"]
        self.assertIsNotNone(context)
        self.assertEqual(context["context_id"], HUMIDITY)
        self.assertGreater(context["effect_size"], .12)

        sources = {
            RADAR: {
                "role": "radar_activity", "value": .8, "available": True,
                "communication_reliability": 1.0, "ts": 100.0, "state_since_ts": 100.0,
            },
            HUMIDITY: {
                "role": "reliability_context", "value": .9, "available": True,
                "communication_reliability": 1.0, "ts": 100.0, "state_since_ts": 100.0,
            },
        }
        high = model.evaluate("bathroom", RADAR, sources, 100.0, lambda _s, _t: 1.0)
        sources[HUMIDITY]["value"] = .3
        low = model.evaluate("bathroom", RADAR, sources, 100.0, lambda _s, _t: 1.0)
        self.assertLess(high["factor"], 1.0)
        self.assertEqual(low["factor"], 1.0)
        self.assertEqual(high["context_id"], HUMIDITY)

    def test_random_humidity_does_not_change_trust(self):
        model = SemanticReliabilityModel()
        for idx in range(16):
            desired = idx % 2
            source = _correct_source_value(desired)
            if idx >= 12:
                source = 1.0 - source
            humidity = .2 if idx % 2 == 0 else .8
            model.record_feedback(
                "bathroom", desired,
                _snapshot(source, desired, humidity=humidity),
                f"event-{idx}", float(idx),
            )
        profile = model.profiles["bathroom"][RADAR]
        self.assertIsNone(profile["reliability_context"])

    def test_disagreement_can_be_learned_without_environment_sensor(self):
        model = SemanticReliabilityModel()
        for idx in range(16):
            desired = idx % 2
            secondary = _correct_source_value(desired)
            source = secondary if idx < 12 else 1.0 - secondary
            model.record_feedback(
                "bathroom", desired,
                _snapshot(source, desired, secondary=secondary),
                f"event-{idx}", float(idx),
            )
        profile = model.profiles["bathroom"][RADAR]
        context = profile["reliability_context"]
        self.assertIsNotNone(context)
        self.assertEqual(context["context_id"], SYNTHETIC_DISAGREEMENT)

        sources = {
            RADAR: {
                "role": "radar_activity", "value": .8, "available": True,
                "communication_reliability": 1.0,
            },
            SECONDARY: {
                "role": "auxiliary", "value": .2, "available": True,
                "communication_reliability": 1.0,
            },
        }
        result = model.evaluate(
            "bathroom", RADAR, sources, 100.0, lambda _s, _t: 1.0
        )
        self.assertLess(result["factor"], 1.0)
        self.assertEqual(result["context_id"], SYNTHETIC_DISAGREEMENT)

    def test_duplicate_supervision_event_does_not_inflate_support(self):
        model = SemanticReliabilityModel()
        first = model.record_feedback(
            "bathroom", 1, _snapshot(.8, 1, humidity=.3), "same", 1.0
        )
        duplicate = model.record_feedback(
            "bathroom", 1, _snapshot(.2, 1, humidity=.9), "same", 2.0
        )
        self.assertTrue(first["recorded"])
        self.assertFalse(duplicate["recorded"])
        self.assertEqual(duplicate["reason"], "duplicate_supervision_event")
        self.assertEqual(len(model.feedback["bathroom"]), 1)

    def test_export_reload_preserves_bounded_calibration(self):
        model = SemanticReliabilityModel()
        for idx in range(16):
            desired = idx % 2
            error = idx >= 12
            source = _correct_source_value(desired)
            if error:
                source = 1.0 - source
            model.record_feedback(
                "bathroom", desired,
                _snapshot(source, desired, humidity=.9 if error else .3),
                f"event-{idx}", float(idx),
            )
        restored = SemanticReliabilityModel(
            json.loads(json.dumps(model.export()))
        )
        self.assertEqual(
            restored.profiles["bathroom"][RADAR]["reliability_context"]["context_id"],
            HUMIDITY,
        )
        self.assertEqual(len(restored.feedback["bathroom"]), 16)


class SemanticReliabilityIntegrationTests(unittest.TestCase):
    def _context(self):
        states = {
            RADAR: state(
                RADAR, "80", unit_of_measurement="%",
                friendly_name="Bathroom Stationary Energy",
            ),
            HUMIDITY: state(
                HUMIDITY, "30", unit_of_measurement="%",
                device_class="humidity", friendly_name="Bathroom Humidity",
            ),
            TARGET: state(TARGET, "off"),
        }
        registry = {
            RADAR: {"area_id": "bathroom", "device_id": "radar"},
            HUMIDITY: {"area_id": "bathroom", "device_id": "humidity"},
            TARGET: {"area_id": "bathroom", "device_id": "light"},
        }
        context = ContextEngine(dict(DEFAULT_OPTIONS))
        context.configure(
            states,
            entities=registry,
            areas=[{"area_id": "bathroom", "name": "Bathroom"}],
        )
        return context, states

    @staticmethod
    def _agent():
        return {
            "id": "bathroom-agent",
            "target_entity": TARGET,
            "target_property": "power",
        }

    def _calibrate(self, context):
        for idx in range(16):
            desired = idx % 2
            error = idx >= 12
            source = _correct_source_value(desired)
            if error:
                source = 1.0 - source
            result = context.record_correct_reliability_feedback(
                self._agent(),
                sample_ts=float(idx),
                desired=float(desired),
                snapshot=_snapshot(
                    source, desired, humidity=.9 if error else .3
                ),
                supervision_id=f"correct-{idx}",
            )
            self.assertTrue(result["recorded"])

    def test_humidity_is_admitted_but_never_presence_authority(self):
        context, states = self._context()
        detail = context.source_details[HUMIDITY]
        self.assertEqual(detail["role"], "reliability_context")
        self.assertFalse(detail["occupancy_authority"])
        context.observe(HUMIDITY, states[HUMIDITY], 1.0)
        forecast = context.forecast(TARGET, 1.0)
        self.assertEqual(forecast["occupancy_now"], .5)
        self.assertFalse(forecast["known"])

    def test_correct_calibration_downweights_local_radar_only_in_learned_context(self):
        context, states = self._context()
        self._calibrate(context)

        context.observe(RADAR, states[RADAR], 100.0)
        high_humidity = state(
            HUMIDITY, "90", unit_of_measurement="%",
            device_class="humidity", friendly_name="Bathroom Humidity",
        )
        context.observe(HUMIDITY, high_humidity, 100.0)
        high = context.forecast(TARGET, 100.0)
        high_rel = high["adaptive_presence"]["raw_semantic_reliability"]
        self.assertLess(high_rel, 1.0)
        radar_evidence = next(
            row for row in high["evidence_sources"]
            if row["entity_id"] == RADAR
        )
        self.assertEqual(radar_evidence["communication_reliability"], 1.0)
        self.assertLess(radar_evidence["semantic_reliability"], 1.0)

        low_humidity = state(
            HUMIDITY, "30", unit_of_measurement="%",
            device_class="humidity", friendly_name="Bathroom Humidity",
        )
        context.observe(HUMIDITY, low_humidity, 101.0)
        low = context.forecast(TARGET, 101.0)
        self.assertEqual(
            low["adaptive_presence"]["raw_semantic_reliability"], 1.0
        )

    def test_non_fast_target_correct_does_not_train_presence_reliability(self):
        context, _states = self._context()
        agent = {
            "id": "temperature-agent",
            "target_entity": "climate.bathroom",
            "target_property": "temperature",
        }
        context.mapping["climate.bathroom"] = "bathroom"
        result = context.record_correct_reliability_feedback(
            agent,
            sample_ts=1.0,
            desired=1.0,
            snapshot=_snapshot(.8, 1, humidity=.9),
            supervision_id="not-fast",
        )
        self.assertFalse(result["recorded"])
        self.assertEqual(
            result["reason"],
            "reliability_calibration_is_fast_binary_correct_only",
        )

    def test_runtime_reliability_path_never_reads_storage(self):
        class ExplodingStore:
            def meta_get(self, key, default=None):
                if key in {
                    ContextEngine.ROOM_MODEL_KEY,
                    ContextEngine.LEGACY_ROOM_MODEL_KEY,
                    ContextEngine.SEMANTIC_RELIABILITY_KEY,
                }:
                    return default
                raise AssertionError("unexpected storage read")

            def meta_set(self, *_args, **_kwargs):
                raise AssertionError("runtime forecast must not write storage")

        context = ContextEngine(dict(DEFAULT_OPTIONS), store=ExplodingStore())
        states = {
            RADAR: state(RADAR, "80", unit_of_measurement="%"),
            HUMIDITY: state(
                HUMIDITY, "70", unit_of_measurement="%",
                device_class="humidity",
            ),
            TARGET: state(TARGET, "off"),
        }
        context.configure(
            states,
            entities={
                RADAR: {"area_id": "bathroom"},
                HUMIDITY: {"area_id": "bathroom"},
                TARGET: {"area_id": "bathroom"},
            },
        )
        context.observe(RADAR, states[RADAR], 1.0)
        context.observe(HUMIDITY, states[HUMIDITY], 1.0)
        context.forecast(TARGET, 1.0)


if __name__ == "__main__":
    unittest.main()
