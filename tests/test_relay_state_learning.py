import copy
import json
import math
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import ROOT, agent, state
from context import TemporalHistory
from binary_state_classifier import BinaryStateClassifier
from frozen_validation import FrozenRidgeSnapshot
from history import HistoryManager
from observation_contract import FeatureSchemaV12, build_observation_features, install_training_contract
from policy import DiagonalLinUCB
from policy_tiny_mlp_training import evaluate_supervised
from radar_context import radar_context_entities

TARGET = "switch.shellyplus1pm_441793a613bc_switch_0"
ENERGY = "sensor.espen4_stationary_energy"


class BinaryDesiredStateTests(unittest.TestCase):
    def head(self):
        head = DiagonalLinUCB(12, [0, 1], desired_state_learning=True)
        for _ in range(80):
            head.update(0, {0: 1, 5: .08}, .8, evidence_weight=.25)
            head.update(1, {0: 1, 5: .55}, .8, evidence_weight=.25)
            for _ in range(3):
                head.update(1, {0: 1, 5: .24}, .8, sample_mass=1/3, evidence_weight=.25)
        head.state_classifier.fit()
        return head

    def test_positive_background_and_stationary_energy_separate(self):
        head = self.head()
        for energy, expected in ((.08, 0), (.24, 1), (.28, 1), (.55, 1)):
            self.assertEqual(head.choose({0: 1, 5: energy})[0]["index"], expected)

    def test_noisy_demonstrations_do_not_erase_weaker_stationary_pattern(self):
        head = DiagonalLinUCB(12, [0, 1], desired_state_learning=True)
        for i in range(200):
            for energy, desired in ((.08, 0), (.24, 1), (.55, 1)):
                observed = 1 - desired if i % 7 == 0 else desired
                head.update(observed, {0: 1, 5: energy}, .8, evidence_weight=.25)
        head.state_classifier.fit()
        for energy, expected in ((.08, 0), (.24, 1), (.55, 1)):
            self.assertEqual(head.choose({0: 1, 5: energy})[0]["index"], expected)
        self.assertEqual(head.state_classifier.active, (5,))

    def test_serialization_and_frozen_snapshot_keep_actual_decision_rule(self):
        head = self.head()
        raw = json.loads(json.dumps(head.export()))
        restored = DiagonalLinUCB(12, [0, 1], model=raw)
        before_snapshot = copy.deepcopy(head.export())
        frozen = FrozenRidgeSnapshot(SimpleNamespace(actions=[0, 1], dims=12, heads={1: head}))
        self.assertEqual(head.export(), before_snapshot)
        for energy in (.08, .24, .55):
            x = {0: 1, 5: energy}
            expected = head.choose(x)[0]["index"]
            self.assertEqual(restored.choose(x)[0]["index"], expected)
            self.assertEqual(frozen.predict(1, x), expected)
        before = copy.deepcopy(raw)
        restored.update(0, {0: 1, 5: .24}, -1)
        self.assertEqual(raw, before)
        self.assertEqual(frozen.predict(1, {0: 1, 5: .24}), 1)

    def test_rejected_action_does_not_create_alternative_positive_label(self):
        head = self.head()
        before = copy.deepcopy(head.state_classifier.export())
        head.update(1, {0: 1, 5: .24}, -1)
        self.assertEqual(head.state_classifier.export(), before)
        self.assertLess(head.rejection_b[1][5], 0)
        self.assertEqual(sum(head.rejection_b[0]), 0)

    def test_reservoir_is_bounded_and_retains_full_history(self):
        learner = BinaryStateClassifier(8)
        for i in range(2000):
            learner.add(0, {5: i / 2000}, .1)
        self.assertEqual(len(learner.rows[0]), learner.ROWS_PER_ACTION)
        self.assertTrue(any(row["id"] < 500 for row in learner.rows[0]))
        self.assertEqual(learner.recent[0][-1]["id"], 2000)
        self.assertFalse(learner.ready)

    def test_legacy_head_keeps_its_original_rule(self):
        legacy = DiagonalLinUCB(12, [0, 1])
        raw = legacy.export()
        raw.pop("binary_state_classifier")
        restored = DiagonalLinUCB(12, [0, 1], model=raw, desired_state_learning=True)
        self.assertIsNone(restored.state_classifier)

    def test_utility_only_head_can_misclassify_positive_stationary_signal(self):
        legacy = DiagonalLinUCB(12, [0, 1])
        for _ in range(80):
            legacy.update(0, {0: 1, 5: .08, 6: 1, 9: 1}, .8, evidence_weight=.5)
            legacy.update(1, {0: 1, 5: .55, 6: 1, 9: 1}, .8, evidence_weight=.25)
            legacy.update(1, {0: 1, 5: .24, 6: 1, 9: 1}, .8, evidence_weight=.25)
        self.assertEqual(legacy.choose({0: 1, 5: .24, 6: 1, 9: 1})[0]["index"], 0)


class RelayObservationContractTests(unittest.TestCase):
    def vector(self, eid, value, unit=None, contract=3):
        st = state(eid, value, **({"unit_of_measurement": unit} if unit else {}))
        history = TemporalHistory()
        history.add(eid, 100, st)
        return build_observation_features(FeatureSchemaV12(128, [eid], contract),
                                         {eid: st}, history, 100,
                                         agent(target_entity=TARGET))[0]

    def test_actual_contract_preserves_distance_resolution_and_units(self):
        eid = "sensor.espen4_still_distance"
        self.assertAlmostEqual(self.vector(eid, 150, "cm")[5], self.vector(eid, 1.5, "m")[5])
        self.assertAlmostEqual(self.vector(eid, 1500, "mm")[5], self.vector(eid, 1.5, "m")[5])
        self.assertGreater(self.vector(eid, 300, "cm")[5], self.vector(eid, 150, "cm")[5])
        self.assertLess(self.vector(eid, 300, "cm")[5], .8)

    def test_energy_without_unit_is_not_saturated(self):
        self.assertAlmostEqual(self.vector(ENERGY, 24)[5], math.tanh(.24))
        self.assertGreater(self.vector(ENERGY, 55)[5], self.vector(ENERGY, 24)[5])

    def test_old_relay_contracts_retain_nominal_columns(self):
        for version in (1, 2):
            self.assertEqual(self.vector(ENERGY, 24, "%", version)[6], 1)
            raw = FeatureSchemaV12(128, [ENERGY], version).export()
            self.assertEqual(FeatureSchemaV12.from_export(raw, 128).feature_contract_version, version)
        self.assertEqual(self.vector(ENERGY, 24, "%").get(6, 0), 0)

    def test_rebuild_seed_upgrades_without_mutating_live_model(self):
        contract = install_training_contract()
        try:
            manager = object.__new__(HistoryManager)
            manager.training_schema_cache = {}
            manager.engine = SimpleNamespace(models={})
            raw = {"version": 11, "dims": 128, "actions": [0, 1], "horizons": [1],
                   "schema": FeatureSchemaV12(128, [ENERGY], 2).export(),
                   "selection_meta": {"automation_baseline_entities": [ENERGY]}, "heads": {"1": {"sentinel": 1}}}
            before = copy.deepcopy(raw)
            seed = manager._remember_training_schema(agent(target_entity=TARGET, input_entities=[ENERGY]), raw)
            self.assertEqual(seed["heads"], {})
            self.assertEqual(seed["schema"]["feature_contract_version"], 4)
            self.assertEqual(seed["selection_meta"], raw["selection_meta"])
            self.assertEqual(raw, before)
        finally:
            contract["restore"]()

    def test_same_device_bundle_excludes_foreign_radar(self):
        still = "binary_sensor.espen4_still_target"
        foreign = "sensor.other_stationary_energy"
        states = {ENERGY: state(ENERGY, 24), still: state(still, "on"), foreign: state(foreign, 50)}
        registry = {ENERGY: {"device_id": "radar", "area_id": "bath"},
                    still: {"device_id": "radar", "area_id": "bath"},
                    foreign: {"device_id": "other", "area_id": "bath"}}
        self.assertEqual(radar_context_entities(agent(target_entity=TARGET), [ENERGY], states, registry), [still])

    def test_unmapped_automation_radar_uses_exact_family_not_kitchen_correlation(self):
        sibling = "sensor.espen4_moving_energy"
        distance = "sensor.espen4_stationary_target_distance"
        foreign = "sensor.kitchen_presence_g6_still_energy"
        states = {eid: state(eid, 24) for eid in (ENERGY, sibling, distance, foreign)}
        registry = {TARGET: {"area_id": "bath"}, foreign: {"area_id": "kitchen"}}
        selected = radar_context_entities(agent(target_entity=TARGET), [ENERGY], states, registry)
        self.assertEqual(set(selected), {sibling, distance})
        registry[sibling] = {"area_id": "kitchen"}
        self.assertNotIn(sibling, radar_context_entities(agent(target_entity=TARGET), [ENERGY], states, registry))

    def test_rebuild_replaces_full_correlated_schema_before_truncation(self):
        import ha
        import threading
        sibling = "sensor.espen4_moving_energy"
        foreign = [f"sensor.kitchen_presence_g{i}_still_energy" for i in range(7)]
        states = {eid: state(eid, 24) for eid in [ENERGY, sibling] + foreign}
        contract = install_training_contract()
        try:
            manager = object.__new__(HistoryManager)
            manager.training_schema_cache = {}
            manager.engine = SimpleNamespace(models={}, lock=threading.RLock(), state_map=states,
                                             entity_registry={TARGET: {"area_id": "bath"}})
            raw = {"version": 11, "dims": 128, "actions": [0, 1], "horizons": [1],
                   "schema": FeatureSchemaV12(128, foreign + [ENERGY], 2).export(),
                   "selection_meta": {"automation_baseline_entities": [ENERGY]}, "heads": {}}
            with patch.object(ha.AUTOMATION_KNOWLEDGE, "hints_for_target", return_value=({ENERGY}, [])):
                seed = manager._remember_training_schema(agent(target_entity=TARGET), raw)
            self.assertEqual(seed["schema"]["entities"], [ENERGY, sibling])
        finally:
            contract["restore"]()

    def test_schema_migration_preserves_contract_and_classifier_feature_identity(self):
        from manual_context_learning import _migrate_schema
        from policy import DiagonalLinUCB
        first, second = ENERGY, "sensor.espen4_moving_energy"
        schema = FeatureSchemaV12(128, [first, second], 3)
        head = DiagonalLinUCB(128, [0, 1], .65, desired_state_learning=True)
        for _ in range(8):
            head.update(0, {0: 1, 5: -.4, 17: .1}, 1)
            head.update(1, {0: 1, 5: .4, 17: .1}, 1)
        before = head.state_classifier.score({5: .4, 17: .1})
        policy = SimpleNamespace(schema=schema, dims=128, heads={1: head}, selection_meta={})
        _migrate_schema(policy, [second, first], {})
        self.assertAlmostEqual(head.state_classifier.score({5: .1, 17: .4}), before)
        self.assertEqual(policy.schema.feature_contract_version, 3)
        policy.schema = FeatureSchemaV12(128, [first], 2)
        _migrate_schema(policy, [second], {})
        self.assertEqual(policy.schema.feature_contract_version, 2)

    def test_installed_selector_keeps_baseline_and_radar_but_respects_manual_inputs(self):
        import ha
        import policy
        from fast_local_primary import install
        still = "binary_sensor.espen4_still_target"
        foreign = "sensor.other_stationary_energy"
        states = {TARGET: state(TARGET), ENERGY: state(ENERGY, 24), still: state(still, "on"),
                  foreign: state(foreign, 50)}
        registry = {TARGET: {"area_id": "bath"}, ENERGY: {"device_id": "radar", "area_id": "bath"},
                    still: {"device_id": "radar", "area_id": "bath"},
                    foreign: {"device_id": "other", "area_id": "bath"}}
        engine = SimpleNamespace(policy=lambda a: None)
        install(SimpleNamespace(), engine)
        infos = [{"entity_id": "automation.bath", "enabled": True, "context_entities": [ENERGY]}]
        with patch.object(ha.AUTOMATION_KNOWLEDGE, "hints_for_target", return_value=([ENERGY], infos)):
            selected, meta = policy.select_context_entities(agent(target_entity=TARGET), states, registry, [ENERGY])
            self.assertEqual(selected[0], ENERGY)
            self.assertIn(still, selected)
            self.assertNotIn(foreign, selected)
            self.assertEqual(meta["radar_context_entities"], [still])
            manual, _ = policy.select_context_entities(agent(target_entity=TARGET, input_entities=[ENERGY]), states, registry, [ENERGY])
            self.assertEqual(manual, [ENERGY])

    def test_maintenance_samples_count_once_and_all_must_be_correct(self):
        backend = SimpleNamespace(actions=[0, 1], predict=lambda obs: ({"index": int(obs)},))
        rows = [{"observation": 1, "observations": [1, 1, 0], "action_idx": 1, "ridge_correct": True},
                {"observation": 0, "observations": [0, 0, 0], "action_idx": 0, "ridge_correct": True}]
        with patch("policy_tiny_mlp_training._numpy_prediction_indices", return_value=None):
            report = evaluate_supervised(backend, agent(target_entity=TARGET), rows)
        self.assertEqual(report["samples"], 2)
        self.assertEqual(report["correct"], 1)
        self.assertEqual(report["per_action_accuracy"]["1"], 0)
        self.assertEqual(report["paired_comparison"]["ridge_only_correct"], 1)


if __name__ == "__main__":
    unittest.main()
