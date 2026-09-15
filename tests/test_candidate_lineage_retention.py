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
from agent_candidate_lineage_guards import install as install_lineage_guards
from agent_candidate_lineage_retention import install as install_lineage_retention


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
        self.service = Mock(side_effect=AssertionError("Candidate lineage must never dispatch"))

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


class CandidateLineageRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "lineage-retention.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Lineage retention light",
            "target_entity": "light.lineage_retention",
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
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 1},
            "prediction": 0.0,
            "marker": "G0",
        })
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
        manager = install_conservative_correct(AgentCandidateManager(self.core, start_worker=False))
        manager = install_lineage(manager)
        manager = install_lineage_retention(manager)
        self.manager = install_lineage_guards(manager)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _first(self):
        status = self.manager.enqueue(self.root["id"], "teach")
        return status, self.manager.lineage_status(status["generation_id"])

    def test_parent_candidate_is_frozen_after_child_exists(self):
        g1_status, g1 = self._first()
        g1_model = self.store.get_model(g1_status["candidate_id"])
        self.manager.spawn_child(g1["generation_id"], "candidate_correct")
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.manager.enqueue(g1_status["candidate_id"], "teach")
        self.assertEqual(self.store.get_model(g1_status["candidate_id"]), g1_model)

    def test_candidate_enqueue_on_leaf_creates_next_generation(self):
        g1_status, g1 = self._first()
        result = self.manager.enqueue(g1_status["candidate_id"], "teach")
        self.assertEqual(result["generation_number"], 2)
        self.assertEqual(result["parent_generation_id"], g1["generation_id"])
        self.assertEqual(result["parent_type"], "candidate")

    def test_full_model_retention_is_bounded_to_three_candidate_generations(self):
        _, g1 = self._first()
        g2 = self.manager.spawn_child(g1["generation_id"])
        g3 = self.manager.spawn_child(g2["generation_id"])
        g4 = self.manager.spawn_child(g3["generation_id"])

        lineage = self.manager.list_lineage(self.root["id"])
        candidates = [x for x in lineage if x.get("generation_type") == "candidate"]
        retained = [x for x in candidates if int(x.get("model_retained") or 0)]
        self.assertLessEqual(len(retained), 3)
        by_number = {int(x["generation_number"]): x for x in candidates}
        self.assertEqual(int(by_number[1]["model_retained"]), 0)
        self.assertEqual(by_number[1]["lifecycle_state"], "pruned")
        self.assertEqual(int(by_number[3]["model_retained"]), 1)
        self.assertEqual(int(by_number[4]["model_retained"]), 1)
        self.assertIsNotNone(self.store.get_model(g3["agent_id"]))
        self.assertIsNotNone(self.store.get_model(g4["agent_id"]))

    def test_root_feedback_advances_from_current_tip_after_descendant_exists(self):
        _, g1 = self._first()
        g2 = self.manager.spawn_child(g1["generation_id"])
        result = self.manager.enqueue(self.root["id"], "teach")
        self.assertEqual(result["generation_number"], 3)
        self.assertEqual(result["parent_generation_id"], g2["generation_id"])

    def test_lineage_extensions_do_not_dispatch_service(self):
        _, g1 = self._first()
        g2 = self.manager.spawn_child(g1["generation_id"])
        self.manager.spawn_child(g2["generation_id"])
        self.executor.service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
