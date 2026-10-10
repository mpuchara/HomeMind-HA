"""Occupied daylight, raw radar photometry and condition structure regressions."""
import math
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import agent, state
from lighting_conditions import condition_tree, direct_on_conditions, lighting_context
from radar_context import radar_context_entities, radar_role
from context import TemporalHistory, entity_capability_tags
from training_quality import light_dwell_reward, sensor_snapshot
from fast_runtime import fast_light_on_assist_action
from observation_contract import FeatureSchemaV12, _feature_observation, install_training_contract

LIGHT = "sensor.radar_light"
ENERGY = "sensor.radar_stationary_energy"
PRESENCE = "binary_sensor.radar_has_target"
TARGET = "light.kitchen"


def metadata(conditions, **overrides):
    info = dict(entity_id="automation.kitchen_on", enabled=True,
                action_services=["light.turn_on"], context_entities=[LIGHT, ENERGY],
                condition_tree=condition_tree(conditions), direct_on_conditions=True)
    info.update(overrides)
    return {"automation_baseline_automations": [info], "automation_baseline_candidates": [LIGHT, ENERGY]}


class Lighting167Tests(unittest.TestCase):
    def gate(self, raw, conditions=None, **overrides):
        conditions = conditions if conditions is not None else [{"condition": "numeric_state", "entity_id": LIGHT, "below": 40}]
        return lighting_context(metadata(conditions), {LIGHT: state(LIGHT, raw), **overrides})

    def test_strict_threshold_dark_bright_and_unavailable(self):
        for raw, expected in [(0, True), (39, True), (40, False), (200, False), ("unknown", None), ("unavailable", None), ("nan", None)]:
            self.assertIs(self.gate(raw)["need"], expected)

    def test_dynamic_bounds_attributes_and_invalid_bounds(self):
        rules = [{"condition": "numeric_state", "entity_id": LIGHT, "below": "number.radar_threshold"}]
        self.assertFalse(self.gate(100, rules, **{"number.radar_threshold": state("number.radar_threshold", 40)})["need"])
        self.assertIsNone(self.gate(20, rules)["need"])
        rules[0]["below"] = "{{ invalid }}"
        self.assertIsNone(self.gate(20, rules)["need"])
        rules[0].update(below=40, attribute="level")
        self.assertTrue(lighting_context(metadata(rules), {LIGHT: state(LIGHT, "ok", level=20)})["need"])

    def test_boolean_or_not_and_do_not_flatten(self):
        low = {"condition": "numeric_state", "entity_id": LIGHT, "below": 40}
        high = {"condition": "numeric_state", "entity_id": LIGHT, "above": 100}
        self.assertTrue(self.gate(200, [{"condition": "or", "conditions": [low, high]}])["need"])
        self.assertFalse(self.gate(60, [{"condition": "or", "conditions": [low, high]}])["need"])
        self.assertFalse(self.gate(20, [{"condition": "not", "conditions": [low]}])["need"])
        unrelated = {"condition": "state", "entity_id": "input_boolean.override", "state": "on"}
        self.assertIsNone(self.gate(200, [{"condition": "or", "conditions": [low, unrelated]}])["need"])
        mixed = {"condition": "and", "conditions": [low, unrelated]}
        self.assertIsNone(self.gate(20, [{"condition": "not", "conditions": [mixed]}])["need"])

    def test_unreadable_legacy_templates_and_branched_actions_are_unknown(self):
        for data in [metadata([], condition_tree=None), metadata([], direct_on_conditions=False),
                     metadata([{"condition": "template", "value_template": "{{ true }}"}]),
                     metadata([{"condition": "numeric_state", "entity_id": LIGHT, "below": 40, "value_template": "{{ value }}"}])]:
            self.assertIsNone(lighting_context(data, {LIGHT: state(LIGHT, 200)})["need"])
        self.assertFalse(direct_on_conditions([{"choose": []}]))
        self.assertFalse(direct_on_conditions([{"service": "light.turn_on"}, {"service": "light.turn_off"}]))

    def test_bathroom_ungated_and_disabled_controller_takeover(self):
        data = metadata([])
        self.assertTrue(lighting_context(data, {LIGHT: state(LIGHT, 200)})["need"])
        data = metadata([{"condition": "numeric_state", "entity_id": LIGHT, "below": 40}], enabled=False)
        self.assertFalse(lighting_context(data, {LIGHT: state(LIGHT, 200)})["need"])

    def test_alternative_ungated_on_does_not_assert_brightness_block(self):
        data = metadata([{"condition": "numeric_state", "entity_id": LIGHT, "below": 40}])
        data["automation_baseline_automations"].append(metadata([])["automation_baseline_automations"][0])
        self.assertTrue(lighting_context(data, {LIGHT: state(LIGHT, 200)})["need"])

    def test_occupied_daylight_off_is_positive_training_evidence(self):
        states = {PRESENCE: state(PRESENCE, "on", device_class="occupancy"), LIGHT: state(LIGHT, 200)}
        reg = {e: {"area_id": "kitchen"} for e in [TARGET, PRESENCE, LIGHT]}
        snap = sensor_snapshot(agent(), list(states), states, reg, lighting=self.gate(200))
        self.assertEqual(light_dwell_reward(0, .8, snap, snap, False), (.8, "occupied_daylight_off"))
        self.assertNotIn(LIGHT, snap["radar"])
        dark = sensor_snapshot(agent(), list(states), states, reg, lighting=self.gate(20))
        self.assertEqual(light_dwell_reward(0, .8, dark, dark, False)[0], -1)
        self.assertEqual(light_dwell_reward(0, .8, dark, dark, False, explicit_user=True)[0], .8)
        unknown = sensor_snapshot(agent(), list(states), states, reg, lighting=self.gate("unknown"))
        self.assertEqual(light_dwell_reward(0, .8, unknown, unknown, False)[0], .8)

    def test_radar_light_bundle_is_local_and_never_occupancy(self):
        states = {e: state(e, 20) for e in [LIGHT, ENERGY, "sensor.other_light"]}
        self.assertEqual(radar_role(LIGHT), "illumination")
        self.assertEqual(entity_capability_tags(LIGHT, states[LIGHT]), {"illuminance"})
        self.assertEqual(radar_context_entities(agent(), [ENERGY], states, {}), [LIGHT])
        reg = {ENERGY: {"device_id": "a"}, LIGHT: {"device_id": "b"}}
        self.assertNotIn(LIGHT, radar_context_entities(agent(), [ENERGY], states, reg))

    def test_on_assist_cannot_override_bright_or_unknown_lighting(self):
        forecast = {"known": True, "occupancy_now": 1, "evidence_sources": [dict(
            entity_id=e, role="radar_occupancy", available=True, contribution=1,
            communication_reliability=1, evidence_freshness=1) for e in ("a", "b")]}
        arms = [{"index": 0, "value": 0, "mean": .5}, {"index": 1, "value": 1, "ucb": .6}]
        args = (agent(), 0, 0, "historical_policy_bootstrap", arms, forecast)
        self.assertEqual(fast_light_on_assist_action(*args, lighting=self.gate(20)), 1)
        self.assertIsNone(fast_light_on_assist_action(*args, lighting=self.gate(200)))
        self.assertIsNone(fast_light_on_assist_action(*args, lighting=self.gate("unknown")))

    def test_raw_light_contract_resolution_and_legacy_roundtrip(self):
        for contract in (1, 2, 3, 4):
            schema = FeatureSchemaV12(128, [LIGHT], contract)
            self.assertEqual(FeatureSchemaV12.from_export(schema.export(), 128).feature_contract_version, contract)
        def observation(raw, contract=4):
            states = {LIGHT: state(LIGHT, raw), TARGET: state(TARGET, "off")}
            return _feature_observation(FeatureSchemaV12(128, [LIGHT], contract), LIGHT, states[LIGHT], agent(), states, TemporalHistory(), 1800000000, 1800000000)[0]
        self.assertGreater(observation(100)["value"] - observation(20)["value"], .25)
        self.assertAlmostEqual(observation(20, 3)["value"], math.tanh(2))
        self.assertNotEqual(observation(20)["canonical_unit"], "lx")

    def test_emitted_light_is_frozen_before_action_and_future_samples_excluded(self):
        at = 1800000000
        temporal = TemporalHistory()
        for eid, when, value in [(TARGET, at-20, "off"), (LIGHT, at-10, 20),
                                  (TARGET, at, "on"), (LIGHT, at+1, 200), (LIGHT, at+10, 250)]:
            temporal.add(eid, when, state(eid, value))
        states = {TARGET: state(TARGET, "on"), LIGHT: state(LIGHT, 200)}
        obs, detail = _feature_observation(FeatureSchemaV12(128, [LIGHT]), LIGHT, states[LIGHT], agent(), states, temporal, at+2, at+2)
        self.assertEqual(obs["physical_value"], 20)
        self.assertEqual(detail["source"], "pre_action_baseline")

    def test_ha_scan_copies_boolean_tree_into_policy_diagnostics(self):
        import ha
        from fast_local_primary import _automation_diagnostics
        rules = [{"condition": "numeric_state", "entity_id": LIGHT, "below": 40}]
        cfg = {"actions": [{"action": "light.turn_on", "target": {"entity_id": TARGET}}], "conditions": rules}
        knowledge = ha.AutomationKnowledge()
        knowledge.cached_infos = {}
        with patch.object(ha.HA, "automation_config", return_value=cfg), patch.object(ha.STORE, "meta_set"), patch.object(ha.STORE, "event"):
            knowledge.scan({"automation.kitchen": state("automation.kitchen", "on", id="123")}, force=True)
        _, infos = knowledge.hints_for_target(TARGET)
        data = {"automation_baseline_automations": _automation_diagnostics(infos)}
        self.assertFalse(lighting_context(data, {LIGHT: state(LIGHT, 200)})["need"])
