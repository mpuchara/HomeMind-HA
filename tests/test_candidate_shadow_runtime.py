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
        self.service = Mock(side_effect=AssertionError("Candidate Shadow must never dispatch HA service"))

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
        state = (state_map or {}).get("binary_sensor.presence") or {}
        present = 1.0 if str(state.get("state")) == "on" else 0.0
        return {0: 1.0, 1: present}, {}, {"at_ts": at_ts}

    def predict(self, features):
        return {"value": float(self.model.get("prediction", 0.0))}, float(self.model.get("confidence", .9)), [], 1.0, 1.0, 0.0

    def serialize(self):
        return dict(self.model)


class CandidateShadowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "candidate-shadow.db"
        self.store = storage.Store(self.db)
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Shadow light",
            "target_entity": "light.shadow",
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
            "model_revision": "g0-rev",
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 10},
            "prediction": 0.0,
            "confidence": .82,
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
        self.manager = install_shadow_runtime(install_lineage(install_conservative_correct(base)))

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    @staticmethod
    def _states(light="off", presence="on", user_id=None, parent_id=None):
        context = {}
        if user_id is not None:
            context["user_id"] = user_id
        if parent_id is not None:
            context["parent_id"] = parent_id
        return {
            "light.shadow": {"entity_id": "light.shadow", "state": light, "attributes": {}, "context": context},
            "binary_sensor.presence": {"entity_id": "binary_sensor.presence", "state": presence, "attributes": {}},
        }

    def _mark_trained(self, parent_agent_id, candidate_id, generation_id):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE agent_candidates SET state='comparing',dirty=0,build_revision=feedback_revision,
                   offline_gate_json=?,comparison_json='{}',comparison_started_ts=?,updated_ts=?
                   WHERE parent_agent_id=? AND candidate_id=?""",
                ('{"passed":true,"status":"passed"}', time.time(), time.time(), str(parent_agent_id), str(candidate_id)),
            )
            c.execute(
                """UPDATE agent_candidate_generations SET lifecycle_state='comparing',updated_ts=?
                   WHERE generation_id=?""",
                (time.time(), str(generation_id)),
            )

    def _g1(self, prediction=1.0, confidence=.91):
        status = self.manager.enqueue(self.root["id"], "teach")
        gid = status["generation_id"]
        model = self.store.get_model(status["candidate_id"])
        model["prediction"] = float(prediction)
        model["confidence"] = float(confidence)
        model["model_revision"] = "g1-rev"
        self.store.save_model(status["candidate_id"], model)
        self.engine.models.pop(status["candidate_id"], None)
        self._mark_trained(self.root["id"], status["candidate_id"], gid)
        return status, self.manager.lineage_status(gid)

    def _run_shadow(self, states=None):
        states = states or self._states()
        self.engine.runtime[self.root["id"]] = {"last_prediction": 0.0, "last_confidence": .82}
        return self.manager.after_live_process(self.root, states)

    def test_candidate_shadow_inference_runs_after_training_and_exposes_card_values(self):
        status, generation = self._g1(prediction=1.0, confidence=.93)
        bundle = self._run_shadow()
        self.assertIsNotNone(bundle)
        self.assertIn(generation["generation_id"], bundle["results"])
        card = self.manager.status(self.root["id"])
        self.assertTrue(card["shadow_active"])
        self.assertEqual(card["shadow_current"], 0.0)
        self.assertEqual(card["candidate_desired"], 1.0)
        self.assertAlmostEqual(card["candidate_confidence"], .93)
        self.assertEqual(card["shadow_model_revision"], "g1-rev")
        self.assertEqual(card["shadow_schema_revision"], "10")

    def test_candidate_shadow_never_dispatches_home_assistant_service(self):
        self._g1(prediction=1.0)
        self._run_shadow()
        self.executor.service.assert_not_called()
        self.executor.release_control.assert_not_called()

    def test_parent_and_child_receive_the_exact_same_shadow_event(self):
        g1_status, g1 = self._g1(prediction=0.0, confidence=.8)
        g2 = self.manager.spawn_child(g1["generation_id"], "candidate_correct")
        g2_model = self.store.get_model(g2["agent_id"])
        g2_model["prediction"] = 1.0
        g2_model["confidence"] = .95
        g2_model["model_revision"] = "g2-rev"
        self.store.save_model(g2["agent_id"], g2_model)
        self.engine.models.pop(g2["agent_id"], None)
        self._mark_trained(g1_status["candidate_id"], g2["agent_id"], g2["generation_id"])

        bundle = self._run_shadow()
        self.assertIn(g1["generation_id"], bundle["results"])
        self.assertIn(g2["generation_id"], bundle["results"])
        with self.store.conn() as c:
            parent = dict(c.execute(
                "SELECT * FROM candidate_generation_decisions WHERE generation_id=? ORDER BY ts DESC LIMIT 1",
                (g1["generation_id"],),
            ).fetchone())
            child = dict(c.execute(
                "SELECT * FROM candidate_generation_decisions WHERE generation_id=? ORDER BY ts DESC LIMIT 1",
                (g2["generation_id"],),
            ).fetchone())
        self.assertEqual(parent["event_id"], child["event_id"])
        self.assertEqual(parent["ts"], child["ts"])
        self.assertEqual(parent["current"], child["current"])

    def test_generation_history_is_separate_and_never_replays_a_new_policy(self):
        g1_status, g1 = self._g1(prediction=1.0)
        bundle = self._run_shadow()
        ts = bundle["ts"]
        root_gid = f"root:{self.root['id']}"
        root_history = self.manager.generation_history(root_gid, ts - 1, ts + 1)
        g1_history = self.manager.generation_history(g1["generation_id"], ts - 1, ts + 1)
        self.assertEqual(root_history["points"][0]["desired"], 0.0)
        self.assertEqual(g1_history["points"][0]["desired"], 1.0)
        self.assertNotEqual(root_history["points"][0]["generation_id"], g1_history["points"][0]["generation_id"])

        # Mutating today's model cannot rewrite yesterday's observed Desired.
        model = self.store.get_model(g1_status["candidate_id"])
        model["prediction"] = 0.0
        model["model_revision"] = "g1-new-policy"
        self.store.save_model(g1_status["candidate_id"], model)
        historical = self.manager.generation_history(g1["generation_id"], ts - 1, ts + 1)
        self.assertEqual(historical["points"][0]["desired"], 1.0)
        self.assertFalse(historical["policy_replay_used"])

    def test_generation_downtime_remains_a_gap(self):
        _, g1 = self._g1(prediction=1.0)
        bundle = self._run_shadow()
        future = bundle["ts"] + 300.0
        self.assertIsNone(self.manager.generation_decision_at(g1["generation_id"], future))
        history = self.manager.generation_history(g1["generation_id"], future, future + 120.0)
        self.assertEqual(history["points"], [])
        self.assertEqual(history["gaps"][0]["reason"], "generation_not_observed")

    def test_paired_ab_scoring_uses_one_shared_future_outcome(self):
        _, g1 = self._g1(prediction=1.0, confidence=.94)
        bundle = self._run_shadow(self._states(light="off"))
        self.assertEqual(bundle["current"], 0.0)

        # The one future ON transition is evaluated against both predictions from the same
        # stored prediction event. Root G0 predicted OFF, G1 predicted ON.
        self.manager.before_live_process(self.root, self._states(light="on"))
        comparison = self.manager.generation_comparison(g1["generation_id"])
        self.assertEqual(comparison["pairs"], 1)
        self.assertEqual(comparison["summary"]["child_wins"], 1)
        with self.store.conn() as c:
            pair = dict(c.execute("SELECT * FROM candidate_generation_pairs").fetchone())
        self.assertEqual(pair["prediction_event_id"], bundle["event_id"])
        self.assertEqual(pair["outcome"], 1.0)
        self.assertEqual(pair["parent_prediction"], 0.0)
        self.assertEqual(pair["child_prediction"], 1.0)
        self.assertEqual(pair["parent_correct"], 0)
        self.assertEqual(pair["child_correct"], 1)
        self.assertEqual(pair["paired_result"], "child_win")
        self.assertEqual(pair["evidence_kind"], "external_target_transition")
        self.assertEqual(pair["calibration_eligible"], 0)
        self.assertIsNone(pair["dependency_cluster"])
        self.assertIsNotNone(pair["child_lead_seconds"])

    def test_direct_user_transition_is_independent_calibration_evidence(self):
        _, g1 = self._g1(prediction=1.0, confidence=.94)
        self._run_shadow(self._states(light="off"))
        self.manager.before_live_process(
            self.root, self._states(light="on", user_id="user-123")
        )
        with self.store.conn() as db:
            row = dict(db.execute(
                "SELECT * FROM candidate_generation_pairs ORDER BY outcome_ts DESC LIMIT 1"
            ).fetchone())
        self.assertEqual(row["evidence_kind"], "manual_user_target_change")
        self.assertEqual(row["calibration_eligible"], 1)
        self.assertIn("manual:user-123:light.shadow:", row["dependency_cluster"])
        self.assertEqual(
            self.manager.generation_comparison(g1["generation_id"])["pairs"], 1
        )

    def test_user_context_with_parent_is_not_independent_calibration(self):
        self._g1(prediction=1.0, confidence=.94)
        self._run_shadow(self._states(light="off"))
        self.manager.before_live_process(
            self.root,
            self._states(light="on", user_id="user-123", parent_id="automation-parent"),
        )
        with self.store.conn() as db:
            row = dict(db.execute(
                "SELECT * FROM candidate_generation_pairs ORDER BY outcome_ts DESC LIMIT 1"
            ).fetchone())
        self.assertEqual(row["evidence_kind"], "external_target_transition")
        self.assertEqual(row["calibration_eligible"], 0)

    def test_restart_preserves_generation_history_and_paired_comparison(self):
        _, g1 = self._g1(prediction=1.0)
        bundle = self._run_shadow(self._states(light="off"))
        self.manager.before_live_process(self.root, self._states(light="on"))
        before_history = self.manager.generation_history(g1["generation_id"], bundle["ts"] - 1, bundle["ts"] + 1)
        before_comparison = self.manager.generation_comparison(g1["generation_id"])
        self.manager.stop()

        class RestartHandler:
            def do_GET(self):
                return None
            def do_POST(self):
                return None
            def do_DELETE(self):
                return None

        store2 = storage.Store(self.db)
        executor2 = FakeExecutor()
        engine2 = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=FakeRLTeaching(), models={}, runtime={}, executor=executor2,
            temporal_history=None, process_agent=lambda *args, **kwargs: None,
            own_command_echo=lambda *args, **kwargs: False, wake_event=SimpleNamespace(set=lambda: None),
        )
        engine2.policy = lambda agent: DummyPolicy(store2, agent)
        core2 = SimpleNamespace(STORE=store2, ENGINE=engine2, Handler=RestartHandler, TRAINING_QUEUE=FakeQueue(), HISTORY=None)
        manager2 = install_shadow_runtime(install_lineage(install_conservative_correct(AgentCandidateManager(core2, start_worker=False))))
        try:
            after_history = manager2.generation_history(g1["generation_id"], bundle["ts"] - 1, bundle["ts"] + 1)
            after_comparison = manager2.generation_comparison(g1["generation_id"])
            self.assertEqual(after_history["points"], before_history["points"])
            self.assertEqual(after_comparison["pairs"], before_comparison["pairs"])
            self.assertEqual(after_comparison["summary"]["child_wins"], before_comparison["summary"]["child_wins"])
            executor2.service.assert_not_called()
        finally:
            manager2.stop()


if __name__ == "__main__":
    unittest.main()
