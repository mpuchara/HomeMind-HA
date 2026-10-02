"""Release 0.14.125: persisted policy snapshots must survive read-only replay.

The 0.14.124 Correct export reconstructed the same raw model twice. The first
policy's time decay used shallow references into the JSON matrices, invalidating
the second policy's checksum. RoomBelief source auditing also needs to show
remote/unmapped HA automation radar without silently assigning an area.
"""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import agent, state
from policy import MultiHorizonPolicy
from policy_backend import model_checksum, verify_model_checksum
from correct_learning_debug import (
    _automation_raw_sources, _current_context, _context_at_label,
)


class ImmutableModelSnapshotTests(unittest.TestCase):
    def test_lazy_decay_and_updates_do_not_edit_raw_model_input(self):
        a = agent(id="bathroom", target_entity="switch.bathroom",
                  target_property="power", mode="shadow")
        states = {
            "switch.bathroom": state("switch.bathroom", "off"),
            "sensor.espen4_stationary_energy": state(
                "sensor.espen4_stationary_energy", "32",
                friendly_name="Stationary Energy", unit_of_measurement="%"
            ),
        }
        policy = MultiHorizonPolicy(
            a, states, {}, {"sensor.espen4_stationary_energy"}
        )
        for horizon in policy.horizons:
            for action_idx in (0, 1):
                policy.update(horizon, action_idx,
                              {0: 1.0, 1: 0.45}, 0.85 if action_idx else -0.25)
            policy.heads[horizon].last_decay_ts = time.time() - 7200.0
        raw = policy.serialize()
        self.assertTrue(verify_model_checksum(raw))
        original_hash = model_checksum(raw)
        before_weights = list(raw["heads"][str(policy.horizons[0])]["a"][0])

        clone = MultiHorizonPolicy(a, states, {}, set(), model=raw)
        clone.predict({0: 1.0, 1: 0.45})
        self.assertTrue(verify_model_checksum(raw))
        self.assertEqual(model_checksum(raw), original_hash)
        self.assertEqual(
            raw["heads"][str(policy.horizons[0])]["a"][0], before_weights
        )
        for horizon in clone.horizons:
            clone.update(horizon, 1, {0: 1.0, 1: 0.45}, 1.0)
        self.assertTrue(verify_model_checksum(raw))
        self.assertEqual(model_checksum(raw), original_hash)

        # The exact second construction in Correct diagnostics must not fail.
        second = MultiHorizonPolicy(a, states, {}, set(), model=raw)
        second.predict({0: 1.0, 1: 0.45})
        self.assertEqual(model_checksum(raw), original_hash)

    def test_correct_label_debug_reconstructs_the_same_checksum_snapshot_twice(self):
        a = agent(id="bathroom", target_entity="switch.bathroom",
                  target_property="power", mode="shadow")
        states = {"switch.bathroom": state("switch.bathroom", "off")}
        policy = MultiHorizonPolicy(a, states, {}, set())
        horizon = policy.horizons[0]
        policy.update(horizon, 1, {0: 1.0}, 1.0)
        policy.heads[horizon].last_decay_ts = time.time() - 7200.0
        raw = policy.serialize()
        digest = model_checksum(raw)
        engine = SimpleNamespace(
            lock=threading.RLock(),
            state_map=states,
            entity_registry={},
            context_relevance={},
            context=SimpleNamespace(excluded=set()),
            teaching=SimpleNamespace(
                point_context=lambda *args, **kwargs: (states, None, None)
            ),
        )
        store = SimpleNamespace(get_model=lambda agent_id: raw)
        with patch.object(MultiHorizonPolicy, "features",
                          return_value=({0: 1.0}, {0: ["bias"]}, {})):
            result = _context_at_label(
                engine, store, a, {"sample_ts": time.time() - 600}
            )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["prediction"]["value"], 1.0)
        self.assertEqual(model_checksum(raw), digest)
        self.assertTrue(verify_model_checksum(raw))

    def test_loaded_heads_are_independent_of_each_other(self):
        a = agent(id="bathroom", target_entity="switch.bathroom",
                  target_property="power", mode="shadow")
        states = {"switch.bathroom": state("switch.bathroom", "off")}
        policy = MultiHorizonPolicy(a, states, {}, set())
        raw = policy.serialize()
        left = MultiHorizonPolicy(a, states, {}, set(), model=raw)
        right = MultiHorizonPolicy(a, states, {}, set(), model=raw)
        horizon = left.horizons[0]
        old_value = right.heads[horizon].a[0][0]
        left.update(horizon, 0, {0: 1.0}, 1.0)
        self.assertEqual(right.heads[horizon].a[0][0], old_value)
        self.assertTrue(verify_model_checksum(raw))


class RoomSourceAuditTests(unittest.TestCase):
    def test_numeric_baseline_sources_are_extracted_from_lineage(self):
        radar = "sensor.espen4_stationary_energy"
        lineage = [{
            "model": {"selection_meta": {
                "automation_baseline_automations": [
                    {"baseline_rules": [{
                        "source": "trigger", "kind": "numeric_state",
                        "entity_id": radar,
                    }]},
                    {"baseline_rules": [{
                        "source": "trigger", "kind": "numeric_state",
                        "entity_id": radar,
                    }]}
                ]
            }}
        }]
        self.assertEqual(_automation_raw_sources(lineage), [radar])

    def test_unmapped_radar_is_reported_not_promoted_to_local_source(self):
        radar = "sensor.espen4_stationary_energy"
        target = "switch.bathroom"
        class Room:
            admitted = {radar}
            excluded = set()
            devices = {"radar-device": {"area_id": None}}
            def area_for(self, eid):
                return "bathroom" if eid == target else None
            def evidence_metadata(self, eid):
                return {"role": "radar_activity", "reason": "missing_area",
                        "available": True, "selected": True}

        engine = SimpleNamespace(
            lock=threading.RLock(),
            state_map={
                radar: state(radar, "64"),
                target: state(target, "off"),
            },
            entity_registry={radar: {"device_id": "radar-device"}},
            context=Room(),
        )
        with patch("correct_learning_debug._base_room_forecast_read_only",
                   return_value={"known": False, "uncertainty": 1.0}):
            output = _current_context(
                engine, {"target_entity": target}, [radar], [radar]
            )
        self.assertFalse(output["sources"])
        audit = output["automation_source_audit"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["status"], "unmapped_area")
        self.assertEqual(audit[0]["target_area_id"], "bathroom")
        self.assertIsNone(audit[0]["source_area_id"])
        self.assertEqual(audit[0]["role"], "radar_activity")
        self.assertIsNone(engine.context.area_for(radar))

    def test_other_room_source_is_not_local_evidence(self):
        radar = "sensor.espen4_stationary_energy"
        class Room:
            admitted = {radar}
            excluded = set()
            devices = {}
            def area_for(self, eid):
                return "bathroom" if eid == "switch.bathroom" else "kitchen"
            def evidence_metadata(self, eid):
                return {"role": "radar_activity", "selected": True}
        engine = SimpleNamespace(
            lock=threading.RLock(),
            state_map={radar: state(radar, "99")},
            entity_registry={radar: {"area_id": "kitchen"}},
            context=Room(),
        )
        with patch("correct_learning_debug._base_room_forecast_read_only",
                   return_value={"known": False}):
            output = _current_context(
                engine, {"target_entity": "switch.bathroom"}, [radar], [radar]
            )
        self.assertEqual(output["automation_source_audit"][0]["status"], "different_area")
        self.assertEqual(output["automation_source_audit"][0]["registry_area_id"], "kitchen")


if __name__ == "__main__":
    unittest.main()
