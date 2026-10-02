"""0.14.124 observer-only false activation classification regressions."""
from pathlib import Path
from types import SimpleNamespace
import unittest

from automatic_correct_rewards import (
    false_activation_suppressor_scores,
    install as install_automatic_correct,
)
from provenance_runtime import install as install_provenance
from support import state
import test_executor as executor_fixture


ROOT = Path(__file__).resolve().parents[1]


class FalseActivationObserverTests(unittest.TestCase):
    def setUp(self):
        self.fixture = executor_fixture.ExecutorTests()
        self.fixture.setUp()
        self.core = SimpleNamespace(
            STORE=self.fixture.store,
            ENGINE=self.fixture.e,
        )
        install_provenance(self.core)
        self.e = self.fixture.e
        self.a = self.fixture.a
        self.e.entity_registry = {
            self.a["target_entity"]: {"area_id": "bathroom"},
            "binary_sensor.bathroom_pir": {"area_id": "bathroom"},
            "binary_sensor.hall_motion": {"area_id": "hall"},
        }
        self.e.state_map["binary_sensor.bathroom_pir"] = state(
            "binary_sensor.bathroom_pir", "off", device_class="motion"
        )
        self.e.state_map["binary_sensor.hall_motion"] = state(
            "binary_sensor.hall_motion", "off", device_class="motion"
        )
        self.e.context.configure(
            self.e.state_map, entities=self.e.entity_registry
        )
        self.service = install_automatic_correct(self.core)

    def tearDown(self):
        self.fixture.tearDown()

    def _seed(self, key, *, action_value=1.0, prediction_inputs=None):
        payload = {
            "resolution_key": "decision:" + key,
            "agent_id": self.a["id"],
            "generation_id": "generation:g1",
            "decision_id": key,
            "trial_id": None,
            "action_index": 1 if action_value >= .5 else 0,
            "action_value": action_value,
            "action_ts": 1000.0,
            "observation_start": 1000.0,
            "observation_end": 1090.0,
            "target_entity": self.a["target_entity"],
            "target_property": "power",
            "area_id": "bathroom",
            "observation_schema_id": "obs-v1:test",
            "observation_mask_id": "mask-test",
            "observation": {
                "timestamp": 1000.0,
                "feature_ids": [],
                "values": [],
            },
            "observation_mask": {
                "schema_id": "obs-v1:test",
                "mask_id": "mask-test",
                "features": [],
            },
            "prediction_inputs": list(prediction_inputs or []),
            "background_dependencies": [],
            "outcome_sources": {},
            "reward_sources": [],
            "metadata": {"test": True},
        }
        row, inserted = self.service.journal.start(payload)
        self.assertTrue(inserted)
        return row

    def _event(self, eid, value, *, event_time, area, origin="unknown",
               user_id=None, device_class="motion"):
        st = state(eid, value, device_class=device_class)
        st["context"] = {
            "id": f"ctx-{eid}-{event_time}",
            "parent_id": None,
            "user_id": user_id,
        }
        self.e.state_map[eid] = st
        self.e.entity_registry[eid] = {"area_id": area}
        event_id, _ = self.e.provenance.record_event(
            eid, st, event_time=event_time,
            received_time=event_time + .01,
            source="ha_state_changed", origin=origin,
        )
        self.e._provenance_latest_events[eid] = (
            float(event_time), event_id, origin
        )
        return event_id

    @staticmethod
    def _runtime(row):
        return {
            "pending": {
                "decision_id": row["decision_id"],
                "action_index": row["action_index"],
                "action_value": row["action_value"],
                "features": {},
                "started_ts": row["action_ts"],
            },
            "reward_components_pending": {},
        }

    def test_cross_area_motion_without_local_confirmation_is_only_suspected(self):
        row = self._seed("remote-only")
        self._event(
            "binary_sensor.hall_motion", "on",
            event_time=999.5, area="hall",
        )
        resolved = self.service.resolve_runtime(
            self.a, self._runtime(row), .15,
            "weak acceptance after settling",
        )
        self.assertEqual(resolved["status"], "unknown")
        self.assertIsNone(resolved["trusted_reward"])
        self.assertEqual(
            resolved["activation_class"],
            "suspected_false_activation",
        )
        self.assertEqual(
            resolved["activation_source_entity_id"],
            "binary_sensor.hall_motion",
        )
        self.assertEqual(resolved["activation_source_area_id"], "hall")
        self.assertLess(resolved["activation_confidence"], .80)
        self.assertTrue(resolved["activation_evidence"]["observer_only"])
        self.assertEqual(
            resolved["activation_evidence"]["reward_effect"], "none"
        )
        summary = self.service.summary(self.a["id"])
        self.assertEqual(
            summary["activation_counts"]["suspected_false_activation"], 1
        )

    def test_same_radar_binary_presence_cannot_confirm_its_own_raw_trigger(self):
        raw = "sensor.bathroom_stationary_energy"
        same_device_presence = "binary_sensor.bathroom_radar_presence"
        self.e.state_map[raw] = state(
            raw, "80", unit_of_measurement="%",
            friendly_name="Bathroom Stationary Energy",
        )
        self.e.state_map[same_device_presence] = state(
            same_device_presence, "off", device_class="presence",
        )
        self.e.entity_registry[raw] = {
            "area_id": "bathroom", "device_id": "bathroom-radar",
        }
        self.e.entity_registry[same_device_presence] = {
            "area_id": "bathroom", "device_id": "bathroom-radar",
        }
        row = self._seed(
            "same-radar-self-confirmation",
            prediction_inputs=[raw],
        )
        self._event(
            same_device_presence, "on",
            event_time=999.6, area="bathroom",
            device_class="presence",
        )
        # Preserve the physical-device identity after the test helper updates area data.
        self.e.entity_registry[same_device_presence]["device_id"] = "bathroom-radar"
        self._event(
            "binary_sensor.hall_motion", "on",
            event_time=999.5, area="hall",
        )
        resolved = self.service.resolve_runtime(
            self.a, self._runtime(row), .15,
            "weak acceptance after settling",
        )
        self.assertEqual(
            resolved["activation_class"],
            "suspected_false_activation",
        )
        rejected = resolved["activation_evidence"][
            "correlated_local_rejected"
        ]
        self.assertEqual(
            rejected[0]["entity_id"], same_device_presence
        )
        self.assertIn(
            "same physical device",
            rejected[0]["rejected_reason"],
        )

    def test_remote_motion_after_short_observer_window_is_not_attributed(self):
        row = self._seed("remote-late")
        self._event(
            "binary_sensor.hall_motion", "on",
            event_time=1030.0, area="hall",
        )
        resolved = self.service.resolve_runtime(
            self.a, self._runtime(row), .15,
            "weak acceptance after settling",
        )
        self.assertEqual(resolved["activation_class"], "unknown")
        self.assertIsNone(resolved["activation_source_entity_id"])

    def test_same_area_presence_confirms_use_and_keeps_existing_reward_contract(self):
        row = self._seed("local-confirmed")
        event_id = self._event(
            "binary_sensor.bathroom_pir", "on",
            event_time=1002.0, area="bathroom",
        )
        # Trusted anticipation remains the existing Stage-6 reward path.
        row["prediction_inputs"] = ["binary_sensor.bathroom_pir"]
        with self.service.store.lock, self.service.store.conn() as c:
            c.execute(
                """UPDATE automatic_reward_experiences
                   SET prediction_inputs_json=?
                   WHERE resolution_key=?""",
                ('["binary_sensor.bathroom_pir"]', row["resolution_key"]),
            )
        resolved = self.service.resolve_runtime(
            self.a, self._runtime(row), .6, "anticipation outcome"
        )
        self.assertEqual(resolved["status"], "trusted")
        self.assertEqual(resolved["trusted_reward"], .6)
        self.assertEqual(resolved["source_event_id"], event_id)
        self.assertEqual(resolved["activation_class"], "confirmed_use")
        self.assertEqual(
            resolved["activation_source_entity_id"],
            "binary_sensor.bathroom_pir",
        )

    def test_explicit_target_user_reversal_marks_verified_false_activation(self):
        row = self._seed("manual-reversal")
        self._event(
            "binary_sensor.hall_motion", "on",
            event_time=999.5, area="hall",
        )
        self._event(
            self.a["target_entity"], "off",
            event_time=1001.0, area="bathroom",
            origin="user", user_id="human", device_class=None,
        )
        resolved = self.service.resolve_runtime(
            self.a, self._runtime(row), -1.0,
            "manual correction", user_id="human",
        )
        self.assertEqual(resolved["status"], "trusted")
        self.assertEqual(resolved["trusted_reward"], -1.0)
        self.assertEqual(
            resolved["activation_class"],
            "verified_false_activation",
        )
        self.assertEqual(resolved["activation_confidence"], 1.0)
        self.assertEqual(
            resolved["activation_source_entity_id"],
            "binary_sensor.hall_motion",
        )
        self.assertEqual(
            resolved["activation_evidence"]["reward_effect"],
            "none_beyond_existing_manual_correction",
        )
        scores = false_activation_suppressor_scores(
            self.service.store, self.a["id"]
        )
        self.assertGreaterEqual(
            scores["binary_sensor.hall_motion"], .65
        )

    def test_off_action_is_not_classified_as_false_activation(self):
        row = self._seed("off-action", action_value=0.0)
        self._event(
            "binary_sensor.hall_motion", "on",
            event_time=1002.0, area="hall",
        )
        resolved = self.service.resolve_runtime(
            self.a, self._runtime(row), .15,
            "weak acceptance after settling",
        )
        self.assertIsNone(resolved["activation_class"])
        self.assertEqual(
            self.service.summary(self.a["id"])["activation_counts"][
                "suspected_false_activation"
            ],
            0,
        )


class FalseActivationSourceContracts(unittest.TestCase):
    def test_observer_does_not_gain_learning_or_physical_authority(self):
        source = (
            ROOT / "adaptive_ai" / "src"
            / "automatic_correct_rewards.py"
        ).read_text(encoding="utf-8")
        self.assertIn("suspected_false_activation", source)
        self.assertIn('"observer_only": True', source)
        self.assertIn('"reward_effect": "none"', source)
        self.assertNotIn("policy.update(", source)
        self.assertNotIn("save_model(", source)
        self.assertNotIn("executor._service(", source)

    def test_ui_exposes_false_activation_diagnostics(self):
        source = (
            ROOT / "adaptive_ai" / "src" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("False activation observer:", source)
        self.assertIn("False activation episodes:", source)
        self.assertIn("observer only / no reward", source)

    def test_history_uses_suppressor_only_for_feature_relevance(self):
        source = (
            ROOT / "adaptive_ai" / "src" / "history.py"
        ).read_text(encoding="utf-8")
        self.assertIn("false_activation_suppressor_scores", source)
        self.assertIn("feature-selection context only", source)
        self.assertIn("context_suppressor_relevance", source)


if __name__ == "__main__":
    unittest.main()
