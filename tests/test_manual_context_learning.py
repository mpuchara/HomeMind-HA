import time
import unittest
from types import SimpleNamespace

from support import agent, state
from storage import STORE
from manual_context_learning import (
    _candidate_snapshot, _ensure_table, _insert_snapshot, _migrate_schema,
    _SCORE_CACHE, _OBSERVATION_CACHE, manual_scores,
)
from context import ExplicitFeatureSchema
from policy import DiagonalLinUCB


class ManualContextLearningTests(unittest.TestCase):
    def setUp(self):
        _ensure_table(STORE)
        with STORE.lock, STORE.conn() as c:
            c.execute("DELETE FROM manual_context_feedback")
        _SCORE_CACHE.clear()
        _OBSERVATION_CACHE.clear()

    def test_snapshot_keeps_esphome_behaviour_and_excludes_electrical_and_target(self):
        a = agent(target_entity="light.kitchen")
        state_map = {
            "light.kitchen": state("light.kitchen", "on"),
            "sensor.espen4_moving_energy": state("sensor.espen4_moving_energy", "82", unit_of_measurement="%"),
            "sensor.espen4_moving_target_distance": state("sensor.espen4_moving_target_distance", "1.4", unit_of_measurement="m"),
            "sensor.shelly_power": state("sensor.shelly_power", "250", unit_of_measurement="W"),
        }
        snap = _candidate_snapshot(a, state_map, {})
        self.assertIn("sensor.espen4_moving_energy", snap)
        self.assertIn("sensor.espen4_moving_target_distance", snap)
        self.assertNotIn("sensor.shelly_power", snap)
        self.assertNotIn("light.kitchen", snap)

    def test_repeated_manual_corrections_make_hidden_sensor_relevant(self):
        now = time.time()
        labels = [0.0, 0.0, 1.0, 1.0]
        values = [-1.0, -0.8, 0.8, 1.0]
        for i, (desired, value) in enumerate(zip(labels, values)):
            _insert_snapshot(
                STORE, "agent-x", desired, 1.0-desired, "ui_keep_current", "user",
                {
                    "sensor.espen4_hidden_activity": {"v": value, "age": 1.0},
                    "sensor.unrelated": {"v": 0.2 if i % 2 else -0.2, "age": 200.0},
                },
                created_ts=now-i,
            )
        scores = manual_scores(STORE, "agent-x", now=now)
        self.assertGreater(scores.get("sensor.espen4_hidden_activity", 0.0), 0.55)
        self.assertGreater(scores["sensor.espen4_hidden_activity"], scores.get("sensor.unrelated", 0.0))

    def test_one_sided_teaching_is_retained_but_not_promoted_yet(self):
        now = time.time()
        for i in range(6):
            _insert_snapshot(
                STORE, "agent-y", 1.0, 0.0, "ui_keep_current", "user",
                {"sensor.always_high": {"v": 0.9, "age": 1.0}},
                created_ts=now-i,
            )
        self.assertNotIn("sensor.always_high", manual_scores(STORE, "agent-y", now=now))

    def test_schema_migration_preserves_matching_feature_weights(self):
        dims = 128
        old_schema = ExplicitFeatureSchema(dims, ["sensor.old"])
        head = DiagonalLinUCB(dims, [0.0, 1.0])
        old_index = next(i for i, labels in old_schema.labels().items() if labels == ["sensor.old:value"])
        head.a[1][old_index] = 4.0
        head.b[1][old_index] = 3.5
        policy = SimpleNamespace(
            schema=old_schema,
            dims=dims,
            heads={1: head},
            model_revision="old",
            selection_meta={},
        )
        result = _migrate_schema(policy, ["sensor.old", "sensor.new"], {"selected_entities": 2})
        self.assertTrue(result["changed"])
        self.assertIn("sensor.new", result["added"])
        labels = policy.schema.labels()
        new_old_index = next(i for i, value in labels.items() if value == ["sensor.old:value"])
        new_sensor_index = next(i for i, value in labels.items() if value == ["sensor.new:value"])
        self.assertEqual(head.a[1][new_old_index], 4.0)
        self.assertEqual(head.b[1][new_old_index], 3.5)
        self.assertEqual(head.a[1][new_sensor_index], 1.0)
        self.assertEqual(head.b[1][new_sensor_index], 0.0)


if __name__ == "__main__":
    unittest.main()
