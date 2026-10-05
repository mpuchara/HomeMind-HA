import unittest
from types import SimpleNamespace

from training_quality import ConditionalPatternMemory, sensor_snapshot, light_dwell_reward
from replay import SQLiteTemporalTracker


class PatternTests(unittest.TestCase):
    signature = [("binary_sensor.radar", 1)]

    def memory(self):
        return ConditionalPatternMemory(2)

    def test_context_majority_softens_minorities_after_independent_days(self):
        memory = self.memory()
        for day in range(3):
            memory.observe(self.signature, 1, day * 86400 + 100, day * 86400 + 200, 1)
        self.assertEqual(memory.factor(self.signature, 1, 4 * 86400, "automation"), 1)
        self.assertLess(memory.factor(self.signature, 0, 4 * 86400, "automation"), 1)
        self.assertEqual(memory.factor(self.signature, 0, 4 * 86400, "manual_feedback"), 1)

    def test_one_chatty_day_cannot_create_recurrence(self):
        memory = self.memory()
        for second in range(1000):
            memory.observe(self.signature, 1, second, second + 1, 1)
        self.assertEqual(memory.factor(self.signature, 0, 2000, "unknown"), 1)
        self.assertEqual(len(next(iter(memory.rows.values()))), 1)

    def test_future_outcomes_cannot_change_past_weight(self):
        memory = self.memory()
        for day in range(4):
            memory.observe(self.signature, 1, day * 86400, 10 * 86400, 1)
        self.assertEqual(memory.factor(self.signature, 0, 5 * 86400, "unknown"), 1)
        self.assertLess(memory.factor(self.signature, 0, 11 * 86400, "unknown"), 1)

    def test_missing_context_negative_outcomes_and_other_context_do_not_vote(self):
        memory = self.memory()
        for day in range(4):
            memory.observe([], 1, day * 86400, day * 86400 + 1, 1)
            memory.observe(self.signature, 1, day * 86400, day * 86400 + 1, -1)
        self.assertEqual(memory.status()["contexts"], 0)
        memory.observe([("other", 1)], 1, 0, 1, 1)
        self.assertEqual(memory.factor(self.signature, 0, 500000, "automation"), 1)

    def test_checkpoint_and_memory_caps(self):
        memory = ConditionalPatternMemory(2, max_contexts=2, max_events=3)
        for index in range(10):
            memory.observe([("x", index)], 1, index * 86400, index * 86400 + 1, 1)
        self.assertEqual(len(memory.rows), 2)
        restored = ConditionalPatternMemory(2, memory.export(), max_contexts=2, max_events=3)
        self.assertEqual(restored.export(), memory.export())
        self.assertEqual(ConditionalPatternMemory(3, memory.export()).status()["contexts"], 0)

    def test_corrupt_or_missing_context_cannot_supply_votes(self):
        memory = self.memory()
        state = memory.export()
        state["rows"] = [None, ["context", [None, ["nan", 1, 1], [1, 1, 99]]]]
        restored = ConditionalPatternMemory(2, state)
        self.assertEqual(restored.factor(self.signature, 0, 100, "automation"), 1)
        restored.observe([("missing", None)], 1, 0, 1, 1)
        self.assertIsNone(restored.key([("missing", None)]))


class LightOutcomeTests(unittest.TestCase):
    agent = {"target_entity": "light.room", "target_property": "power"}
    registry = {"light.room": {"area_id": "room"}, "binary_sensor.radar": {"area_id": "room"},
                "binary_sensor.motion": {"area_id": "room"}}

    def snapshot(self, radar="off", motion="off"):
        states = {
            "binary_sensor.radar": {"state": radar, "attributes": {"device_class": "occupancy"}},
            "binary_sensor.motion": {"state": motion, "attributes": {"device_class": "motion"}},
        }
        return sensor_snapshot(self.agent, list(states), states, self.registry)

    def test_stationary_person_is_not_vacancy(self):
        snapshot = self.snapshot("on", "off")
        reward, reason = light_dwell_reward(0, .8, snapshot, snapshot, False)
        self.assertEqual(reward, -1)
        self.assertEqual(reason, "premature_off_confirmed_presence")

    def test_motion_off_and_sensor_failure_do_not_prove_false_activation(self):
        for radar in ("unavailable", "unknown"):
            snapshot = self.snapshot(radar, "off")
            self.assertEqual(light_dwell_reward(1, .8, snapshot, snapshot, False)[0], .8)

    def test_verified_vacancy_penalizes_unneeded_on(self):
        snapshot = self.snapshot()
        self.assertEqual(light_dwell_reward(1, .8, snapshot, snapshot, False)[0], -.6)

    def test_arrival_during_anticipation_keeps_positive_evidence(self):
        self.assertGreater(light_dwell_reward(1, .8, self.snapshot(), self.snapshot("on"), True)[0], 0)

    def test_sensing_gap_does_not_become_negative_reward(self):
        snapshot = self.snapshot()
        self.assertEqual(light_dwell_reward(1, .8, snapshot, snapshot, False,
                                           observation_complete=False)[0], .8)

    def test_explicit_user_exception_and_sensor_conflict_are_preserved(self):
        snapshot = self.snapshot()
        self.assertEqual(light_dwell_reward(1, 1, snapshot, snapshot, False, explicit_user=True)[0], 1)
        conflict = self.snapshot("off", "on")
        self.assertEqual(light_dwell_reward(1, .8, conflict, snapshot, False)[0], 0)

    def test_other_room_cannot_certify_presence_or_absence(self):
        registry = {**self.registry, "binary_sensor.radar": {"area_id": "elsewhere"}}
        snapshot = sensor_snapshot(self.agent, ["binary_sensor.radar"],
                                   {"binary_sensor.radar": {"state": "on", "attributes": {"device_class": "occupancy"}}}, registry)
        self.assertEqual(snapshot["active"], [])
        self.assertEqual(snapshot["reliable"], [])

    def test_interval_gap_is_detected_by_production_tracker(self):
        tracker = SQLiteTemporalTracker.__new__(SQLiteTemporalTracker)
        tracker._closed = True
        tracker._base_interval_rows = lambda *args, **kw: [{"entity_id": "binary_sensor.radar", "state": "unavailable", "ts": 1700000000, "attributes_json": "{}"}]
        self.assertFalse(tracker.observation_known_between(["binary_sensor.radar"], 1, 2))
