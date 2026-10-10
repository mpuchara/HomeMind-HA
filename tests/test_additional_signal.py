import json
import unittest
from types import SimpleNamespace

from additional_signal import apply, child_config_matches, evaluate, normalize, shadow_prediction, stabilize
from agent_candidate_config_guard import config_signature, install as install_guard
from agent_candidate_lineage import _row
from context import TemporalHistory
from lighting_conditions import lighting_context
import test_agent_explore as explore_fixture


SIGNAL = normalize({"entity_id": "sensor.kitchen_light", "purpose": "avoid_bright_on",
                    "threshold": 40, "hysteresis": 5, "max_age_seconds": 60})


def state(value, stamp=100):
    return {"entity_id": SIGNAL["entity_id"], "state": str(value), "last_updated": stamp,
            "attributes": {"__hm_received_time": stamp, "__hm_event_time": stamp}}


class SignalSemanticsTests(unittest.TestCase):
    def test_threshold_missing_stale_future_band_and_raw_units(self):
        agent = {"additional_signal": SIGNAL}
        for value, stamp, need, reason in [(100,100,False,"bright_enough"),
              (20,100,True,"dark"), (40,100,None,"hysteresis_band"),
              (20,20,None,"stale_observation"), (20,110,None,"future_observation"),
              ("nan",100,None,"value_unavailable"), ("unavailable",100,None,"value_unavailable")]:
            out = evaluate(agent, {SIGNAL["entity_id"]: state(value,stamp)}, 100)
            self.assertIs(out["need"], need)
            self.assertEqual(out["reason"], reason)
            self.assertEqual(out["unit"], "raw")
        self.assertEqual(evaluate(agent, {},100)["reason"], "value_unavailable")
        changed=state(100)
        changed["attributes"]["unit_of_measurement"]="lx"
        self.assertEqual(evaluate(agent,{SIGNAL["entity_id"]:changed},100)["reason"],"unit_changed")

    def test_only_new_on_unknown_and_explicit_user_priority(self):
        agent = {"additional_signal": SIGNAL}
        bright = evaluate(agent,{SIGNAL["entity_id"]:state(100)},100)
        self.assertEqual(apply(agent,0,1,bright), (0,True))
        self.assertEqual(apply(agent,1,1,bright), (1,False))
        self.assertEqual(apply(agent,1,0,bright), (0,False))
        self.assertEqual(apply(agent,0,1,bright,"user_instruction"), (1,False))
        self.assertEqual(apply(agent,0,1,{"need":None}), (1,False))
        self.assertEqual(apply({"additional_signal":{**SIGNAL,"purpose":"context"}},0,1,bright), (1,False))

    def test_causal_observation_rejects_late_packet(self):
        temporal = TemporalHistory(maxlen=30)
        late = state(100,110)
        temporal.add(SIGNAL["entity_id"],90,late)
        out=evaluate({"additional_signal":SIGNAL},{SIGNAL["entity_id"]:late},100,temporal)
        self.assertIsNone(out["need"])

    def test_hysteresis_holds_bright_until_dark_but_missing_resets_it(self):
        agent={"additional_signal":SIGNAL}
        bright=evaluate(agent,{SIGNAL["entity_id"]:state(100)},100)
        band=evaluate(agent,{SIGNAL["entity_id"]:state(40)},100)
        self.assertFalse(stabilize(band,bright)["need"])
        missing=stabilize(evaluate(agent,{},100),bright)
        self.assertIsNone(stabilize(band,missing)["need"])
        self.assertTrue(stabilize(evaluate(agent,{SIGNAL["entity_id"]:state(20)},100),bright)["need"])

    def test_goal_does_not_change_ungated_other_agents(self):
        states={SIGNAL["entity_id"]:state(100)}
        self.assertTrue(lighting_context({},states,100)["need"])
        self.assertFalse(lighting_context({"additional_signal":SIGNAL},states,100)["need"])

    def test_tournament_cannot_evict_selected_signal_for_higher_scoring_sensor(self):
        from context_tournament_primary_protection import replacement_plan
        from unittest.mock import patch
        policy=SimpleNamespace(schema=SimpleNamespace(entities=[SIGNAL["entity_id"]]), selection_meta={})
        with patch("context_tournament_promotion._selection_limit", return_value=1):
            plan=replacement_plan({"additional_signal":SIGNAL},policy,"sensor.other",{},.1,.99)
        self.assertEqual(plan["reason"],"no_replaceable_schema_slot")
        self.assertFalse(plan["passes"])

    def test_invalid_configuration_rejected(self):
        for config in [{**SIGNAL,"threshold":float("inf")}, {**SIGNAL,"hysteresis":41},
                       {**SIGNAL,"entity_id":"switch.helper"}, {**SIGNAL,"surprise":1},
                       {**SIGNAL,"threshold":True}, {**SIGNAL,"max_age_seconds":0}]:
            with self.assertRaises(ValueError): normalize(config)

    def test_shadow_keeps_virtual_off_when_legacy_automation_turns_on(self):
        agent={"id":"child","target_entity":"light.stairs","additional_signal":SIGNAL}
        manager=SimpleNamespace(store=SimpleNamespace(get_agent_config=lambda _:agent))
        generation={"agent_id":"child","generation_id":"g1"}
        result={"desired":1,"model_revision":"v1"}
        states={SIGNAL["entity_id"]:state(100),"light.stairs":{"entity_id":"light.stairs","state":"off"}}
        first=shadow_prediction(manager,generation,states,100,result,None)
        self.assertEqual(first["desired"],0)
        states["light.stairs"]={"entity_id":"light.stairs","state":"on"}
        self.assertEqual(shadow_prediction(manager,generation,states,101,result,None)["desired"],0)
        states["light.stairs"]={"entity_id":"light.stairs","state":"on","context":{"user_id":"user"}}
        self.assertEqual(shadow_prediction(manager,generation,states,102,result,None)["desired"],1)


class AdditionalSignalWorkflowTests(unittest.TestCase):
    setUp=explore_fixture.AgentExploreTests.setUp
    tearDown=explore_fixture.AgentExploreTests.tearDown

    def create(self, purpose="avoid_bright_on"):
        self.engine.state_map[SIGNAL["entity_id"]]=state(100)
        return self.manager.workflow_explore(self.root["id"],{
            "mode":"additional_signal","additional_signal":{**SIGNAL,"purpose":purpose}})

    def test_child_rebuild_preserves_parent_and_inherits_durable_signal(self):
        before=self.store.get_agent_config(self.root["id"])
        model=self.store.get_model(self.root["id"])
        result=self.create()
        child=_row(self.store,generation_id=result["child_generation_id"])
        config=self.store.get_agent_config(child["agent_id"])
        self.assertEqual(config["additional_signal"],SIGNAL)
        self.assertEqual(before,self.store.get_agent_config(self.root["id"]))
        self.assertEqual(model,self.store.get_model(self.root["id"]))
        self.assertTrue(child_config_matches(self.store,before,config))
        row=self.manager._candidate_row(self.root["id"])
        self.assertTrue(self.manager._start_build(row))
        self.assertEqual(self.queue.calls[-1]["reason"],"explore_additional_signal")
        self.assertTrue(self.queue.calls[-1]["rebuild"])
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()
        # A fresh Store reads the exact durable config after restart.
        import storage
        self.assertEqual(storage.Store(self.store.path).get_agent_config(child["agent_id"])["additional_signal"],SIGNAL)

    def test_parent_edit_invalidates_goal_proof_and_guard_preserves_intentional_difference(self):
        install_guard(self.manager)
        result=self.create()
        child=_row(self.store,generation_id=result["child_generation_id"])
        candidate=self.store.get_agent_config(child["agent_id"])
        self.assertTrue(child_config_matches(self.store,self.root,candidate))
        self.manager._start_build(self.manager._candidate_row(self.root["id"]))
        self.assertEqual(self.store.get_agent_config(child["agent_id"])["additional_signal"],SIGNAL)
        self.store.update_agent(self.root["id"],{"action_interval":3})
        self.assertFalse(child_config_matches(self.store,self.store.get_agent_config(self.root["id"]),candidate))

    def test_context_only_is_distinct_from_targeted_gain_trial(self):
        result=self.create("context")
        child=_row(self.store,generation_id=result["child_generation_id"])
        self.assertEqual(self.store.get_agent_config(child["agent_id"])["additional_signal"]["purpose"],"context")
        self.assertEqual(result["session"]["status"],"queued")

    def test_failed_request_does_not_create_child(self):
        with self.assertRaises(ValueError):
            self.manager.workflow_explore(self.root["id"],{"mode":"additional_signal", "additional_signal":SIGNAL})
        self.assertIsNone(self.manager._candidate_row(self.root["id"]))

    def test_cross_area_signal_is_pinned_without_replacing_primary_motion(self):
        from observation_contract import install_training_contract
        from policy import MultiHorizonPolicy
        self.engine.state_map[SIGNAL["entity_id"]]=state(100)
        states=self.engine.state_map
        registry={eid:{"area_id":"kitchen" if eid==SIGNAL["entity_id"] else "stairs"} for eid in states}
        contract=install_training_contract()
        try:
            policy=MultiHorizonPolicy({**self.root,"additional_signal":SIGNAL},states,registry,[])
            self.assertIn("binary_sensor.presence",policy.schema.entities)
            self.assertIn(SIGNAL["entity_id"],policy.schema.entities)
            self.assertEqual(policy.selection_meta["additional_signal"],SIGNAL)
        finally: contract["restore"]()
