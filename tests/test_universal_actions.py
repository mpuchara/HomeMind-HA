"""U04 contracts: variable number of devices/actions, explicit arbiter mask, safe observer."""
import copy
import json
import math
import unittest

import numpy as np

from universal_actions import from_device_agent_mask, ActionSet
from universal_action_scorer import UniversalActionScorer
from universal_neural_encoder import IncrementalNeuralHome, SharedSetEncoder


def fixture(domain="light", prop="power", *, entity=None, attrs=None, state="off",
            lo=0.0, hi=1.0, area="kitchen", autonomy=True):
    entity = entity or f"{domain}.device"
    agent = {
        "id": entity+":"+prop, "target_entity": entity,
        "target_property": prop, "min_value": lo, "max_value": hi,
    }
    descriptor = {
        "logical_device_id": "dev-"+entity,
        "device_property": prop,
        "physical_target_property": prop,
        "area_id": area,
        "resource_keys": ["device:dev-"+entity],
        "properties": [{"entity_id": entity, "property": prop}],
        "autonomy_allowed": autonomy,
    }
    snapshot = {"entity_id": entity, "state": state, "attributes": attrs or {}}
    return agent, descriptor, snapshot


def mask(*values, legal=True):
    return {
        "actions": [{"requested": float(x), "value": float(x), "legal": legal, "reason": None}
                    for x in values],
        "fallback": "policy_choice_within_mask" if legal else "abstain",
        "manual_priority": True,
    }


def observe(agent, descriptor, state, action_mask, *, now=100.0):
    return from_device_agent_mask(
        agent, descriptor, action_mask, state, mask_timestamp=now,
    )


class DynamicActionContractTests(unittest.TestCase):
    def test_light_binary_and_current_value(self):
        a, d, state = fixture()
        action_set = observe(a, d, state, mask(0, 1))
        self.assertEqual(action_set.count, 2)
        self.assertEqual(action_set.version, 1)
        self.assertEqual(action_set.candidates[0].kind, "binary")
        self.assertEqual([x.value for x in action_set.candidates], [0.0, 1.0])
        self.assertEqual(action_set.candidates[0].current_value, 0.0)
        self.assertEqual(action_set.candidates[1].resources, ("device:dev-light.device",))

    def test_select_option_names_not_only_numeric_indices(self):
        agent, descriptor, state = fixture(
            "select", "option_index", lo=0, hi=10, state="Comfort",
            attrs={"options": ["Eco", "Comfort", "Boost"]},
        )
        proposals = observe(agent, descriptor, state, mask(0, 1, 2))
        self.assertEqual([x.option_label for x in proposals.candidates],
                         ["Eco", "Comfort", "Boost"])
        self.assertEqual(proposals.candidates[0].kind, "categorical")
        self.assertEqual(proposals.candidates[0].upper, 2.0)
        self.assertEqual(proposals.candidates[0].current_value, 1.0)
        with self.assertRaises(ValueError):
            observe(agent, descriptor, state, mask(4))

    def test_existing_device_limits_and_deduplication(self):
        agent, descriptor, state = fixture(
            "climate", "temperature", lo=15, hi=29,
            attrs={"min_temp": 18, "max_temp": 24, "temperature": 21},
            state="heat",
        )
        proposals = observe(agent, descriptor, state, mask(18, 21, 21, 24))
        self.assertEqual(proposals.count, 3)
        self.assertEqual((proposals.candidates[0].lower, proposals.candidates[0].upper),
                         (18.0, 24.0))
        self.assertEqual(proposals.candidates[0].current_value, 21.0)
        with self.assertRaises(ValueError):
            observe(agent, descriptor, state, mask(17, 19))

    def test_manual_hold_or_lease_blocks_all_candidates(self):
        agent, descriptor, state = fixture()
        blocked = {
            "actions": [{"requested": v, "value": v, "legal": False,
                         "reason": "shared resource manual hold"} for v in (0, 1)],
            "fallback": "abstain",
        }
        proposals = observe(agent, descriptor, state, blocked)
        self.assertEqual(proposals.count, 0)
        self.assertEqual(proposals.rejected, 2)
        self.assertEqual(proposals.fallback, "abstain")
        self.assertEqual(proposals.source, "caller_supplied_DeviceAgent.legal_action_mask")

    def test_explicit_autonomy_denial_fail_closed(self):
        a, d, state = fixture(autonomy=False)
        set_ = observe(a, d, state, mask(0, 1))
        self.assertEqual(set_.count, 0)
        self.assertEqual(set_.fallback, "abstain")

    def test_missing_or_inconsistent_authority_fails_closed(self):
        a, d, state = fixture()
        cases = [
            (a, {**d, "physical_target_property": "brightness_pct"}, state, mask(0, 1)),
            (a, {**d, "properties": []}, state, mask(0, 1)),
            (a, d, state, {}),
            (a, d, state, {"actions": [{"legal": True, "value": float("nan"), "requested": 0}]}),
            (a, d, state, {"actions": [{"legal": 1, "value": 0, "requested": 0}]}),
            (a, d, {**state, "entity_id": "light.other"}, mask(0, 1)),
            (a, d, {**state, "state": "unavailable"}, mask(0, 1)),
            (a, {**d, "resource_keys": []}, state, mask(0, 1)),
            (a, d, state, mask(.4)),
        ]
        for i, data in enumerate(cases):
            with self.subTest(case=i), self.assertRaises((ValueError, TypeError)):
                observe(*data)

    def test_action_cardinality_does_not_depend_on_network_width(self):
        agent, descriptor, state = fixture(
            "select", "option_index", lo=0, hi=999, state="m0",
            attrs={"options": ["m"+str(i) for i in range(1000)]},
        )
        proposals = observe(agent, descriptor, state, mask(*range(1000)))
        self.assertEqual(proposals.count, 1000)
        self.assertEqual(proposals.candidates[-1].option_label, "m999")
        self.assertEqual(proposals.candidates[-1].value, 999.0)

    def test_different_numbers_of_devices_have_same_shared_model(self):
        scorer = UniversalActionScorer()
        home = IncrementalNeuralHome()
        params = scorer.parameter_count
        for devices in (1, 5, 200):
            total = 0
            for i in range(devices):
                a, d, s = fixture(
                    "light", "power", entity=f"light.lamp_{i}",
                    area=f"room_{i%8}",
                )
                result = scorer.evaluate(observe(a, d, s, mask(0, 1)), home)
                self.assertFalse(result["trained"])
                self.assertFalse(result["control_eligible"])
                self.assertIsNone(result["projections"][0]["recommendation"])
                total += result["evaluated"]
            self.assertEqual(total, devices*2)
            self.assertEqual(scorer.parameter_count, params)


class ActionConditionedScorerTests(unittest.TestCase):
    def test_dynamic_output_width_and_bounded_batching(self):
        agent, descriptor, state = fixture(
            "select", "option_index", lo=0, hi=399, state="m0",
            attrs={"options": ["m"+str(i) for i in range(400)]},
        )
        proposals = observe(agent, descriptor, state, mask(*range(400)))
        model = UniversalActionScorer(room_width=32, hidden_width=24)
        result = model.evaluate(proposals, IncrementalNeuralHome(), max_batch=7)
        self.assertEqual(result["evaluated"], 400)
        self.assertEqual(len(result["projections"]), 400)
        self.assertTrue(all(math.isfinite(x["raw_projection"]) for x in result["projections"]))
        self.assertEqual([x["value"] for x in result["projections"]], list(range(400)))
        self.assertIsNone(result["projections"][0]["confidence"])
        self.assertEqual(result["fallback"], "observer_only")

    def test_zero_candidates_never_calls_model_or_suggests_action(self):
        agent, descriptor, state = fixture()
        empty = observe(agent, descriptor, state, mask(0, 1, legal=False))
        model = UniversalActionScorer()
        out = model.evaluate(empty, IncrementalNeuralHome())
        self.assertEqual(out["evaluated"], 0)
        self.assertEqual(out["fallback"], "abstain")
        self.assertEqual(out["projections"], [])

    def test_checkpoint_exact_reconstruction_and_invalid_writes(self):
        model = UniversalActionScorer(seed=12)
        checkpoint = json.loads(json.dumps(model.checkpoint()))
        clone = UniversalActionScorer(seed=99, checkpoint=checkpoint)
        agent, descriptor, state = fixture()
        actions = observe(agent, descriptor, state, mask(0, 1))
        home = IncrementalNeuralHome()
        self.assertEqual(model.evaluate(actions, home), clone.evaluate(actions, home))
        for patch in [
            {"version": 3},
            {"room_width": 999},
            {"trained": True},
            {"action_contract_version": 99},
            {"hash_buckets": 999},
        ]:
            invalid = {**checkpoint, **patch}
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                UniversalActionScorer(checkpoint=invalid)
        invalid = copy.deepcopy(checkpoint)
        invalid["layers"]["dense1"]["weights"][0][0] = "nan"
        with self.assertRaises(ValueError):
            UniversalActionScorer(checkpoint=invalid)

    def test_option_label_changes_projection_even_if_index_same(self):
        agent, descriptor, state = fixture(
            "select", "option_index", lo=0, hi=1, state="Comfort",
            attrs={"options": ["Eco", "Comfort"]},
        )
        a = observe(agent, descriptor, state, mask(1))
        different = observe(agent, descriptor, {**state, "attributes": {
            "options": ["Comfort", "Eco"]
        }}, mask(1))
        scorer = UniversalActionScorer()
        self.assertNotEqual(
            scorer.action_features(a.candidates[0]).tolist(),
            scorer.action_features(different.candidates[0]).tolist(),
        )

    def test_room_encoder_dimension_mismatch_is_explicit(self):
        scorer = UniversalActionScorer(room_width=12)
        a, d, s = fixture()
        with self.assertRaises(ValueError):
            scorer.evaluate(observe(a, d, s, mask(0, 1)), IncrementalNeuralHome())

    def test_fixed_encoder_cardinality_does_not_depend_on_action_count(self):
        scorer = UniversalActionScorer()
        a, d, s = fixture("light", "power")
        actions = observe(a, d, s, mask(0, 1))
        vectors = [scorer.action_features(x) for x in actions.candidates]
        self.assertTrue(all(x.shape == vectors[0].shape for x in vectors))
        self.assertEqual(len(vectors), 2)


if __name__ == "__main__":
    unittest.main()
