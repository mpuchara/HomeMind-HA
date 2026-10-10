import json
import time
import unittest
from unittest.mock import patch

import test_atomic_promote_lifecycle as atomic_fixture
import test_candidate_shadow_runtime as shadow_fixture
from additional_signal import normalize
from agent_candidate_config_guard import config_signature
from agent_candidate_lineage import _row
from agent_explore import ensure_explore_tables

SIGNAL=normalize({"entity_id":"sensor.kitchen_light","purpose":"avoid_bright_on",
                  "threshold":40,"hysteresis":5,"max_age_seconds":60})


class GoalAtomicPromotionTests(unittest.TestCase):
    setUp=atomic_fixture.AtomicPromoteTests.setUp
    tearDown=atomic_fixture.AtomicPromoteTests.tearDown

    def configure(self):
        ensure_explore_tables(self.store)
        child=_row(self.store,agent_id=self.candidate["id"])
        with self.store.conn() as c:
            c.execute("UPDATE agents SET additional_signal=? WHERE id=?",(json.dumps(SIGNAL),self.candidate["id"]))
            c.execute("""INSERT INTO agent_explore_sessions(session_id,root_agent_id,parent_generation_id,
                child_generation_id,live_owner_agent_id,mode,status,requested_config_json,created_ts,updated_ts)
                VALUES('signal',?,?,?,?, 'additional_signal','comparing',?,1,1)""",
                (self.root["id"],child["parent_generation_id"],child["generation_id"],self.root["id"],
                 json.dumps({"parent_signature":config_signature(self.root),"additional_signal":SIGNAL})))
        model=self.store.get_model(self.candidate["id"])
        model["selection_meta"]["additional_signal"]=SIGNAL
        model["schema"]["entities"].append(SIGNAL["entity_id"])
        self.store.save_model(self.candidate["id"],model)

    def test_promotion_commits_model_and_goal_and_keeps_old_config_in_backup(self):
        self.configure()
        self.manager.promote(self.root["id"])
        root=self.store.get_agent_config(self.root["id"])
        self.assertEqual(root["additional_signal"],SIGNAL)
        self.assertEqual(self.store.get_model(root["id"])["model_revision"],"candidate-r1")
        with self.store.conn() as c:
            old=json.loads(c.execute("SELECT agent_json FROM agent_generation_backups").fetchone()[0])
        self.assertIsNone(old["additional_signal"])

    def test_parent_goal_edit_blocks_swap(self):
        self.configure()
        self.store.update_agent(self.root["id"],{"additional_signal":{**SIGNAL,"threshold":80}})
        with self.assertRaises(ValueError): self.manager.promote(self.root["id"])
        self.assertEqual(self.store.get_model(self.root["id"])["model_revision"],"live-r0")

    def test_failed_swap_rolls_back_goal_with_model(self):
        self.configure()
        with self.store.conn() as c:
            c.execute("""CREATE TRIGGER fail_goal_swap BEFORE UPDATE ON agent_generation_state
                BEGIN SELECT RAISE(ABORT, 'test swap failure'); END""")
        with self.assertRaises(Exception): self.manager.promote(self.root["id"])
        self.assertIsNone(self.store.get_agent_config(self.root["id"])["additional_signal"])
        self.assertEqual(self.store.get_model(self.root["id"])["model_revision"],"live-r0")


class GoalFuturePairTests(unittest.TestCase):
    setUp=shadow_fixture.CandidateShadowRuntimeTests.setUp
    tearDown=shadow_fixture.CandidateShadowRuntimeTests.tearDown
    _g1=shadow_fixture.CandidateShadowRuntimeTests._g1
    _mark_trained=shadow_fixture.CandidateShadowRuntimeTests._mark_trained
    _states=staticmethod(shadow_fixture.CandidateShadowRuntimeTests._states)

    def test_daylight_criterion_keeps_observed_on_and_calibration_separate(self):
        status,generation=self._g1()
        with self.store.conn() as c:
            c.execute("UPDATE agents SET additional_signal=? WHERE id=?",(json.dumps(SIGNAL),status["candidate_id"]))
        self.engine.models.pop(status["candidate_id"],None)
        at=time.time()
        states=shadow_fixture.CandidateShadowRuntimeTests._states()
        states[SIGNAL["entity_id"]]={"entity_id":SIGNAL["entity_id"],"state":"100","last_updated":at,"attributes":{}}
        self.engine.runtime[self.root["id"]]={"last_prediction":1,"last_confidence":.9}
        bundle=self.manager.after_live_process(self.root,states)
        self.assertEqual(bundle["results"][generation["generation_id"]]["desired"],0)
        states["light.shadow"]={"entity_id":"light.shadow","state":"on","last_updated":at+1,"attributes":{},"context":{"parent_id":"automation"}}
        self.assertTrue(self.manager.before_live_process(self.root,states))
        with self.store.conn() as c:
            pair=dict(c.execute("SELECT * FROM candidate_generation_pairs").fetchone())
        self.assertEqual(pair["outcome"],1)
        self.assertEqual(pair["child_correct"],0)
        self.assertEqual(pair["objective_outcome"],0)
        self.assertEqual(pair["objective_source"],"configured_daylight_preference")
        self.assertFalse(pair["calibration_eligible"])
        self.assertIsNone(pair["calibration_outcome"])
        self.manager.after_live_process(self.root,states)
        with self.store.conn() as c:
            count=c.execute("SELECT COUNT(*) FROM candidate_generation_decisions").fetchone()[0]
        self.manager.after_live_process(self.root,states)
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM candidate_generation_decisions").fetchone()[0],count)
        # A subsequent manual entry remains a real demonstration, not daylight
        # objective evidence or dark-entry promotion coverage.
        states["light.shadow"]={"entity_id":"light.shadow","state":"off","last_updated":at+2,"attributes":{}}
        self.manager.after_live_process(self.root,states)
        states["light.shadow"]={"entity_id":"light.shadow","state":"on","last_updated":at+3,"attributes":{},"context":{"user_id":"user"}}
        self.assertTrue(self.manager.before_live_process(self.root,states))
        with self.store.conn() as c:
            manual=c.execute("SELECT objective_outcome,objective_source FROM candidate_generation_pairs ORDER BY outcome_ts DESC").fetchone()
        self.assertIsNone(manual[0])
        self.assertIsNone(manual[1])
        self.executor.service.assert_not_called()
