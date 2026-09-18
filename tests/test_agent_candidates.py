import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import support  # adds adaptive_ai/src to sys.path for the repository test harness
import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_balance import install as install_balance


class FakeTeaching:
    def teach(self, engine, agent, desired=None, sample_ts=None):
        return {"ok": True, "desired_value": 1.0, "sample_ts": sample_ts}

    def undo(self, engine, agent):
        return {"ok": True}


class FakePolicy:
    def __init__(self, value=1.0):
        self.value = value

    def features(self, states, temporal, at_ts=None):
        return {0: 1.0}, [], {}

    def predict(self, features):
        return {"value": self.value}, 0.9, [], 1, 1.0, 0.0


class FakeExecutor:
    def __init__(self):
        self.service = Mock(side_effect=AssertionError("Candidate must never dispatch"))
        self.release_control = Mock()

    @contextmanager
    def target_lock(self, entity_id):
        yield


class FakeHandler:
    def do_GET(self):
        return None

    def do_POST(self):
        return None

    def do_DELETE(self):
        return None


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "candidate.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.parent = self.store.create_agent({
            "name": "Bathroom light",
            "target_entity": "light.bathroom",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": .25,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.bathroom_presence"],
        })
        self.store.save_model(
            self.parent["id"],
            {"version": 10, "schema": {"version": 11, "entities": []}, "marker": "baseline"},
        )
        self.executor = FakeExecutor()
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(),
            rl_teaching=None,
            models={},
            runtime={},
            executor=self.executor,
            temporal_history=None,
            process_agent=self._live_process,
            policy=lambda agent: FakePolicy(1.0),
            own_command_echo=lambda agent, state, current: False,
            wake_event=SimpleNamespace(set=lambda: None),
        )
        self.core = SimpleNamespace(STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
                                    TRAINING_QUEUE=None, HISTORY=None)
        self.manager = install_balance(AgentCandidateManager(self.core, start_worker=False))

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _live_process(self, agent, state_map, changed_entities=None):
        self.engine.runtime.setdefault(agent["id"], {})["last_prediction"] = 0.0

    def test_candidate_is_hidden_from_live_agent_enumeration(self):
        status = self.manager.enqueue(self.parent["id"], "wrong_decision")
        self.assertIsNotNone(status)
        candidate_id = status["candidate_id"]
        self.assertIsNotNone(self.store.get_agent(candidate_id))
        self.assertEqual([a["id"] for a in self.store.list_agents()], [self.parent["id"]])
        self.assertEqual([a["id"] for a in self.store.list_agent_configs()], [self.parent["id"]])

    def test_feedback_coalesces_into_one_candidate_and_revision(self):
        first = self.manager.enqueue(self.parent["id"], "wrong_decision")
        second = self.manager.enqueue(self.parent["id"], "teach")
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(second["feedback_revision"], 2)
        with self.store.conn() as c:
            count = c.execute("SELECT COUNT(*) FROM agent_candidates WHERE parent_agent_id=?", (self.parent["id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_wrong_decision_hook_queues_candidate_without_changing_live_model(self):
        live_model = {"version": 10, "schema": {"version": 11, "entities": []}, "marker": "live"}
        self.store.save_model(self.parent["id"], live_model)
        before = self.store.get_model(self.parent["id"])
        self.engine.teaching.teach(self.engine, self.parent, desired=1.0)
        after = self.store.get_model(self.parent["id"])
        self.assertEqual(before, after)
        status = self.manager.status(self.parent["id"])
        self.assertEqual(status["feedback_revision"], 1)

    def test_candidate_inference_never_dispatches_service(self):
        status = self.manager.enqueue(self.parent["id"], "teach")
        candidate_id = status["candidate_id"]
        self.store.save_model(candidate_id, {"version": 10, "schema": {"version": 11, "entities": []}})
        self.store.set_training_state(candidate_id, "qualified", score=.9, samples=100, source="test", detail={})
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE agent_candidates SET state='comparing',build_revision=feedback_revision,dirty=0 WHERE parent_agent_id=?",
                      (self.parent["id"],))
        state_map = {"light.bathroom": {"entity_id": "light.bathroom", "state": "off", "attributes": {}}}
        self.engine.process_agent(self.parent, state_map, {"binary_sensor.bathroom_presence"})
        self.executor.service.assert_not_called()
        runtime = self.manager.runtime[self.parent["id"]]
        self.assertEqual(runtime["candidate_prediction"], 1.0)

    def test_binary_candidate_needs_twenty_future_samples_per_action(self):
        status = self.manager.enqueue(self.parent["id"], "teach")
        candidate_id = status["candidate_id"]
        self.store.save_model(candidate_id, {"version": 10, "schema": {"version": 11, "entities": []}})
        self.store.set_training_state(candidate_id, "qualified", score=.9, samples=100, source="test", detail={})
        comparison = {
            "samples": 40,
            "live_correct": 36,
            "candidate_correct": 38,
            "on_events": 20,
            "off_events": 20,
            "live_false_early": 0,
            "candidate_false_early": 0,
        }
        with self.store.lock, self.store.conn() as c:
            c.execute("""UPDATE agent_candidates SET state='comparing',build_revision=feedback_revision,dirty=0,
                       comparison_json=? WHERE parent_agent_id=?""",
                      (json.dumps(comparison), self.parent["id"]))
        ready = self.manager.status(self.parent["id"])
        self.assertTrue(ready["promotable"])
        comparison["on_events"] = 19
        comparison["off_events"] = 21
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE agent_candidates SET comparison_json=? WHERE parent_agent_id=?",
                      (json.dumps(comparison), self.parent["id"]))
        blocked = self.manager.status(self.parent["id"])
        self.assertFalse(blocked["promotable"])

    def test_promote_replaces_live_model_keeps_logical_agent_and_removes_candidate(self):
        self.store.save_model(self.parent["id"], {"version": 10, "schema": {"version": 11, "entities": []}, "marker": "old"})
        status = self.manager.enqueue(self.parent["id"], "teach")
        candidate_id = status["candidate_id"]
        self.store.save_model(candidate_id, {"version": 10, "schema": {"version": 11, "entities": []}, "marker": "new"})
        self.store.set_training_state(candidate_id, "qualified", score=.9, samples=100, source="teach-rl-shadow",
                                      detail={"qualification_stale": True, "control_qualification": "stale"})
        comparison = {
            "samples": 40, "live_correct": 35, "candidate_correct": 39,
            "on_events": 20, "off_events": 20,
            "live_false_early": 1, "candidate_false_early": 0,
        }
        with self.store.lock, self.store.conn() as c:
            c.execute("""UPDATE agent_candidates SET state='comparing',build_revision=feedback_revision,dirty=0,
                       comparison_json=? WHERE parent_agent_id=?""",
                      (json.dumps(comparison), self.parent["id"]))
        result = self.manager.promote(self.parent["id"])
        self.assertEqual(result["agent_id"], self.parent["id"])
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(self.store.get_model(self.parent["id"])["marker"], "new")
        self.assertIsNone(self.store.get_agent(candidate_id))
        self.assertIsNone(self.manager.status(self.parent["id"]))
        with self.store.conn() as c:
            backups = c.execute("SELECT COUNT(*) FROM agent_generation_backups WHERE agent_id=?", (self.parent["id"],)).fetchone()[0]
        self.assertEqual(backups, 1)


if __name__ == "__main__":
    unittest.main()
