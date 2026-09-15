import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_conservative_correct import install as install_conservative_correct
from agent_candidate_lineage import install as install_lineage


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
        self.service = Mock(side_effect=AssertionError("Candidate lineage must never dispatch HA service"))

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

    def features(self, state_map, history, at_ts=None):
        return {0: 1.0}, {}, {}

    def predict(self, features):
        return {"value": float(self.model.get("prediction", 0.0))}, .9, [], 1.0, 1.0, 0.0

    def serialize(self):
        return dict(self.model)


class CandidateLineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "candidate-lineage.db"
        self.store = storage.Store(self.db)
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Lineage light",
            "target_entity": "light.lineage",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": .25,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.presence"],
        })
        self.root_model = {
            "version": 10,
            "model_revision": "g0-revision",
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 7, "primary_local_sensor": "binary_sensor.presence"},
            "prediction": 0.0,
            "weights": {"marker": "G0"},
        }
        self.store.save_model(self.root["id"], self.root_model)
        self.store.set_training_state(self.root["id"], "qualified", score=.95, samples=100, source="test", detail={})
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
        base = AgentCandidateManager(self.core, start_worker=False)
        self.manager = install_lineage(install_conservative_correct(base))

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _g1(self):
        status = self.manager.enqueue(self.root["id"], "teach")
        return status, self.manager.lineage_status(status["generation_id"])

    def test_live_g0_to_candidate_g1(self):
        status, generation = self._g1()
        self.assertEqual(generation["root_agent_id"], self.root["id"])
        self.assertEqual(generation["generation_number"], 1)
        self.assertEqual(generation["parent_type"], "live")
        self.assertEqual(generation["parent_generation_id"], f"root:{self.root['id']}")
        self.assertEqual(self.store.get_model(status["candidate_id"]), self.store.get_model(self.root["id"]))

    def test_g1_to_g2_and_g2_clone_comes_from_g1_not_g0(self):
        g1_status, g1 = self._g1()
        g1_id = g1_status["candidate_id"]
        g1_model = self.store.get_model(g1_id)
        g1_model["model_revision"] = "g1-revision"
        g1_model["prediction"] = 1.0
        g1_model["weights"] = {"marker": "G1"}
        self.store.save_model(g1_id, g1_model)

        g2 = self.manager.spawn_child(g1["generation_id"], "candidate_correct")
        g2_model = self.store.get_model(g2["agent_id"])

        self.assertEqual(g2["generation_number"], 2)
        self.assertEqual(g2["parent_generation_id"], g1["generation_id"])
        self.assertEqual(g2["parent_type"], "candidate")
        self.assertEqual(g2_model["weights"], {"marker": "G1"})
        self.assertEqual(g2_model["model_revision"], "g1-revision")
        self.assertNotEqual(g2_model, self.root_model)

    def test_g2_comparison_parent_is_g1(self):
        g1_status, g1 = self._g1()
        g2 = self.manager.spawn_child(g1["generation_id"])
        status = self.manager.lineage_status(g2["generation_id"])
        self.assertEqual(status["comparison_parent_generation_id"], g1["generation_id"])
        self.assertEqual(status["comparison_parent_type"], "candidate")
        self.assertEqual(status["parent_agent_id"], g1_status["candidate_id"])

    def test_lineage_survives_restart(self):
        _, g1 = self._g1()
        g2 = self.manager.spawn_child(g1["generation_id"])
        root_id = self.root["id"]
        expected = [(x["generation_id"], x["generation_number"], x["parent_generation_id"]) for x in self.manager.list_lineage(root_id)]
        self.manager.stop()

        # Recreate manager objects against the same SQLite file. Migration is additive
        # and idempotent; no in-memory lineage state is required.
        store2 = storage.Store(self.db)
        engine2 = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=FakeRLTeaching(), models={}, runtime={},
            executor=FakeExecutor(), temporal_history=None,
            process_agent=lambda *args, **kwargs: None,
            own_command_echo=lambda *args, **kwargs: False,
            wake_event=SimpleNamespace(set=lambda: None),
        )
        engine2.policy = lambda agent: DummyPolicy(store2, agent)
        core2 = SimpleNamespace(STORE=store2, ENGINE=engine2, Handler=FakeHandler, TRAINING_QUEUE=FakeQueue(), HISTORY=None)
        manager2 = install_lineage(install_conservative_correct(AgentCandidateManager(core2, start_worker=False)))
        try:
            actual = [(x["generation_id"], x["generation_number"], x["parent_generation_id"]) for x in manager2.list_lineage(root_id)]
            self.assertEqual(actual, expected)
            self.assertIsNotNone(manager2.lineage_status(g2["generation_id"]))
        finally:
            manager2.stop()

    def test_discard_g2_does_not_destroy_g1(self):
        g1_status, g1 = self._g1()
        g1_id = g1_status["candidate_id"]
        g1_model = self.store.get_model(g1_id)
        g2 = self.manager.spawn_child(g1["generation_id"])
        result = self.manager.discard(g1_id)
        self.assertTrue(result["discarded"])
        self.assertEqual(self.store.get_model(g1_id), g1_model)
        self.assertIsNotNone(self.store.get_agent_config(g1_id))
        self.assertEqual(self.manager.lineage_status(g2["generation_id"])["state"], "discarded")

    def test_config_fingerprints_are_generation_aware(self):
        g1_status, g1 = self._g1()
        self.store.update_agent(g1_status["candidate_id"], {"deadband": .25})
        g2 = self.manager.spawn_child(g1["generation_id"])
        self.assertNotEqual(g1["config_fingerprint"], g2["config_fingerprint"])
        self.assertEqual(g2["config_fingerprint"], self.manager.lineage_status(g2["generation_id"])["config_fingerprint"])

    def test_only_one_branch_can_be_active(self):
        _, g1 = self._g1()
        self.manager.spawn_child(g1["generation_id"])
        with self.assertRaisesRegex(ValueError, "only one active branch"):
            self.manager.spawn_child(g1["generation_id"])

    def test_candidates_remain_hidden_from_live_enumeration(self):
        g1_status, g1 = self._g1()
        g2 = self.manager.spawn_child(g1["generation_id"])
        visible = {a["id"] for a in self.store.list_agents()}
        self.assertIn(self.root["id"], visible)
        self.assertNotIn(g1_status["candidate_id"], visible)
        self.assertNotIn(g2["agent_id"], visible)

    def test_lineage_creation_never_dispatches_service(self):
        _, g1 = self._g1()
        self.manager.spawn_child(g1["generation_id"])
        self.executor.service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
