import threading
import unittest
from types import SimpleNamespace

from support import state
from fast_local_primary import install, normalize_fast_primary


BATH = "binary_sensor.bathroom_presence"
KITCHEN = "binary_sensor.kitchen_presence"


def fast_agent(**extra):
    payload = {
        "id": "bath-agent",
        "target_entity": "switch.bathroom_light",
        "target_property": "power",
    }
    payload.update(extra)
    return payload


def engine_for(relevance=None):
    return SimpleNamespace(
        lock=threading.RLock(),
        state_map={
            BATH: state(BATH, "on", device_class="occupancy"),
            KITCHEN: state(KITCHEN, "off", device_class="occupancy"),
        },
        context_relevance={"bath-agent": dict(relevance or {})},
    )


def policy_for(meta, entities=None):
    return SimpleNamespace(
        schema=SimpleNamespace(entities=list(entities or [BATH, KITCHEN])),
        selection_meta=dict(meta),
    )


class FastLocalPrimaryTests(unittest.TestCase):
    def test_fast_local_occupancy_beats_more_correlated_remote_sensor(self):
        engine = engine_for({KITCHEN: 0.97, BATH: 0.61})
        policy = policy_for({
            "primary_local_sensors": [BATH],
            "primary_local_sensor": BATH,
            "primary_occupancy_sensor": KITCHEN,
            "selection_reasons": {
                BATH: ["local-primary"],
                KITCHEN: ["causal-behaviour", "historical-precursor"],
            },
        })
        before = list(policy.schema.entities)

        result = normalize_fast_primary(fast_agent(), policy, engine)

        self.assertTrue(result["changed"])
        self.assertEqual(result["previous"], KITCHEN)
        self.assertEqual(result["primary"], BATH)
        self.assertEqual(result["source"], "local")
        self.assertEqual(policy.selection_meta["primary_occupancy_sensor"], BATH)
        self.assertEqual(policy.selection_meta["primary_occupancy_previous"], KITCHEN)
        self.assertEqual(policy.schema.entities, before, "metadata correction must not mutate schema")

    def test_target_automation_occupancy_is_fallback_when_area_mapping_missing(self):
        engine = engine_for({KITCHEN: 0.99, BATH: 0.52})
        policy = policy_for({
            "primary_local_sensors": [],
            "primary_local_sensor": None,
            "primary_occupancy_sensor": KITCHEN,
            "selection_reasons": {
                # The target automation identifies the bathroom sensor structurally even
                # when the generic Shelly target has no useful room token/area metadata.
                BATH: ["automation", "sensor-fit"],
                KITCHEN: ["causal-behaviour", "historical-precursor"],
            },
        })

        result = normalize_fast_primary(fast_agent(), policy, engine)

        self.assertTrue(result["changed"])
        self.assertEqual(result["primary"], BATH)
        self.assertEqual(result["source"], "automation")
        self.assertTrue(policy.selection_meta["primary_occupancy_structural"])

    def test_remote_context_remains_in_schema_after_primary_correction(self):
        engine = engine_for({KITCHEN: 0.95, BATH: 0.70})
        policy = policy_for({
            "primary_local_sensors": [BATH],
            "primary_local_sensor": BATH,
            "primary_occupancy_sensor": KITCHEN,
            "selection_reasons": {BATH: ["local-primary"], KITCHEN: ["causal-behaviour"]},
        })

        normalize_fast_primary(fast_agent(), policy, engine)

        self.assertIn(KITCHEN, policy.schema.entities)
        self.assertIn(BATH, policy.schema.entities)
        self.assertEqual(policy.selection_meta["primary_occupancy_sensor"], BATH)

    def test_non_fast_policy_is_unchanged(self):
        engine = engine_for({KITCHEN: 0.99, BATH: 0.50})
        policy = policy_for({
            "primary_local_sensors": [BATH],
            "primary_local_sensor": BATH,
            "primary_occupancy_sensor": KITCHEN,
        })

        result = normalize_fast_primary(
            fast_agent(target_entity="climate.bathroom", target_property="temperature"),
            policy,
            engine,
        )

        self.assertFalse(result["changed"])
        self.assertEqual(policy.selection_meta["primary_occupancy_sensor"], KITCHEN)

    def test_installed_wrapper_corrects_anchor_before_consumers_receive_policy(self):
        engine = engine_for({KITCHEN: 0.96, BATH: 0.63})
        policy = policy_for({
            "primary_local_sensors": [BATH],
            "primary_local_sensor": BATH,
            "primary_occupancy_sensor": KITCHEN,
            "selection_reasons": {BATH: ["local-primary"], KITCHEN: ["causal-behaviour"]},
        })
        engine.policy = lambda _agent: policy

        class Store:
            def __init__(self):
                self.events = []

            def event(self, *args):
                self.events.append(args)

        store = Store()
        self.assertTrue(install(store, engine))

        returned = engine.policy(fast_agent())

        self.assertIs(returned, policy)
        self.assertEqual(returned.selection_meta["primary_occupancy_sensor"], BATH)
        self.assertEqual(len(store.events), 1)
        self.assertEqual(store.events[0][2], "fast_primary_anchor_corrected")
        self.assertFalse(install(store, engine), "install must be idempotent")


if __name__ == "__main__":
    unittest.main()
