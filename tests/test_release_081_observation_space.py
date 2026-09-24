"""0.14.81 Stage-2 observation-space, mask and historical causality contracts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import context as context_module
from context import HistoricalTemporalTracker, TemporalHistory
from context_engine import ContextEngine
from observation_contract import FeatureJournal, ObservationSQLiteTemporalTracker
from observation_space import (
    ENTITY_DESCRIPTORS,
    GLOBAL_FEATURES,
    HOME_FEATURES,
    ObservationMask,
    global_observation_catalog,
    observation_as_of,
    observation_schema_id,
    select_observation_mask,
)
from policy import MultiHorizonPolicy
from settings import DEFAULT_OPTIONS
from storage import Store
from support import agent, state


BASE = 1_700_400_000.0


def archived(eid, ts, value, **attrs):
    return {
        "entity_id": eid,
        "ts": float(ts),
        "state": str(value),
        "attributes_json": json.dumps(attrs),
        "context_user_id": None,
    }


class ObservationSpaceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.target = "light.kitchen"
        self.motion = "binary_sensor.kitchen_motion"
        self.radar = "sensor.kitchen_stationary_energy"
        self.states = {
            self.target: state(self.target, "off"),
            self.motion: state(self.motion, "off", device_class="motion"),
            self.radar: state(
                self.radar, "18", device_class="signal_strength",
                unit_of_measurement="%",
            ),
        }
        self.registry = {
            self.target: {"area_id": "kitchen", "device_id": "lamp"},
            self.motion: {"area_id": "kitchen", "device_id": "pir"},
            self.radar: {"area_id": "kitchen", "device_id": "radar"},
        }
        self.agent = agent(target_entity=self.target, input_entities=["*"])

    def test_global_catalog_scales_six_descriptors_per_eligible_entity(self):
        states = {}
        registry = {}
        for idx in range(100):
            eid = f"binary_sensor.context_{idx:03d}"
            states[eid] = state(eid, "off", device_class="motion")
            registry[eid] = {"area_id": f"area_{idx % 4}"}
        states["light.target"] = state("light.target", "off")
        registry["light.target"] = {"area_id": "area_0", "device_id": "lamp"}
        states["switch.other"] = state("switch.other", "off")
        registry["switch.other"] = {"area_id": "area_0", "device_id": "relay"}
        states["sensor.power"] = state(
            "sensor.power", "220", device_class="power", unit_of_measurement="W"
        )
        registry["sensor.power"] = {"area_id": "area_0"}

        catalog = global_observation_catalog(states, registry)
        self.assertEqual(len(ENTITY_DESCRIPTORS), 6)
        self.assertEqual(catalog["eligible_entity_count"], 100)
        self.assertEqual(
            catalog["feature_count"],
            100 * 6 + len(GLOBAL_FEATURES) + len(HOME_FEATURES),
        )
        entity_ids = {
            row["entity_id"] for row in catalog["features"] if row["entity_id"]
        }
        self.assertNotIn("light.target", entity_ids)
        self.assertNotIn("switch.other", entity_ids)
        self.assertNotIn("sensor.power", entity_ids)

    def test_selector_is_deterministic_bounded_and_preserves_provenance(self):
        extra_states = dict(self.states)
        extra_registry = dict(self.registry)
        for idx in range(24):
            eid = f"binary_sensor.extra_{idx:02d}"
            extra_states[eid] = state(eid, "on" if idx % 2 else "off", device_class="motion")
            extra_registry[eid] = {"area_id": "kitchen" if idx < 10 else "hall"}

        relevance = {self.motion: .95, self.radar: .91}
        first, first_diag = select_observation_mask(
            self.agent, extra_states, extra_registry, [self.motion],
            relevance_scores=relevance,
        )
        second, second_diag = select_observation_mask(
            self.agent, extra_states, extra_registry, [self.motion],
            relevance_scores=relevance,
        )

        self.assertEqual(first.mask_id, second.mask_id)
        self.assertEqual(first.feature_ids, second.feature_ids)
        # Fast reactive targets intentionally keep at most eight context entities:
        # 11 global/home features + 8 * 6 entity descriptors = 59.
        self.assertEqual(first_diag["selected_feature_count"], 59)
        self.assertLessEqual(first_diag["selected_feature_count"], 128)
        self.assertGreaterEqual(first_diag["selected_feature_count"], 32)
        self.assertEqual(first_diag["schema_id"], observation_schema_id())
        self.assertFalse(first_diag["hot_path_active"])

        entity_rows = [row for row in first.features if row.get("entity_id")]
        self.assertTrue(entity_rows)
        self.assertTrue(all("score" in row for row in entity_rows))
        self.assertTrue(all(row.get("selection_reason") for row in entity_rows))
        self.assertTrue(all("area_id" in row for row in entity_rows))
        self.assertFalse(any(row.get("entity_id") == self.target for row in entity_rows))

        home_rows = [row for row in first.features if row.get("kind") == "home"]
        self.assertTrue(home_rows)
        self.assertTrue(all(row.get("area_id") == "kitchen" for row in home_rows))
        self.assertTrue(all(row.get("target_entity") == self.target for row in home_rows))

    def test_stage2_mask_is_stable_across_wall_clock_time_for_same_snapshot(self):
        states = copy.deepcopy(self.states)
        for idx, eid in enumerate(sorted(states)):
            states[eid]["last_changed"] = BASE + idx
            states[eid]["last_updated"] = BASE + idx
        original_now = context_module.now_ts
        try:
            context_module.now_ts = lambda: BASE + 1_000
            first, first_diag = select_observation_mask(
                self.agent, states, self.registry, [self.motion],
                relevance_scores={self.motion: .95, self.radar: .91},
            )
            context_module.now_ts = lambda: BASE + 1_000_000
            second, second_diag = select_observation_mask(
                self.agent, states, self.registry, [self.motion],
                relevance_scores={self.motion: .95, self.radar: .91},
            )
        finally:
            context_module.now_ts = original_now
        self.assertEqual(first.mask_id, second.mask_id)
        self.assertEqual(first.feature_ids, second.feature_ids)
        self.assertEqual(
            first_diag["selection_reference_ts"],
            second_diag["selection_reference_ts"],
        )
        self.assertEqual(
            first_diag["selection_reference_ts"],
            max(BASE + idx for idx, _eid in enumerate(sorted(states))),
        )

    def test_mask_roundtrip_and_incompatibility_detection(self):
        mask, _ = select_observation_mask(
            self.agent, self.states, self.registry, [self.motion],
            relevance_scores={self.motion: .9},
        )
        raw = mask.export()
        restored = ObservationMask.from_export(raw)
        self.assertEqual(restored.mask_id, mask.mask_id)
        self.assertEqual(restored.feature_ids, mask.feature_ids)

        wrong_schema = copy.deepcopy(raw)
        wrong_schema["schema_id"] = "obs-v999:wrong"
        with self.assertRaisesRegex(ValueError, "incompatible observation feature schema"):
            ObservationMask.from_export(wrong_schema)

        wrong_payload = copy.deepcopy(raw)
        wrong_payload["feature_ids"] = list(wrong_payload["feature_ids"])
        wrong_payload["feature_ids"][0] = "time:tampered"
        with self.assertRaisesRegex(ValueError, "mask payload mismatch"):
            ObservationMask.from_export(wrong_payload)

        wrong_checksum = copy.deepcopy(raw)
        wrong_checksum["mask_id"] = "bad"
        with self.assertRaisesRegex(ValueError, "mask checksum mismatch"):
            ObservationMask.from_export(wrong_checksum)


class HistoricalObservationTests(unittest.TestCase):
    def setUp(self):
        self.target = "light.kitchen"
        self.motion = "binary_sensor.kitchen_motion"
        self.states = {
            self.target: state(self.target, "off"),
            # Current state deliberately represents the future relative to t=25.
            self.motion: {
                **state(self.motion, "off", device_class="motion"),
                "last_changed": "2023-11-19T13:20:40+00:00",
                "last_updated": "2023-11-19T13:20:40+00:00",
            },
        }
        self.registry = {
            self.target: {"area_id": "kitchen", "device_id": "lamp"},
            self.motion: {"area_id": "kitchen", "device_id": "pir"},
        }
        self.agent = agent(target_entity=self.target, input_entities=["*"])
        self.mask, _ = select_observation_mask(
            self.agent, self.states, self.registry, [self.motion],
            relevance_scores={self.motion: .95},
            max_features=64,
        )
        self.rows = [
            archived(self.motion, BASE + 0, "off", device_class="motion"),
            archived(self.motion, BASE + 20, "on", device_class="motion"),
            archived(self.motion, BASE + 40, "off", device_class="motion"),
            archived(self.motion, BASE + 50, "unavailable", device_class="motion"),
        ]

    def _value(self, result, feature_id):
        return result["values"][result["feature_ids"].index(feature_id)]

    def test_observation_as_of_uses_historical_state_not_current_state(self):
        tracker = HistoricalTemporalTracker(self.rows, [self.motion])
        result = observation_as_of(
            self.mask, self.states, tracker, BASE + 25, self.agent
        )
        self.assertEqual(
            self._value(result, f"entity:{self.motion}:value"),
            1.0,
        )
        self.assertEqual(
            self._value(result, f"entity:{self.motion}:delta_10s"),
            2.0,
        )
        # The state_map says OFF, but t=25 was ON. If current state had leaked this
        # value would be -1 instead of +1.
        self.assertEqual(self.states[self.motion]["state"], "off")

    def test_future_transition_is_invisible_before_its_timestamp(self):
        tracker = HistoricalTemporalTracker(self.rows, [self.motion])
        before = observation_as_of(
            self.mask, self.states, tracker, BASE + 10, self.agent
        )
        after = observation_as_of(
            self.mask, self.states, tracker, BASE + 25, self.agent
        )
        self.assertEqual(
            self._value(before, f"entity:{self.motion}:value"),
            -1.0,
        )
        self.assertEqual(
            self._value(after, f"entity:{self.motion}:value"),
            1.0,
        )

    def test_rewind_reconstructs_same_exact_vector_without_future_leak(self):
        tracker = HistoricalTemporalTracker(self.rows, [self.motion])
        early_a = observation_as_of(
            self.mask, self.states, tracker, BASE + 10, self.agent
        )
        observation_as_of(self.mask, self.states, tracker, BASE + 45, self.agent)
        early_b = observation_as_of(
            self.mask, self.states, tracker, BASE + 10, self.agent
        )
        self.assertEqual(early_b["feature_ids"], early_a["feature_ids"])
        self.assertEqual(early_b["values"], early_a["values"])
        self.assertEqual(early_b["mask_id"], early_a["mask_id"])

    def test_unavailable_source_is_zero_with_explicit_available_flag(self):
        tracker = HistoricalTemporalTracker(self.rows, [self.motion])
        result = observation_as_of(
            self.mask, self.states, tracker, BASE + 55, self.agent
        )
        self.assertEqual(
            self._value(result, f"entity:{self.motion}:available"),
            0.0,
        )
        self.assertEqual(
            self._value(result, f"entity:{self.motion}:value"),
            0.0,
        )
        self.assertIn(
            f"entity:{self.motion}:value",
            result["missing_feature_ids"],
        )
        self.assertGreaterEqual(result["missing_feature_count"], 5)


class ReceivedTimeCausalityTests(unittest.TestCase):
    def test_feature_received_in_future_is_invisible_until_receipt(self):
        temp = tempfile.TemporaryDirectory(prefix="hm-observation-asof-")
        try:
            store = Store(Path(temp.name) / "observation.db")
            target = "light.kitchen"
            motion = "binary_sensor.motion"
            states = {
                target: state(target, "off"),
                motion: state(motion, "off", device_class="motion"),
            }
            registry = {
                target: {"area_id": "kitchen", "device_id": "lamp"},
                motion: {"area_id": "kitchen", "device_id": "pir"},
            }
            a = agent(target_entity=target, input_entities=["*"])
            mask, _ = select_observation_mask(
                a, states, registry, [motion],
                relevance_scores={motion: .95},
                max_features=64,
            )
            store.archive_batch([
                (motion, BASE, "off", {"device_class": "motion"}, None, "test"),
            ])
            journal = FeatureJournal(store)
            journal.record(
                motion,
                state(motion, "on", device_class="motion"),
                event_time=BASE + 10,
                received_time=BASE + 30,
                source="late-test",
                event_key="late-on",
            )
            ctx = ContextEngine(DEFAULT_OPTIONS)
            ctx.configure(states, entities=registry)
            tracker = ObservationSQLiteTemporalTracker(
                store, [motion], ctx, BASE, BASE + 60
            )
            try:
                before = observation_as_of(mask, states, tracker, BASE + 20, a)
                after = observation_as_of(mask, states, tracker, BASE + 35, a)
            finally:
                tracker.close()
            fid = f"entity:{motion}:value"
            before_value = before["values"][before["feature_ids"].index(fid)]
            after_value = after["values"][after["feature_ids"].index(fid)]
            self.assertEqual(before_value, -1.0)
            self.assertEqual(after_value, 1.0)
        finally:
            temp.cleanup()


class RidgeIsolationTests(unittest.TestCase):
    def test_observation_mask_does_not_change_active_ridge_feature_vector(self):
        target = "light.kitchen"
        motion = "binary_sensor.motion"
        states = {
            target: state(target, "off"),
            motion: state(motion, "on", device_class="motion"),
        }
        registry = {
            target: {"area_id": "kitchen", "device_id": "lamp"},
            motion: {"area_id": "kitchen", "device_id": "pir"},
        }
        p = MultiHorizonPolicy(
            agent(target_entity=target, input_entities=["*"]),
            states,
            registry,
            [motion],
        )
        temporal = TemporalHistory()
        temporal.add(motion, BASE, states[motion])
        before = p.features(states, temporal, at_ts=BASE + 1)

        # Mutating Stage-2 diagnostics must not alter DiagonalLinUCB features.
        p.observation_mask = None
        p.observation_diagnostics = {"tampered": True}
        after = p.features(states, temporal, at_ts=BASE + 1)
        self.assertEqual(before, after)

    def test_policy_diagnostics_expose_observation_mask(self):
        target = "light.kitchen"
        motion = "binary_sensor.motion"
        states = {
            target: state(target, "off"),
            motion: state(motion, "on", device_class="motion"),
        }
        registry = {
            target: {"area_id": "kitchen", "device_id": "lamp"},
            motion: {"area_id": "kitchen", "device_id": "pir"},
        }
        p = MultiHorizonPolicy(
            agent(target_entity=target, input_entities=["*"]),
            states,
            registry,
            [motion],
        )
        self.assertEqual(p.diagnostics()["observation_space"]["status"], "not_materialized")
        p.materialize_observation_mask(
            states, registry, [motion], relevance_scores={motion: .9}
        )
        diag = p.diagnostics()["observation_space"]
        self.assertEqual(diag["schema_id"], observation_schema_id())
        self.assertTrue(diag["mask_id"])
        self.assertGreater(diag["global_feature_count"], 0)
        self.assertGreater(diag["selected_feature_count"], 0)
        self.assertIn("features", diag)


if __name__ == "__main__":
    unittest.main()
