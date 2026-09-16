import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_conservative_correct import install as install_conservative_correct
from agent_candidate_lineage import install as install_lineage
from agent_candidate_shadow_runtime import install as install_shadow_runtime
from agent_candidate_user_promotion import install as install_user_promotion


class FakeTeaching:
    def teach(self, engine, agent, desired=None, sample_ts=None):
        return {"ok": True}

    def undo(self, engine, agent):
        return {"ok": True}


class FakeRLTeaching:
    def add_label(self, agent, desired, sample_ts):
        return {"ok": True}

    def undo(self, agent):
        return {"ok": True}

    def labels(self, agent_id):
        return []

    def status(self, agent_id):
        return {"state": "idle"}


class FakeExecutor:
    def __init__(self):
        self.release_control = Mock()
        self.service = Mock(side_effect=AssertionError("Candidate observation must never dispatch"))

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


class FakeQueue:
    def status_for(self, agent_id):
        return None

    def cancel(self, agent_id):
        return False

    def enqueue(self, *args, **kwargs):
        return {"state": "queued"}


class DummyPolicy:
    def __init__(self, store, agent):
        self.store = store
        self.agent = agent
        self.model = store.get_model(agent["id"]) or {}
        self.actions = [0.0, 1.0]
        self.horizons = [1.0]
        self.model_revision = self.model.get("model_revision")
        self.selection_meta = dict(self.model.get("selection_meta") or {})
        self.schema = SimpleNamespace(version=(self.model.get("schema") or {}).get("version"))

    def features(self, state_map, history, at_ts=None):
        present = 1.0 if str(((state_map or {}).get("binary_sensor.presence") or {}).get("state")) == "on" else 0.0
        return {0: 1.0, 1: present}, {}, {"at_ts": at_ts}

    def predict(self, features):
        return {"value": float(self.model.get("prediction", 0.0))}, float(self.model.get("confidence", .9)), [], 1.0, 1.0, 0.0


class BlockedCandidateFutureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "blocked-future.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Blocked future light",
            "target_entity": "light.blocked_future",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": .25,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.presence"],
        })
        self.store.save_model(self.root["id"], {
            "version": 10,
            "model_revision": "g0",
            "schema": {"version": 1, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 1},
            "prediction": 0.0,
            "confidence": .9,
        })
        self.store.set_training_state(self.root["id"], "qualified", score=.9, samples=50, source="test", detail={})
        self.root = self.store.get_agent(self.root["id"])
        self.executor = FakeExecutor()
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=FakeRLTeaching(), models={}, runtime={},
            executor=self.executor, temporal_history=None,
            process_agent=lambda *args, **kwargs: None,
            own_command_echo=lambda *args, **kwargs: False,
            wake_event=SimpleNamespace(set=lambda: None),
        )
        self.engine.policy = lambda agent: DummyPolicy(self.store, agent)
        self.core = SimpleNamespace(
            STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
            TRAINING_QUEUE=FakeQueue(), HISTORY=None,
        )
        manager = AgentCandidateManager(self.core, start_worker=False)
        manager = install_conservative_correct(manager)
        manager = install_lineage(manager)
        manager = install_shadow_runtime(manager)
        self.manager = install_user_promotion(manager)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    @staticmethod
    def states(light="off"):
        return {
            "light.blocked_future": {
                "entity_id": "light.blocked_future", "state": light,
                "attributes": {}, "context": {},
            },
            "binary_sensor.presence": {
                "entity_id": "binary_sensor.presence", "state": "on", "attributes": {},
            },
        }

    def set_model_prediction(self, agent_id, prediction, revision):
        model = self.store.get_model(agent_id) or {
            "version": 10, "schema": {"version": 1}, "selection_meta": {"schema_revision": 1},
        }
        model["prediction"] = float(prediction)
        model["confidence"] = .95
        model["model_revision"] = revision
        self.store.save_model(agent_id, model)
        self.engine.models.pop(agent_id, None)

    def mark_edge(self, parent_id, candidate_id, generation_id, *, state, gate_passed):
        gate = '{"passed":true,"status":"passed"}' if gate_passed else '{"passed":false,"status":"failed","reasons":["regression"]}'
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE agent_candidates SET state=?,dirty=0,build_revision=feedback_revision,
                   offline_gate_json=?,comparison_json='{}',comparison_started_ts=?,updated_ts=?
                   WHERE parent_agent_id=? AND candidate_id=?""",
                (state, gate, time.time(), time.time(), str(parent_id), str(candidate_id)),
            )
            c.execute(
                "UPDATE agent_candidate_generations SET lifecycle_state=?,updated_ts=? WHERE generation_id=?",
                (state, time.time(), str(generation_id)),
            )

    def test_blocked_g2_leaf_collects_future_samples_and_stays_blocked(self):
        g1_status = self.manager.enqueue(self.root["id"], "teach")
        self.set_model_prediction(g1_status["candidate_id"], 0.0, "g1")
        self.mark_edge(
            self.root["id"], g1_status["candidate_id"], g1_status["generation_id"],
            state="comparing", gate_passed=True,
        )
        g1 = self.manager.lineage_status(g1_status["generation_id"])

        g2 = self.manager.spawn_child(g1["generation_id"], "candidate_correct")
        self.set_model_prediction(g2["agent_id"], 1.0, "g2")
        self.mark_edge(
            g1_status["candidate_id"], g2["agent_id"], g2["generation_id"],
            state="offline_blocked", gate_passed=False,
        )

        self.engine.runtime[self.root["id"]] = {"last_prediction": 0.0, "last_confidence": .9}
        bundle = self.manager.after_live_process(self.root, self.states("off"))
        self.assertIn(g1["generation_id"], bundle["results"])
        self.assertIn(g2["generation_id"], bundle["results"])

        inserted = self.manager.before_live_process(self.root, self.states("on"))
        self.assertTrue(inserted)

        comparison = self.manager.generation_comparison(g2["generation_id"])
        self.assertEqual(comparison["pairs"], 1)
        self.assertEqual(comparison["summary"]["samples"], 1)
        self.assertEqual(comparison["summary"]["child_wins"], 1)

        leaf = self.manager.lineage_status(g2["generation_id"])
        self.assertEqual(leaf["state"], "offline_blocked")
        self.assertEqual(leaf["lineage_state"], "offline_blocked")
        self.assertEqual(leaf["comparison"]["samples"], 1)
        self.assertFalse(leaf["offline_gate"]["passed"])
        self.executor.service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
