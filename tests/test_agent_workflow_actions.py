import json
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
from agent_workflow_actions import install as install_workflow
from manual_context_learning import install as install_manual_context_learning
from teaching_rl import fingerprint as rl_fingerprint


class FakeTeaching:
    def __init__(self, store):
        self.store = store
        with store.lock, store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS teaching_labels (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                  created_ts REAL NOT NULL, sample_ts REAL NOT NULL, desired REAL NOT NULL,
                  previous_desired REAL, fingerprint TEXT NOT NULL, signature_json TEXT NOT NULL,
                  source TEXT NOT NULL, undone_ts REAL);
                CREATE TABLE IF NOT EXISTS decision_history (
                  agent_id TEXT NOT NULL, ts REAL NOT NULL, current REAL, desired REAL,
                  PRIMARY KEY(agent_id,ts));
                """
            )

    def teach(self, engine, agent, desired=None, sample_ts=None):
        previous = (engine.runtime.get(agent["id"]) or {}).get("last_prediction")
        if desired is None:
            if agent["target_property"] != "power" or previous is None:
                raise ValueError("desired required")
            desired = 0.0 if float(previous) >= .5 else 1.0
        desired = float(desired)
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            row = c.execute(
                """INSERT INTO teaching_labels
                   (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,signature_json,source,undone_ts)
                   VALUES(?,?,?,?,?,'test','{}','wrong_decision',NULL)""",
                (agent["id"], now, now, desired, previous),
            )
            label_id = int(row.lastrowid)
        engine.runtime.setdefault(agent["id"], {})["last_prediction"] = desired
        return {"ok": True, "label_id": label_id, "desired_value": desired, "sample_ts": now}

    def undo(self, engine, agent):
        return {"ok": True}

    def match(self, agent, policy, states, temporal, timestamp):
        with self.store.conn() as c:
            row = c.execute(
                """SELECT * FROM teaching_labels
                   WHERE agent_id=? AND undone_ts IS NULL ORDER BY id DESC LIMIT 1""",
                (agent["id"],),
            ).fetchone()
        return dict(row) if row else None


class FakeRLTeaching:
    MAX_LABELS = 256

    def __init__(self, store):
        self.store = store
        with store.lock, store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL, created_ts REAL NOT NULL,
                    sample_ts REAL NOT NULL, desired REAL NOT NULL,
                    previous_desired REAL, fingerprint TEXT NOT NULL, undone_ts REAL);
                CREATE TABLE IF NOT EXISTS teaching_rl_jobs (
                    agent_id TEXT PRIMARY KEY, requested_ts REAL NOT NULL,
                    state TEXT NOT NULL, original_inputs_json TEXT NOT NULL,
                    pre_schema_json TEXT NOT NULL, selected_inputs_json TEXT NOT NULL,
                    report_json TEXT NOT NULL DEFAULT '{}');
                """
            )

    def add_label(self, agent, desired, sample_ts):
        return {"ok": True, "desired_value": float(desired), "sample_ts": float(sample_ts)}

    def undo(self, agent):
        return {"ok": True}

    def labels(self, agent_id):
        with self.store.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY id",
                (str(agent_id),),
            ).fetchall()]

    def status(self, agent_id):
        return {"state": "idle", "report": {}}


class FakeExecutor:
    def __init__(self):
        self.service = Mock(side_effect=AssertionError("generation workflow must not dispatch HA service"))
        self._service = Mock(side_effect=AssertionError("generation workflow must not dispatch HA service"))
        self.release_control = Mock(side_effect=AssertionError("generation action must not release control"))

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
        self.calls.append({"agent_id": agent_id, "rebuild": bool(rebuild), "reason": reason})
        return {"state": "queued", "position": 1, "agent_id": agent_id,
                "rebuild": bool(rebuild), "reason": reason}


class DummyPolicy:
    def __init__(self, store, agent):
        self.agent = agent
        self.model = store.get_model(agent["id"]) or {}
        self.actions = [0.0, 1.0]
        self.horizons = [1.0]
        self.model_revision = self.model.get("model_revision")
        self.selection_meta = dict(self.model.get("selection_meta") or {})
        self.schema = SimpleNamespace(version=(self.model.get("schema") or {}).get("version"), entities=["binary_sensor.presence"])
    def features(self, state_map, history, at_ts=None):
        return {0: 1.0}, {}, {}
    def predict(self, features):
        desired = float(self.model.get("prediction", 0.0))
        return {"value": desired}, .9, [], 1.0, 1.0, 0.0
    def serialize(self):
        return dict(self.model)


class AgentWorkflowActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "workflow.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Workflow light", "target_entity": "light.workflow", "target_property": "power",
            "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
            "exploration_step": 1, "input_entities": ["binary_sensor.presence"],
        })
        self.root_model = {
            "version": 10, "model_revision": "root-r1",
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 7},
            "prediction": 0.0, "weights": {"root": 1},
        }
        self.store.save_model(self.root["id"], self.root_model)
        self.store.set_training_state(self.root["id"], "qualified", score=.92, samples=100,
                                      source="test", detail={"balanced": True})
        self.root = self.store.get_agent(self.root["id"])
        self.executor = FakeExecutor()
        self.teaching = FakeTeaching(self.store)
        self.rl = FakeRLTeaching(self.store)
        self.queue = FakeQueue()
        self.engine = SimpleNamespace(
            teaching=self.teaching, rl_teaching=self.rl, models={}, runtime={}, executor=self.executor,
            temporal_history=None, entity_registry={}, state_map={
                "light.workflow": {"entity_id": "light.workflow", "state": "off", "attributes": {}, "context": {}},
                "binary_sensor.presence": {"entity_id": "binary_sensor.presence", "state": "on", "attributes": {}},
            },
            context_relevance={}, process_agent=lambda *args, **kwargs: None,
            own_command_echo=lambda *args, **kwargs: False,
            wake_event=SimpleNamespace(set=lambda: None),
            lock=SimpleNamespace(__enter__=lambda s: s, __exit__=lambda s,*a: None),
        )
        # A real context manager lock is required by workflow/current snapshot helpers.
        import threading
        self.engine.lock = threading.RLock()
        self.engine.policy = lambda agent: DummyPolicy(self.store, agent)
        self.core = SimpleNamespace(
            STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
            TRAINING_QUEUE=self.queue, HISTORY=None,
        )
        install_manual_context_learning(self.core)
        manager = AgentCandidateManager(self.core, start_worker=False)
        manager = install_conservative_correct(manager)
        manager = install_lineage(manager)
        self.manager = install_workflow(manager)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def model(self, agent_id):
        return self.store.get_model(agent_id)

    def parent_snapshot(self, generation):
        return json.dumps(self.model(generation["agent_id"]), sort_keys=True)

    def make_g1(self):
        result = self.manager.workflow_autonomous(self.root["id"])
        g1 = self.manager.lineage_status(result["child_generation_id"])
        self.assertIsNotNone(g1)
        return g1

    def add_correct_label(self, agent):
        with self.store.lock, self.store.conn() as c:
            row = c.execute(
                """INSERT INTO teaching_rl_labels
                   (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts)
                   VALUES(?,?,?,?,?,?,NULL)""",
                (agent["id"], time.time(), time.time()-5, 1.0, 0.0, rl_fingerprint(agent)),
            )
            return int(row.lastrowid)

    def test_live_autonomous_creates_child_without_clear_learning_and_preserves_parent(self):
        before = self.model(self.root["id"])
        original_clear = self.store.clear_learning
        self.store.clear_learning = Mock(wraps=original_clear)
        result = self.manager.workflow_autonomous(self.root["id"])
        child = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(child["parent_generation_id"], f"root:{self.root['id']}")
        self.assertEqual(self.model(self.root["id"]), before)
        row = self.manager._candidate_row(self.root["id"])
        self.assertTrue(self.manager._start_build(row))
        self.assertEqual(self.queue.calls[-1]["reason"], "autonomous_continuation")
        self.assertFalse(self.queue.calls[-1]["rebuild"])
        self.store.clear_learning.assert_not_called()
        self.assertEqual(self.model(self.root["id"]), before)

    def test_live_correct_creates_child_and_preserves_parent(self):
        before = self.model(self.root["id"])
        self.add_correct_label(self.root)
        result = self.manager.workflow_correct_commit(self.root["id"])
        child = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(child["parent_generation_id"], f"root:{self.root['id']}")
        self.assertEqual(self.model(self.root["id"]), before)

    def test_durable_correct_request_groups_only_labels_from_current_operation(self):
        first_label = self.add_correct_label(self.root)
        second_label = self.add_correct_label(self.root)
        first = self.manager.workflow_correct_commit(
            self.root["id"], request_id="correct-op-a"
        )
        self.assertEqual(first["correct_operation_id"], "correct-op-a")
        self.assertEqual(first["correct_label_ids"], [first_label, second_label])

        with self.store.conn() as c:
            row = dict(c.execute(
                "SELECT * FROM agent_correct_operations WHERE operation_id=?",
                ("correct-op-a",),
            ).fetchone())
        self.assertEqual(row["status"], "committed")
        self.assertEqual(json.loads(row["label_ids_json"]), [first_label, second_label])
        self.assertEqual(row["child_generation_id"], first["child_generation_id"])

        # A later Correct on the same direct parent coalesces into the existing child,
        # but its operation batch contains only the newly added/edited labels.
        time.sleep(.002)
        third_label = self.add_correct_label(self.root)
        second = self.manager.workflow_correct_commit(
            self.root["id"], request_id="correct-op-b"
        )
        self.assertTrue(second["coalesced"])
        self.assertEqual(second["child_generation_id"], first["child_generation_id"])
        self.assertEqual(second["correct_label_ids"], [third_label])
        with self.store.conn() as c:
            row = dict(c.execute(
                "SELECT * FROM agent_correct_operations WHERE operation_id=?",
                ("correct-op-b",),
            ).fetchone())
        self.assertEqual(json.loads(row["label_ids_json"]), [third_label])

    def test_committed_correct_operation_replays_idempotently_after_crash(self):
        label_id = self.add_correct_label(self.root)
        first = self.manager.workflow_correct_commit(
            self.root["id"], request_id="correct-crash-safe"
        )
        second = self.manager.workflow_correct_commit(
            self.root["id"], request_id="correct-crash-safe"
        )
        self.assertEqual(second["child_generation_id"], first["child_generation_id"])
        self.assertEqual(second["correct_label_ids"], [label_id])
        self.assertTrue(second["idempotent_replay"])
        with self.store.conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM agent_correct_operations WHERE operation_id=?",
                ("correct-crash-safe",),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_live_change_decision_records_context_label_and_creates_child(self):
        before = self.model(self.root["id"])
        self.engine.runtime[self.root["id"]] = {"last_prediction": 0.0, "last_confidence": .8}
        result = self.manager.workflow_change_decision(self.root["id"], 1.0)
        child = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(child["parent_generation_id"], f"root:{self.root['id']}")
        self.assertTrue(result["context_feedback"]["recorded"])
        with self.store.conn() as c:
            labels = c.execute("SELECT COUNT(*) FROM teaching_labels WHERE agent_id=?", (self.root["id"],)).fetchone()[0]
            contexts = c.execute("SELECT COUNT(*) FROM manual_context_feedback WHERE agent_id=?", (self.root["id"],)).fetchone()[0]
        self.assertEqual(labels, 1)
        self.assertEqual(contexts, 1)
        self.assertEqual(self.model(self.root["id"]), before)
        self.executor.service.assert_not_called()
        self.executor._service.assert_not_called()

    def test_candidate_autonomous_creates_grandchild_and_preserves_g1(self):
        g1 = self.make_g1()
        g1_before = self.model(g1["agent_id"])
        # Discard the queued edge only from workflow perspective by using G1 as the next parent;
        # lineage spawn_child freezes G1 and creates the direct grandchild.
        result = self.manager.workflow_autonomous(g1["generation_id"])
        g2 = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(g2["parent_generation_id"], g1["generation_id"])
        self.assertEqual(self.model(g1["agent_id"]), g1_before)

    def test_candidate_correct_creates_grandchild_and_preserves_g1(self):
        g1 = self.make_g1()
        g1_agent = self.store.get_agent_config(g1["agent_id"])
        before = self.model(g1["agent_id"])
        self.add_correct_label(g1_agent)
        result = self.manager.workflow_correct_commit(g1["generation_id"])
        g2 = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(g2["parent_generation_id"], g1["generation_id"])
        self.assertEqual(self.model(g1["agent_id"]), before)

    def test_candidate_change_decision_creates_grandchild_without_dispatch_and_preserves_g1(self):
        g1 = self.make_g1()
        before = self.model(g1["agent_id"])
        self.manager.generation_decision_at = lambda ref, ts: {
            "generation_id": g1["generation_id"], "ts": ts, "current": 0.0,
            "desired": 0.0, "confidence": .87,
        } if str(ref) == str(g1["generation_id"]) else None
        result = self.manager.workflow_change_decision(g1["generation_id"], 1.0)
        g2 = self.manager.lineage_status(result["child_generation_id"])
        self.assertEqual(g2["parent_generation_id"], g1["generation_id"])
        self.assertEqual(self.model(g1["agent_id"]), before)
        self.assertIsNone(result["physical_service"])
        self.executor.service.assert_not_called()
        self.executor._service.assert_not_called()
        self.executor.release_control.assert_not_called()

    def test_same_parent_change_decisions_coalesce_into_one_child(self):
        self.engine.runtime[self.root["id"]] = {"last_prediction": 0.0, "last_confidence": .8}
        first = self.manager.workflow_change_decision(self.root["id"], 1.0)
        self.engine.runtime[self.root["id"]]["last_prediction"] = 1.0
        second = self.manager.workflow_change_decision(self.root["id"], 0.0)
        self.assertEqual(first["child_generation_id"], second["child_generation_id"])
        self.assertFalse(first["coalesced"])
        self.assertTrue(second["coalesced"])
        children = [x for x in self.manager.list_lineage(self.root["id"]) if x.get("parent_generation_id") == f"root:{self.root['id']}"]
        self.assertEqual(len(children), 1)
        row = self.manager._candidate_row(self.root["id"])
        self.assertGreaterEqual(int(row["feedback_revision"]), 2)

    def test_feedback_never_crosses_generation_parent_boundary(self):
        g1 = self.make_g1()
        g2 = self.manager.workflow_autonomous(g1["generation_id"])
        with self.assertRaisesRegex(ValueError, "no longer the active correction edge"):
            self.manager.workflow_change_decision(self.root["id"], 1.0)
        self.assertEqual(self.manager.lineage_status(g2["child_generation_id"])["parent_generation_id"], g1["generation_id"])


if __name__ == "__main__":
    unittest.main()
