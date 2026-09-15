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
from agent_candidate_shadow_context import install as install_shadow_context


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
        self.service = Mock(side_effect=AssertionError("Candidate Shadow must never dispatch"))
        self.release_control = Mock()
    @contextmanager
    def target_lock(self, entity_id):
        yield


class FakeHandler:
    def do_GET(self): return None
    def do_POST(self): return None
    def do_DELETE(self): return None


class FakeQueue:
    def status_for(self, agent_id): return None
    def cancel(self, agent_id): return False
    def enqueue(self, *args, **kwargs): return {"state": "queued"}


class ContextPolicy:
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
        presence = (state_map or {}).get("binary_sensor.presence") or {}
        value = 1.0 if str(presence.get("state")) == "on" else 0.0
        return {0: 1.0, 1: value}, {}, {"at_ts": at_ts}
    def predict(self, features):
        value = float(features.get(1, 0.0))
        return {"value": value}, .9, [], 1.0, 1.0, 0.0
    def serialize(self): return dict(self.model)


class CandidateShadowExactContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "shadow-context.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Exact context light", "target_entity": "light.exact", "target_property": "power",
            "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
            "exploration_step": 1, "input_entities": ["binary_sensor.presence"],
        })
        model = {
            "version": 10, "model_revision": "g0", "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 1},
        }
        self.store.save_model(self.root["id"], model)
        self.store.set_training_state(self.root["id"], "qualified", score=.9, samples=100, source="test", detail={})
        self.root = self.store.get_agent(self.root["id"])
        self.executor = FakeExecutor()
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=FakeRLTeaching(), models={}, runtime={}, executor=self.executor,
            temporal_history=None, process_agent=lambda *args, **kwargs: None,
            own_command_echo=lambda *args, **kwargs: False, wake_event=SimpleNamespace(set=lambda: None),
        )
        self.engine.policy = lambda agent: ContextPolicy(self.store, agent)
        self.core = SimpleNamespace(STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
                                    TRAINING_QUEUE=FakeQueue(), HISTORY=None)
        manager = AgentCandidateManager(self.core, start_worker=False)
        manager = install_conservative_correct(manager)
        manager = install_lineage(manager)
        manager = install_shadow_runtime(manager)
        self.manager = install_shadow_context(manager)

        status = self.manager.enqueue(self.root["id"], "teach")
        self.candidate_id = status["candidate_id"]
        self.generation_id = status["generation_id"]
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE agent_candidates SET state='comparing',dirty=0,build_revision=feedback_revision,
                   offline_gate_json='{"passed":true,"status":"passed"}',comparison_json='{}',updated_ts=?
                   WHERE parent_agent_id=?""", (time.time(), self.root["id"]),
            )
            c.execute("UPDATE agent_candidate_generations SET lifecycle_state='comparing',updated_ts=? WHERE generation_id=?",
                      (time.time(), self.generation_id))

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    @staticmethod
    def states(presence):
        return {
            "light.exact": {"entity_id": "light.exact", "state": "off", "attributes": {}, "context": {}},
            "binary_sensor.presence": {"entity_id": "binary_sensor.presence", "state": presence, "attributes": {}},
        }

    def test_shadow_uses_exact_state_map_seen_by_parent_policy_not_outer_snapshot(self):
        outer = self.states("off")
        exact = self.states("on")
        self.engine.runtime[self.root["id"]] = {"last_prediction": 1.0, "last_confidence": .88, "last_inference_ts": 10.0}

        self.manager.before_live_process(self.root, outer)
        # Simulate Engine.process_agent refreshing its internal state snapshot after the
        # wrapper's outer snapshot. The exact-context guard observes this policy call.
        self.engine.runtime[self.root["id"]]["last_inference_ts"] = 20.0
        policy = self.engine.policy(self.root)
        policy.features(exact, None, at_ts=20.0)
        bundle = self.manager.after_live_process(self.root, outer)

        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["current"], 0.0)
        self.assertEqual(bundle["results"][self.generation_id]["desired"], 1.0)
        self.executor.service.assert_not_called()

    def test_shadow_does_not_run_when_parent_did_not_perform_fresh_inference(self):
        outer = self.states("off")
        self.engine.runtime[self.root["id"]] = {"last_prediction": 0.0, "last_confidence": .8, "last_inference_ts": 10.0}
        self.manager.before_live_process(self.root, outer)
        result = self.manager.after_live_process(self.root, outer)
        self.assertIsNone(result)
        with self.store.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM candidate_generation_decisions WHERE generation_id=?",
                          (self.generation_id,)).fetchone()[0]
        self.assertEqual(n, 0)
        self.executor.service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
