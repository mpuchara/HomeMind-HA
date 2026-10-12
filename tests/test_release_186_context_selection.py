import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import agent, state
from context import TemporalHistory
from lighting_conditions import condition_tree, lighting_context
from observation_contract import FeatureSchemaV12
from training_context_selection import assess, select_training_context

TARGET = "switch.bathroom"
ENERGY = "sensor.bathroom_stationary_energy"
LIGHT = "sensor.bathroom_light"
DISTANCE = "sensor.bathroom_stationary_target_distance"


def config(gated=False):
    return {"automation_baseline_entities": [ENERGY] + ([LIGHT] if gated else []),
            "automation_baseline_automations": [
                {"enabled": True, "action_services": ["switch.turn_on"],
                 "baseline_rules": [{"source": "trigger", "kind": "numeric_state", "entity_id": ENERGY, "above": 22}],
                 "condition_tree": condition_tree([{"condition": "numeric_state", "entity_id": LIGHT, "below": 40}] if gated else []),
                 "direct_on_conditions": True},
                {"enabled": True, "action_services": ["switch.turn_off"],
                 "baseline_rules": [{"source": "trigger", "kind": "numeric_state", "entity_id": ENERGY, "below": 12}]}]}


def history(dependent=False, constant=False, origin="automation", late=False):
    rows = []
    for cycle in range(48):
        t = 1700001000 + cycle * 120
        bright = cycle % 2 == 1
        ambient = 20 if constant or not bright else 150
        values = [(t-20, TARGET, "off"), (t-10, ENERGY, 8),
                  (t-5, LIGHT, ambient), (t, ENERGY, 55)]
        if not dependent or not bright:
            values += [(t+1, TARGET, "on"), (t+2, LIGHT, 200)]
        values += [(t+60, ENERGY, 8), (t+70, TARGET, "off")]
        for ts, eid, value in values:
            rows.append(dict(id=len(rows)+1, entity_id=eid, state=str(value), ts=ts,
                             received_ts=ts + (100 if late and eid == LIGHT else 0),
                             _provenance={"origin": origin if eid == TARGET and value == "on" else "automation"}))
    return sorted(rows, key=lambda r: (r["ts"], r["id"]))


class Context186Tests(unittest.TestCase):
    def setUp(self):
        self.agent = agent(target_entity=TARGET)
        self.states = {ENERGY: state(ENERGY, 8), LIGHT: state(LIGHT, 20), DISTANCE: state(DISTANCE, 100)}
        self.registry = {eid: {"device_id": "radar"} for eid in self.states}

    def assessment(self, **kwargs):
        return assess(history(**kwargs), self.agent, config(), self.states, end_ts=1700008000)

    def test_lamp_feedback_does_not_create_daylight_dependency(self):
        result = self.assessment(constant=True)
        self.assertGreaterEqual(result["opportunities"], 40)
        self.assertEqual(result["sensors"][LIGHT]["status"], "uninformative_range")

    def test_presence_only_across_bright_and_dark_arrivals(self):
        self.assertEqual(self.assessment()["sensors"][LIGHT]["status"], "presence_only_in_observed_range")

    def test_conditioned_brightness_association_is_replicated(self):
        item = self.assessment(dependent=True)["sensors"][LIGHT]
        self.assertEqual(item["status"], "dependent")
        self.assertEqual(item["low_on_rate"], 1)
        self.assertEqual(item["high_on_rate"], 0)

    def test_unequal_day_and_night_coverage_can_show_dependency(self):
        from training_context_selection import _association
        samples = [(i, float(i % 4 != 0), int(i % 4 == 0)) for i in range(48)]
        self.assertEqual(_association(samples)["status"], "dependent")

    def test_late_photometry_cannot_become_a_precursor(self):
        self.assertEqual(self.assessment(dependent=True, late=True)["sensors"][LIGHT]["samples"], 0)

    def test_sub_100ms_packet_still_cannot_appear_before_receipt(self):
        rows = history(dependent=True)
        for row in rows:
            if row["entity_id"] == LIGHT and row["state"] != "200":
                row["ts"] += 4.99
                row["received_ts"] = row["ts"] + .09
        rows.sort(key=lambda row: row["ts"])
        result = assess(rows, self.agent, config(), self.states, end_ts=1700008000)
        # A still-valid older reading is allowed; the alternating *new* values
        # were not received at arrival and cannot certify a dependency.
        self.assertNotEqual(result["sensors"][LIGHT]["status"], "dependent")

    def test_target_received_after_selection_cutoff_is_not_an_outcome(self):
        rows = history()
        for row in rows:
            if row["entity_id"] == TARGET and row["state"] == "on":
                row["received_ts"] = row["ts"] + .08
        result = assess(rows, self.agent, config(), self.states, end_ts=1700001001.05)
        self.assertEqual(result["opportunities"], 0)

    def test_no_new_light_reading_after_off_does_not_reuse_lamp_emission(self):
        rows = [row for row in history() if not (row["entity_id"] == LIGHT and row["state"] != "200")]
        result = assess(rows, self.agent, config(), self.states, end_ts=1700008000)
        self.assertEqual(result["sensors"][LIGHT]["samples"], 0)

    def test_missing_numeric_automation_is_not_replaced_by_boolean_energy(self):
        meta = {"primary_occupancy_sensor": ENERGY}
        result = assess(history(dependent=True), self.agent, meta, self.states, end_ts=1700008000)
        self.assertIsNone(result["driver"])
        self.assertEqual(result["opportunities"], 0)

    def test_assessment_budget_stops_streaming(self):
        with patch("training_context_selection.MAX_ROWS", 25):
            result = self.assessment(dependent=True)
        self.assertTrue(result["bounded"])
        self.assertEqual(result["rows"], 25)
        self.assertNotEqual(result["sensors"][LIGHT]["status"], "dependent")

    def test_user_and_own_actions_do_not_certify_dependency(self):
        for origin in ("user", "user_intent", "own_command"):
            with self.subTest(origin=origin):
                self.assertNotEqual(self.assessment(dependent=True, origin=origin)["sensors"][LIGHT]["status"], "dependent")

    def test_validation_future_cannot_change_feature_selection(self):
        prefix = history(constant=True)
        future = [{**row, "ts": row["ts"] + 10000, "received_ts": row["received_ts"] + 10000} for row in history(dependent=True)]
        first = assess(prefix, self.agent, config(), self.states, end_ts=1700008000)
        second = assess(prefix + future, self.agent, config(), self.states, end_ts=1700008000)
        self.assertEqual(first["sensors"], second["sensors"])

    def selection(self, *, gated=False, dependent=False, additional=None):
        a = {**self.agent, "additional_signal": additional}
        meta = config(gated)
        policy = SimpleNamespace(dims=128, total_updates=0, selection_meta=meta,
                                 schema=FeatureSchemaV12(128, [ENERGY, LIGHT]), excluded_context_entities=set())
        store = SimpleNamespace(archive_iter=lambda **kwargs: iter(history(dependent=dependent)))
        with patch("long_memory.selected_history_provenance", return_value={}):
            select_training_context(policy, a, self.states, self.registry, store, 1700000900, 1700008000)
        return policy

    def test_rebuild_drops_unproven_light_and_keeps_extra_radar_channel(self):
        policy = self.selection()
        self.assertEqual(policy.schema.entities, [ENERGY, DISTANCE])
        self.assertIn(LIGHT, policy.selection_meta["training_context_assessment"]["excluded"])

    def test_proven_brightness_is_selected(self):
        self.assertIn(LIGHT, self.selection(dependent=True).schema.entities)

    def test_explicit_automation_brightness_is_preserved_without_evidence(self):
        self.assertIn(LIGHT, self.selection(gated=True).schema.entities)

    def test_additional_sensor_is_preserved(self):
        self.assertIn(LIGHT, self.selection(additional={"entity_id": LIGHT}).schema.entities)

    def test_next_checkpoint_never_reorders_trained_slots(self):
        policy = self.selection()
        original = list(policy.schema.entities)
        store = SimpleNamespace(archive_iter=lambda **kwargs: self.fail("Repeated assessment"))
        self.assertFalse(select_training_context(policy, self.agent, self.states, self.registry, store, 1700008000, 1700009000))
        self.assertEqual(original, policy.schema.entities)
        policy.selection_meta.pop("training_context_assessment")
        policy.total_updates = 10
        self.assertFalse(select_training_context(policy, self.agent, self.states, self.registry, store, 1700008000, 1700009000))

    def test_cached_checkpoint_keeps_assessment_but_explicit_rebuild_rescreens(self):
        import threading
        import ha
        from history import HistoryManager
        from observation_contract import install_training_contract
        contract = install_training_contract()
        try:
            manager = object.__new__(HistoryManager)
            manager.training_schema_cache = {}
            manager.engine = SimpleNamespace(models={}, lock=threading.RLock(),
                                             state_map=self.states, entity_registry=self.registry)
            raw = {"version": 11, "dims": 128, "actions": [0, 1], "horizons": [1],
                   "schema": FeatureSchemaV12(128, [ENERGY, DISTANCE]).export(),
                   "selection_meta": {**config(), "training_context_assessment": {"version": 1}}, "heads": {}}
            with patch.object(ha.AUTOMATION_KNOWLEDGE, "hints_for_target", return_value=({ENERGY}, [])):
                checkpoint = manager._remember_training_schema(self.agent, raw)
                rebuild = manager._remember_training_schema(self.agent, raw, rescreen=True)
            self.assertNotIn(LIGHT, checkpoint["schema"]["entities"])
            self.assertIn("training_context_assessment", checkpoint["selection_meta"])
            self.assertNotIn("training_context_assessment", rebuild["selection_meta"])
            self.assertIn(LIGHT, rebuild["schema"]["entities"])
        finally:
            contract["restore"]()

    def test_manual_input_list_is_not_reselected(self):
        policy = self.selection()
        policy.selection_meta.pop("training_context_assessment")
        manual = {**self.agent, "input_entities": [ENERGY, LIGHT]}
        self.assertFalse(select_training_context(policy, manual, self.states, self.registry, None, 1700000900, 1700008000))

    def test_new_reward_and_features_share_pre_action_ambient(self):
        meta = config(True)
        meta["training_context_assessment"] = dict(version=1, target_entity=TARGET, target_property="power")
        temporal = TemporalHistory()
        temporal.add(TARGET, 1700000010, state(TARGET, "off"))
        temporal.add(LIGHT, 1700000011, state(LIGHT, 20))
        temporal.add(TARGET, 1700000012, state(TARGET, "on"))
        temporal.add(LIGHT, 1700000013, state(LIGHT, 200))
        states = {TARGET: state(TARGET, "on"), LIGHT: state(LIGHT, 200)}
        self.assertTrue(lighting_context(meta, states, 1700000014, temporal)["need"])
        self.assertFalse(lighting_context(config(True), states, 1700000014, temporal)["need"])

    def test_unresolved_on_baseline_remains_unknown(self):
        meta = config(True)
        meta["training_context_assessment"] = dict(version=1, target_entity=TARGET, target_property="power")
        self.assertIsNone(lighting_context(meta, {TARGET: state(TARGET, "on"), LIGHT: state(LIGHT, 200)}, 1700000014, TemporalHistory())["need"])


if __name__ == "__main__":
    unittest.main()
