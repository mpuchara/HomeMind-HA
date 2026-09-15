import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import support
import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_conservative_correct import _offline_gate, install as install_conservative_correct
from teaching_rl import fingerprint


class FakeTeaching:
    def teach(self, engine, agent, desired=None, sample_ts=None):
        return {"ok": True}

    def undo(self, engine, agent):
        return {"ok": True}


class FakeRLTeaching:
    def __init__(self, store):
        self.store = store

    def add_label(self, agent, desired, sample_ts):
        return {"ok": True}

    def undo(self, agent):
        return {"ok": True}

    def labels(self, agent_id):
        with self.store.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY sample_ts,id",
                (str(agent_id),),
            ).fetchall()]

    def _label_context(self, agent, policy, sample_ts):
        # Teach fixtures use a dedicated feature key that does not appear in unrelated
        # historical regression samples.
        return {0: 1.0, 1: 1.0}


class ToyPolicy:
    """Small deterministic policy whose positive update corrects only one context key."""
    def __init__(self, store, agent):
        self.store = store
        self.agent = agent
        raw = store.get_model(agent["id"]) or {}
        self.actions = [0.0, 1.0]
        self.horizons = [1.0]
        self.model_revision = raw.get("model_revision") or "parent-rev"
        self.mapping = {str(k): int(v) for k, v in (raw.get("toy_mapping") or {"1": 0, "2": 0, "3": 1}).items()}
        self.schema = SimpleNamespace(entities=list((raw.get("schema") or {}).get("entities") or []))

    @staticmethod
    def _key(features):
        if float(features.get(1, 0.0)) == 1.0:
            return "1"
        if float(features.get(2, 0.0)) == 1.0:
            return "2"
        return "3"

    def predict(self, features):
        value = float(self.mapping.get(self._key(features), 0))
        return {"value": value}, 0.9, [], 1.0, 1.0, 0.0

    def update(self, horizon, action_idx, features, reward):
        # Negative evidence is intentionally non-destructive in this fixture; the
        # positive desired update makes the local correction visible and deterministic.
        if float(reward) > 0:
            self.mapping[self._key(features)] = int(action_idx)

    def serialize(self):
        raw = self.store.get_model(self.agent["id"]) or {}
        raw.update({
            "version": 10,
            "model_revision": self.model_revision,
            "toy_mapping": dict(self.mapping),
            "schema": raw.get("schema") or {"version": 11, "entities": list(self.schema.entities)},
        })
        return raw


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


class FakeQueue:
    def __init__(self):
        self.enqueue = Mock(side_effect=AssertionError("Correct must not enter rebuild queue"))

    def status_for(self, agent_id):
        return None

    def cancel(self, agent_id):
        return False


class ConservativeCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "candidate-correct.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        with self.store.lock, self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    sample_ts REAL NOT NULL,
                    desired REAL NOT NULL,
                    previous_desired REAL,
                    fingerprint TEXT NOT NULL,
                    undone_ts REAL
                );
                CREATE TABLE IF NOT EXISTS teaching_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    sample_ts REAL NOT NULL,
                    desired REAL NOT NULL,
                    previous_desired REAL,
                    fingerprint TEXT NOT NULL,
                    signature_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'test',
                    undone_ts REAL
                );
                """
            )
        self.parent = self.store.create_agent({
            "name": "Bathroom light",
            "target_entity": "light.bathroom",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": .25,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.bathroom_presence", "sensor.keep"],
        })
        self.parent_model = {
            "version": 10,
            "model_revision": "live-gen-n-revision",
            "schema": {"version": 11, "entities": ["binary_sensor.bathroom_presence", "sensor.keep"]},
            "selection_meta": {"primary_local_sensor": "binary_sensor.bathroom_presence"},
            "toy_mapping": {"1": 0, "2": 0, "3": 1},
            "knowledge_marker": {"learned": [1, 2, 3]},
        }
        self.store.save_model(self.parent["id"], self.parent_model)
        self.store.set_training_state(self.parent["id"], "qualified", score=.95, samples=120, source="test", detail={})
        self.parent = self.store.get_agent(self.parent["id"])

        self.rl = FakeRLTeaching(self.store)
        self.queue = FakeQueue()
        self.executor = FakeExecutor()
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=self.rl, models={}, runtime={}, executor=self.executor,
            temporal_history=None, process_agent=lambda *args, **kwargs: None,
            own_command_echo=lambda *args, **kwargs: False,
            wake_event=SimpleNamespace(set=lambda: None),
        )
        self.engine.policy = lambda agent: ToyPolicy(self.store, agent)
        self.core = SimpleNamespace(
            STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
            TRAINING_QUEUE=self.queue, HISTORY=None,
        )
        self.manager = install_conservative_correct(AgentCandidateManager(self.core, start_worker=False))

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _add_teach(self, desired=1.0, previous_desired=0.0, sample_ts=1000.0):
        agent = self.store.get_agent_config(self.parent["id"])
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO teaching_rl_labels
                   (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts)
                   VALUES(?,?,?,?,?,?,NULL)""",
                (agent["id"], sample_ts + 10, sample_ts, desired, previous_desired, fingerprint(agent)),
            )

    def _add_history_fixture(self, teach_ts=1000.0):
        # 12 unrelated held-out points, balanced across OFF/ON, plus one row exactly at
        # the Teach timestamp that must be excluded from regression scoring.
        fixtures = []
        for i in range(12):
            actual = i % 2
            feature_key = 2 if actual == 0 else 3
            fixtures.append((2000.0 + i, actual, {0: 1.0, feature_key: 1.0}))
        fixtures.append((teach_ts, 1, {0: 1.0, 1: 1.0}))
        for ts, actual, features in fixtures:
            self.store.archive_upsert("light.bathroom", ts, "on" if actual else "off", {}, None, "test")
            with self.store.conn() as c:
                history_id = c.execute(
                    "SELECT id FROM entity_history WHERE entity_id='light.bathroom' AND ts=?", (ts,)
                ).fetchone()[0]
            with self.store.lock, self.store.conn() as c:
                c.execute(
                    """INSERT INTO historical_experiences
                       (agent_id,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json,user_id)
                       VALUES(?,?,?,?,?,?,?,?,NULL)""",
                    (self.parent["id"], history_id, "2026-01-01T00:00:00+00:00", actual, float(actual), 1.0, 1.0,
                     json.dumps({str(k): v for k, v in features.items()})),
                )

    def test_candidate_clone_is_exact_parent_snapshot_before_fine_tune(self):
        self._add_history_fixture()
        status = self.manager.enqueue(self.parent["id"], "teach")
        candidate = self.store.get_agent(status["candidate_id"])
        self.assertEqual(self.store.get_model(candidate["id"]), self.store.get_model(self.parent["id"]))
        self.assertEqual(candidate["input_entities"], self.parent["input_entities"])
        self.assertEqual(candidate["deadband"], self.parent["deadband"])
        self.assertEqual(candidate["training_state"], self.parent["training_state"])
        self.assertEqual(candidate["mode"], "paused")
        with self.store.conn() as c:
            parent_n = c.execute("SELECT COUNT(*) FROM historical_experiences WHERE agent_id=?", (self.parent["id"],)).fetchone()[0]
            candidate_n = c.execute("SELECT COUNT(*) FROM historical_experiences WHERE agent_id=?", (candidate["id"],)).fetchone()[0]
        self.assertEqual(candidate_n, parent_n)

    def test_correct_does_not_clear_learning_or_change_schema_and_improves_teach_fit(self):
        self._add_teach(desired=1.0, previous_desired=0.0)
        self._add_history_fixture()
        self.store.clear_learning = Mock(wraps=self.store.clear_learning)
        status = self.manager.enqueue(self.parent["id"], "teach")
        row = self.manager._candidate_row(self.parent["id"])
        before_schema = list(self.store.get_model(status["candidate_id"])["schema"]["entities"])

        self.assertTrue(self.manager._start_build(row))
        result = self.manager.status(self.parent["id"])

        self.store.clear_learning.assert_not_called()
        self.queue.enqueue.assert_not_called()
        self.assertEqual(self.store.get_model(status["candidate_id"])["schema"]["entities"], before_schema)
        self.assertEqual(result["state"], "comparing")
        self.assertEqual((result["teach_fit_before_count"], result["teach_fit_total"]), (0, 1))
        self.assertEqual((result["teach_fit_after_count"], result["teach_fit_total"]), (1, 1))
        self.assertEqual(result["historical_benchmark_samples"], 12)
        self.assertAlmostEqual(result["historical_regression_delta"], 0.0, places=9)
        self.assertTrue(result["offline_gate"]["passed"])

    def test_negative_correction_targets_actual_candidate_prediction_only(self):
        self._add_teach(desired=1.0, previous_desired=1.0)
        self._add_history_fixture()
        status = self.manager.enqueue(self.parent["id"], "teach")
        self.manager._start_build(self.manager._candidate_row(self.parent["id"]))
        feedback = self.store.list_feedback(status["candidate_id"], 20)
        negatives = [x for x in feedback if x["source"] == "candidate_correct" and x["reward"] < 0]
        self.assertEqual(len(negatives), 1)
        self.assertEqual(negatives[0]["action_value"], 0.0)

    def test_previous_desired_does_not_punish_candidate_when_prediction_is_already_correct(self):
        model = self.store.get_model(self.parent["id"])
        model["toy_mapping"]["1"] = 1
        self.store.save_model(self.parent["id"], model)
        self._add_teach(desired=1.0, previous_desired=0.0)
        self._add_history_fixture()
        status = self.manager.enqueue(self.parent["id"], "teach")
        self.manager._start_build(self.manager._candidate_row(self.parent["id"]))
        feedback = self.store.list_feedback(status["candidate_id"], 20)
        negatives = [x for x in feedback if x["source"] == "candidate_correct" and x["reward"] < 0]
        self.assertEqual(negatives, [])
        gate = self.manager.status(self.parent["id"])["offline_gate"]
        self.assertEqual(gate["teach_fit_before_count"], 1)
        self.assertEqual(gate["teach_fit_after_count"], 1)

    def test_large_unrelated_historical_regression_blocks_candidate(self):
        parent_stats = {
            "samples": 100, "score": .90, "balanced": True,
            "actual_class_coverage": 2, "predicted_class_coverage": 2,
            "per_action_accuracy": {"0": .90, "1": .90},
        }
        candidate_stats = {
            "samples": 100, "score": .70, "balanced": True,
            "actual_class_coverage": 2, "predicted_class_coverage": 2,
            "per_action_accuracy": {"0": .70, "1": .70},
        }
        gate = _offline_gate(
            self.parent, parent_stats, candidate_stats,
            {"teach_fit_before": .5, "teach_fit_after": 1.0},
        )
        self.assertEqual(gate["status"], "failed")
        self.assertFalse(gate["passed"])
        self.assertAlmostEqual(gate["regression_delta"], -.20)

    def test_insufficient_history_is_not_invented_into_a_score(self):
        sparse = {
            "samples": 3, "score": None, "balanced": True,
            "actual_class_coverage": 1, "predicted_class_coverage": 1,
            "per_action_accuracy": {"0": 1.0},
        }
        gate = _offline_gate(self.parent, sparse, sparse, {"teach_fit_before": 0.0, "teach_fit_after": 1.0})
        self.assertEqual(gate["status"], "insufficient_evidence")
        self.assertFalse(gate["passed"])
        self.assertIsNone(gate["regression_delta"])

    def test_binary_one_class_collapse_is_blocked(self):
        parent_stats = {
            "samples": 100, "score": .90, "balanced": True,
            "actual_class_coverage": 2, "predicted_class_coverage": 2,
            "per_action_accuracy": {"0": .90, "1": .90},
        }
        collapsed = {
            "samples": 100, "score": .88, "balanced": True,
            "actual_class_coverage": 2, "predicted_class_coverage": 1,
            "per_action_accuracy": {"0": 1.0, "1": 0.76},
        }
        gate = _offline_gate(self.parent, parent_stats, collapsed, {"teach_fit_before": .5, "teach_fit_after": 1.0})
        self.assertFalse(gate["passed"])
        self.assertTrue(gate["binary_collapse"])

    def test_candidate_path_never_dispatches_home_assistant_service(self):
        self._add_teach()
        self._add_history_fixture()
        self.manager.enqueue(self.parent["id"], "teach")
        self.manager._start_build(self.manager._candidate_row(self.parent["id"]))
        self.executor.service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
