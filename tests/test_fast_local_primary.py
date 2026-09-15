import threading
import unittest
from types import SimpleNamespace

from support import state
from fast_local_primary import install, normalize_fast_primary


BATH = "binary_sensor.bathroom_presence"
KITCHEN = "binary_sensor.kitchen_presence"
TARGET = "switch.bathroom_light"


def fast_agent(**extra):
    payload = {
        "id": "bath-agent",
        "target_entity": TARGET,
        "target_property": "power",
        "input_entities": ["*"],
    }
    payload.update(extra)
    return payload


def automation_info(sensor=BATH, enabled=True, entity_id="automation.bathroom_light"):
    return {
        "entity_id": entity_id,
        "name": "Bathroom light automation",
        "enabled": enabled,
        "context_entities": [sensor],
        "target_entities": [TARGET],
    }


def engine_for(relevance=None):
    return SimpleNamespace(
        lock=threading.RLock(),
        state_map={
            TARGET: state(TARGET, "off"),
            BATH: state(BATH, "on", device_class="occupancy"),
            KITCHEN: state(KITCHEN, "off", device_class="occupancy"),
        },
        entity_registry={},
        context_relevance={"bath-agent": dict(relevance or {})},
    )


def policy_for(meta, entities=None):
    return SimpleNamespace(
        schema=SimpleNamespace(entities=list(entities or [BATH, KITCHEN])),
        selection_meta=dict(meta),
    )


class FastLocalPrimaryTests(unittest.TestCase):
    def setUp(self):
        import ha
        self.knowledge = ha.AUTOMATION_KNOWLEDGE
        with self.knowledge.lock:
            self.old_by_target = dict(self.knowledge.by_target)
            self.old_automations = list(self.knowledge.automations)
            self.knowledge.by_target = {}
            self.knowledge.automations = []

    def tearDown(self):
        with self.knowledge.lock:
            self.knowledge.by_target = self.old_by_target
            self.knowledge.automations = self.old_automations

    def set_automations(self, infos):
        with self.knowledge.lock:
            self.knowledge.by_target = {TARGET: list(infos)}
            self.knowledge.automations = list(infos)

    def test_current_automation_beats_local_or_more_correlated_remote_sensor(self):
        engine = engine_for({KITCHEN: 0.97, BATH: 0.61})
        policy = policy_for({
            "primary_local_sensors": [KITCHEN],
            "primary_local_sensor": KITCHEN,
            "primary_occupancy_sensor": KITCHEN,
        })

        result = normalize_fast_primary(
            fast_agent(), policy, engine, [automation_info(BATH, enabled=True)]
        )

        self.assertTrue(result["changed"])
        self.assertEqual(result["previous"], KITCHEN)
        self.assertEqual(result["primary"], BATH)
        self.assertEqual(result["source"], "automation")
        self.assertEqual(policy.selection_meta["primary_occupancy_sensor"], BATH)
        self.assertEqual(policy.selection_meta["automation_baseline_entities"], [BATH])
        self.assertTrue(policy.selection_meta["primary_occupancy_structural"])

    def test_local_occupancy_is_fallback_when_target_has_no_automation(self):
        engine = engine_for({KITCHEN: 0.99, BATH: 0.52})
        policy = policy_for({
            "primary_local_sensors": [BATH],
            "primary_local_sensor": BATH,
            "primary_occupancy_sensor": KITCHEN,
        })

        result = normalize_fast_primary(fast_agent(), policy, engine, [])

        self.assertTrue(result["changed"])
        self.assertEqual(result["primary"], BATH)
        self.assertEqual(result["source"], "local")

    def test_enabled_automation_is_preferred_over_old_disabled_mapping(self):
        engine = engine_for({KITCHEN: 0.99, BATH: 0.50})
        policy = policy_for({"primary_occupancy_sensor": KITCHEN})
        infos = [
            automation_info(KITCHEN, enabled=False, entity_id="automation.old_bathroom"),
            automation_info(BATH, enabled=True, entity_id="automation.current_bathroom"),
        ]

        result = normalize_fast_primary(fast_agent(), policy, engine, infos)

        self.assertEqual(result["primary"], BATH)
        self.assertEqual(policy.selection_meta["automation_baseline_candidates"], [BATH])
        self.assertTrue(policy.selection_meta["automation_baseline_current"])

    def test_automation_baseline_does_not_mutate_an_existing_schema(self):
        engine = engine_for({KITCHEN: 0.95, BATH: 0.70})
        policy = policy_for({"primary_occupancy_sensor": KITCHEN})
        before = list(policy.schema.entities)

        normalize_fast_primary(fast_agent(), policy, engine, [automation_info(BATH)])

        self.assertEqual(policy.schema.entities, before)
        self.assertIn(KITCHEN, policy.schema.entities)
        self.assertIn(BATH, policy.schema.entities)
        self.assertEqual(policy.selection_meta["primary_occupancy_sensor"], BATH)

    def test_non_fast_policy_is_unchanged(self):
        engine = engine_for({KITCHEN: 0.99, BATH: 0.50})
        policy = policy_for({"primary_occupancy_sensor": KITCHEN})

        result = normalize_fast_primary(
            fast_agent(target_entity="climate.bathroom", target_property="temperature"),
            policy,
            engine,
            [automation_info(BATH)],
        )

        self.assertFalse(result["changed"])
        self.assertEqual(policy.selection_meta["primary_occupancy_sensor"], KITCHEN)

    def test_new_fast_schema_starts_from_current_automation_context_only(self):
        self.set_automations([automation_info(BATH, enabled=True)])
        engine = engine_for({KITCHEN: 0.99, BATH: 0.55})
        # install() patches MultiHorizonPolicy's selector before a new policy is built.
        engine.policy = lambda _agent: policy_for({"primary_occupancy_sensor": BATH}, [BATH])

        class Store:
            def event(self, *args):
                pass

        install(Store(), engine)

        import policy as policy_module
        selected, meta = policy_module.select_context_entities(
            fast_agent(), engine.state_map, {}, {BATH},
            relevance_scores={KITCHEN: 0.99, BATH: 0.55},
        )

        self.assertEqual(selected, [BATH])
        self.assertEqual(meta["automation_baseline_entities"], [BATH])
        self.assertEqual(meta["automation_baseline_mode"], "automation_first")
        self.assertEqual(meta["automation_baseline_extra_sensors"], "sensor_tournament")
        self.assertNotIn(KITCHEN, selected, "correlated room sensors must begin as challengers")

    def test_explicit_manual_schema_is_not_overridden_by_automation_baseline(self):
        self.set_automations([automation_info(BATH, enabled=True)])
        engine = engine_for({KITCHEN: 0.99, BATH: 0.55})
        engine.policy = lambda _agent: policy_for({}, [BATH, KITCHEN])

        class Store:
            def event(self, *args):
                pass

        install(Store(), engine)

        import policy as policy_module
        manual = fast_agent(input_entities=[BATH, KITCHEN])
        selected, _ = policy_module.select_context_entities(
            manual, engine.state_map, {}, {BATH},
            relevance_scores={KITCHEN: 0.99, BATH: 0.55},
        )

        self.assertIn(BATH, selected)
        self.assertIn(KITCHEN, selected)


if __name__ == "__main__":
    unittest.main()
