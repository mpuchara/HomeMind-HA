import json
import tempfile
import threading
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
from agent_explore import install as install_explore
from agent_workflow_actions import install as install_workflow
from context_tournament import ContextTournament
from experiments import Experiments


class FakeTeaching:
    def teach(self, engine, agent, desired=None, sample_ts=None):
        return {"ok": True, "desired_value": float(desired or 0.0)}
    def undo(self, engine, agent):
        return {"ok": True}
    def match(self, *args, **kwargs):
        return None


class FakeRLTeaching:
    def __init__(self, store):
        self.store = store
        with store.lock, store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL, sample_ts REAL NOT NULL, desired REAL NOT NULL,
                    previous_desired REAL, fingerprint TEXT NOT NULL, undone_ts REAL);
                CREATE TABLE IF NOT EXISTS teaching_rl_jobs (
                    agent_id TEXT PRIMARY KEY, requested_ts REAL NOT NULL,
                    state TEXT NOT NULL, original_inputs_json TEXT NOT NULL,
                    pre_schema_json TEXT NOT NULL, selected_inputs_json TEXT NOT NULL,
                    report_json TEXT NOT NULL DEFAULT '{}');
                """
            )
    def add_label(self, agent, desired, sample_ts):
        return {"ok": True}
    def undo(self, agent):
        return {"ok": True}
    def labels(self, agent_id):
        return []
    def status(self, agent_id):
        return {"state": "idle", "report": {}}


class FakeExecutor:
    def __init__(self):
        self.service = Mock(side_effect=AssertionError("Candidate Explore must not dispatch"))
        self._service = Mock(side_effect=AssertionError("Candidate Explore must not dispatch"))
        self.submit = Mock(side_effect=AssertionError("Explore orchestration must not bypass Live Executor ownership"))
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
    def __init__(self):
        self.calls = []
    def status_for(self, agent_id):
        return None
    def cancel(self, agent_id):
        return False
    def enqueue(self, agent_id, rebuild=False, reason="training"):
        call = {"agent_id": str(agent_id), "rebuild": bool(rebuild), "reason": str(reason)}
        self.calls.append(call)
        return {"state": "queued", "position": 1, **call}


class DummySchema:
    def __init__(self, entities):
        self.entities = list(entities or [])
        self.version = 11
    def labels(self):
        return {0: ["bias"]}


class DummyPolicy:
    VERSION = 10
    dims = 32
    def __init__(self, store, agent):
        self.agent = agent
        self.model = store.get_model(agent["id"]) or {}
        self.model_revision = self.model.get("model_revision") or "test-model"
        self.selection_meta = dict(self.model.get("selection_meta") or {})
        self.schema = DummySchema((self.model.get("schema") or {}).get("entities") or [])
        self.horizons = [1.0]
        self.lock = threading.RLock()
        head = SimpleNamespace(
            actions=[0.0, 1.0],
            a=[[1.0] * self.dims for _ in range(2)],
            b=[[0.0] * self.dims for _ in range(2)],
            ctx_sum=[[0.0] * self.dims for _ in range(2)],
            ctx_sq=[[0.0] * self.dims for _ in range(2)],
        )
        self.heads = {1.0: head}
    def features(self, state_map, history, at_ts=None):
        return {0: 1.0}, {0: ["bias"]}, {}
    def predict(self, features):
        desired = float(self.model.get("prediction", 0.0))
        chosen = {"index": int(desired >= .5), "value": desired, "mean": .8, "support": .9, "novelty": .1}
        return chosen, .9, [chosen], 1.0, .9, .1
    def serialize(self):
        model = dict(self.model)
        model["model_revision"] = self.model_revision
        model["schema"] = {"version": 11, "entities": list(self.schema.entities)}
        model["selection_meta"] = dict(self.selection_meta)
        return model


class AgentExploreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "explore.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Explore light", "target_entity": "light.explore", "target_property": "power",
            "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
            "exploration_step": 1, "input_entities": ["binary_sensor.presence"],
        })
        self.root_model = {
            "version": 10, "model_revision": "root-r1", "prediction": 0.0,
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 4}, "weights": {"root": 1},
        }
        self.store.save_model(self.root["id"], self.root_model)
        self.store.set_training_state(
            self.root["id"], "qualified", score=.95, samples=100,
            source="test", detail={"balanced": True},
        )
        self.store.update_agent(self.root["id"], {"mode": "control"})
        self.root = self.store.get_agent(self.root["id"])

        self.executor = FakeExecutor()
        self.queue = FakeQueue()
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=FakeRLTeaching(self.store), models={}, runtime={},
            executor=self.executor, temporal_history=None, entity_registry={}, context_relevance={},
            state_map={
                "light.explore": {"entity_id": "light.explore", "state": "off", "attributes": {}, "context": {}},
                "binary_sensor.presence": {"entity_id": "binary_sensor.presence", "state": "on", "attributes": {"device_class": "occupancy"}},
                "binary_sensor.new_presence": {"entity_id": "binary_sensor.new_presence", "state": "off", "attributes": {"device_class": "motion"}},
                "sensor.temperature": {"entity_id": "sensor.temperature", "state": "21", "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
            },
            own_command_echo=lambda *args, **kwargs: False,
            process_agent=lambda *args, **kwargs: None,
            wake_event=SimpleNamespace(set=lambda: None),
            lock=threading.RLock(),
        )
        self.engine.policy = lambda agent: DummyPolicy(self.store, agent)
        self.engine.experiments = Experiments(self.store, clock=lambda: time.time())
        self.engine.context_tournament = ContextTournament(self.store, self.engine)
        self.core = SimpleNamespace(
            STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
            TRAINING_QUEUE=self.queue, HISTORY=None,
        )
        manager = AgentCandidateManager(self.core, start_worker=False)
        manager = install_conservative_correct(manager)
        manager = install_lineage(manager)
        manager = install_workflow(manager)
        self.manager = install_explore(manager)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def model(self, agent_id):
        return self.store.get_model(agent_id)

    def make_g1(self):
        result = self.manager.workflow_autonomous(self.root["id"])
        return self.manager.lineage_status(result["child_generation_id"])

    def test_free_explore_uses_existing_experiments_and_creates_child_without_mutating_parent(self):
        before = json.dumps(self.model(self.root["id"]), sort_keys=True)
        experiments_identity = id(self.engine.experiments)
        result = self.manager.workflow_explore(self.root["id"], {
            "mode": "free",
            "config": {"focus": "environment", "intensity": .25, "interval": 600,
                       "daily_budget": 2, "observation_seconds": 45, "max_step": 1},
        })
        child = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(child["parent_generation_id"], f"root:{self.root['id']}")
        self.assertEqual(json.dumps(self.model(self.root["id"]), sort_keys=True), before)
        self.assertEqual(id(self.engine.experiments), experiments_identity)
        cfg = self.engine.experiments.status(self.root["id"])["config"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["focus"], "environment")
        self.assertEqual(cfg["intensity"], .25)
        self.assertEqual(cfg["interval"], 600)
        self.assertEqual(cfg["daily_budget"], 2)
        self.assertEqual(cfg["observation_seconds"], 45)
        self.assertEqual(cfg["max_step"], 1)
        self.assertEqual(self.manager._candidate_row(self.root["id"])["state"], "exploring")
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()
        self.executor._service.assert_not_called()

    def test_free_explore_result_queues_conservative_continuation_without_clear_learning(self):
        before = self.model(self.root["id"])
        result = self.manager.workflow_explore(self.root["id"], {
            "mode": "free",
            "config": {"focus": "presence", "intensity": .2, "interval": 300,
                       "daily_budget": 1, "observation_seconds": 10, "max_step": 1},
        })
        data = self.engine.experiments._get(self.root["id"])
        data["active"] = {"arm": 1, "x": {"bias": 1.0}, "kind": "probe", "focus": "presence"}
        self.engine.experiments._finish(self.root["id"], .2, "measured test outcome")
        session = self.manager.workflow_explore_status(self.root["id"])["session"]
        self.assertEqual(session["status"], "training")
        self.assertEqual(session["outcome_count"], 1)
        self.assertEqual(session["measured_outcomes"], 1)
        row = self.manager._candidate_row(self.root["id"])
        self.assertEqual(row["reason"], "explore_free_training")
        self.assertEqual(row["state"], "queued")
        original_clear = self.store.clear_learning
        self.store.clear_learning = Mock(wraps=original_clear)
        self.assertTrue(self.manager._start_build(row))
        self.assertEqual(self.queue.calls[-1]["reason"], "explore_free_continuation")
        self.assertFalse(self.queue.calls[-1]["rebuild"])
        self.store.clear_learning.assert_not_called()
        self.assertEqual(self.model(self.root["id"]), before)
        self.assertEqual(result["child_generation_id"], session["child_generation_id"])

    def test_free_explore_from_candidate_creates_grandchild_but_probe_owner_stays_root_live(self):
        g1 = self.make_g1()
        before = self.model(g1["agent_id"])
        result = self.manager.workflow_explore(g1["generation_id"], {
            "mode": "free",
            "config": {"focus": "presence", "intensity": .2, "interval": 300,
                       "daily_budget": 1, "observation_seconds": 10, "max_step": 1},
        })
        g2 = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(g2["parent_generation_id"], g1["generation_id"])
        self.assertEqual(self.model(g1["agent_id"]), before)
        self.assertTrue(self.engine.experiments.status(self.root["id"])["config"]["enabled"])
        self.assertFalse(self.engine.experiments.status(g1["agent_id"])["config"]["enabled"])
        self.assertEqual(result["session"]["probe_owner"], "live_executor_only")
        self.assertFalse(result["session"]["candidate_dispatch"])
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()

    def test_targeted_sensor_is_forced_only_on_child_and_parent_model_stays_immutable(self):
        before = json.dumps(self.model(self.root["id"]), sort_keys=True)
        result = self.manager.workflow_explore(self.root["id"], {
            "mode": "targeted_sensor", "sensor_entity": "binary_sensor.new_presence",
        })
        child = self.manager.lineage_status(result["child_generation_id"])
        state = self.engine.context_tournament.state(child["agent_id"])
        self.assertIn("binary_sensor.new_presence", state["challenger_features"])
        self.assertNotIn("binary_sensor.new_presence", state["active_features"])
        parent_state = self.engine.context_tournament.state(self.root["id"])
        self.assertNotIn("binary_sensor.new_presence", parent_state["active_features"])
        self.assertEqual(json.dumps(self.model(self.root["id"]), sort_keys=True), before)
        self.assertEqual(result["session"]["probe_owner"], "passive_shadow")
        self.assertFalse(result["session"]["candidate_dispatch"])
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()

    def test_targeted_candidate_shadow_is_passive_and_never_dispatches(self):
        import agent_candidate_shadow_runtime as shadow_runtime
        result = self.manager.workflow_explore(self.root["id"], {
            "mode": "targeted_sensor", "sensor_entity": "binary_sensor.new_presence",
        })
        child = self.manager.lineage_status(result["child_generation_id"])
        observed = shadow_runtime._predict_candidate(
            self.manager, child, dict(self.engine.state_map), time.time()
        )
        self.assertIsNotNone(observed)
        runtime = self.engine.context_tournament.shadow_status(
            self.store.get_agent_config(child["agent_id"])
        )
        self.assertFalse(runtime["controls_device"])
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()
        self.executor._service.assert_not_called()

    def test_targeted_sensor_priority_does_not_override_negative_future_evidence(self):
        result = self.manager.workflow_explore(self.root["id"], {
            "mode": "targeted_sensor", "sensor_entity": "binary_sensor.new_presence",
        })
        child = self.manager.lineage_status(result["child_generation_id"])
        sensor = "binary_sensor.new_presence"
        original_status = self.engine.context_tournament.shadow_status
        self.engine.context_tournament.shadow_status = lambda agent: {
            "mode": "shadow_only", "controls_device": False,
            "challengers": [{
                "entity_id": sensor, "samples": 1000, "days_observed": 30.0,
                "gain": -0.01, "sensor_quality": 1.0, "promotion_ready": False,
                "promotion_checks": {"samples": True, "days": True, "gain": False},
            }],
        }
        try:
            status = self.manager.workflow_explore_status(self.root["id"])["session"]
        finally:
            self.engine.context_tournament.shadow_status = original_status
        self.assertEqual(status["status"], "no_gain")
        self.assertEqual(status["result_message"], "no measurable gain")
        self.assertIn(sensor, self.engine.context_tournament.state(child["agent_id"])["challenger_features"])
        self.assertNotIn(sensor, self.engine.context_tournament.state(child["agent_id"])["active_features"])
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()

    def test_targeted_sensor_from_candidate_creates_direct_grandchild(self):
        g1 = self.make_g1()
        before = self.model(g1["agent_id"])
        result = self.manager.workflow_explore(g1["generation_id"], {
            "mode": "targeted_sensor", "sensor_entity": "binary_sensor.new_presence",
        })
        g2 = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(g2["parent_generation_id"], g1["generation_id"])
        self.assertEqual(self.model(g1["agent_id"]), before)
        state = self.engine.context_tournament.state(g2["agent_id"])
        self.assertIn("binary_sensor.new_presence", state["challenger_features"])
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()

    def test_targeted_sensor_rejects_unavailable_entity_before_creating_child(self):
        with self.assertRaisesRegex(ValueError, "not currently available"):
            self.manager.workflow_explore(self.root["id"], {
                "mode": "targeted_sensor", "sensor_entity": "binary_sensor.missing",
            })
        lineage = self.manager.list_lineage(self.root["id"])
        self.assertEqual([r for r in lineage if r.get("generation_type") == "candidate"], [])


class ExploreUiContractTests(unittest.TestCase):
    def test_experiments_module_is_now_two_mode_explore_ui(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "adaptive_ai/src/static/experiments.js").read_text(encoding="utf-8")
        self.assertIn("Free exploration", source)
        self.assertIn("Targeted sensor", source)
        self.assertIn("mode:'free'", source)
        self.assertIn("mode:'targeted_sensor'", source)
        self.assertIn("intensity", source)
        self.assertIn("interval", source)
        self.assertIn("daily_budget", source)
        self.assertIn("observation_seconds", source)
        self.assertIn("no measurable gain", source)
        self.assertIn("Live → ActionIntent → Executor", source)
        self.assertNotIn("api/agents/${encodeURIComponent(id)}/experiments", source)

    def test_explore_buttons_are_enabled_by_final_generation_binding(self):
        root = Path(__file__).resolve().parents[1]
        binding = (root / "adaptive_ai/src/static/explore_ui.js").read_text(encoding="utf-8")
        html = (root / "adaptive_ai/src/static/index.html").read_text(encoding="utf-8")
        candidate = (root / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        version = json.loads((root / "adaptive_ai/BUILD_INFO.json").read_text(encoding="utf-8"))["version"]
        self.assertIn("button.disabled=false", binding)
        self.assertIn("window.openExplore", binding)
        self.assertIn("data-generation-id", candidate)
        self.assertIn("candidate-explore-result", candidate)
        self.assertIn(f"explore_ui.js?v={version}", html)

    def test_backend_is_orchestration_not_a_second_learning_subsystem(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "adaptive_ai/src/agent_explore.py").read_text(encoding="utf-8")
        self.assertNotIn("Experiments(", source)
        self.assertNotIn("ContextTournament(", source)
        self.assertIn("manager.engine.experiments", source)
        self.assertIn("manager.engine, \"context_tournament\"", source)
        self.assertNotIn("executor.submit", source)
        self.assertNotIn("._service(", source)


if __name__ == "__main__":
    unittest.main()
