import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import support
import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_manual_rebuild import install as install_candidate_manual_rebuild
from agent_candidate_teach_status import install as install_candidate_teach_status


class FakeTeaching:
    def teach(self, engine, agent, desired=None, sample_ts=None):
        return {"ok": True}

    def undo(self, engine, agent):
        return {"ok": True}


class FakeExecutor:
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


class RecordingQueue:
    def __init__(self):
        self.calls = []

    def status_for(self, agent_id):
        return None

    def enqueue(self, agent_id, rebuild=False, reason="training"):
        self.calls.append((str(agent_id), bool(rebuild), str(reason)))
        return {
            "state": "queued", "position": 1, "ahead": 0,
            "rebuild": bool(rebuild), "reason": str(reason), "agent_id": str(agent_id),
        }

    def cancel(self, agent_id):
        return False


class RejectTeachPreflight:
    def prepare_retrain(self, agent):
        raise AssertionError("manual Rebuild must not enter Teach RL preflight")


class CandidateBuildRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "candidate-build.db")
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
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=None, models={}, runtime={},
            executor=FakeExecutor(), process_agent=lambda *args, **kwargs: None,
            wake_event=SimpleNamespace(set=lambda: None),
        )
        self.core = SimpleNamespace(
            STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
            TRAINING_QUEUE=None, HISTORY=None,
        )
        self.manager = AgentCandidateManager(self.core, start_worker=False)
        self.manager = install_candidate_manual_rebuild(self.manager)
        install_candidate_teach_status(self.core, self.manager)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def test_cold_start_feedback_trains_parent_in_place_before_candidate(self):
        cold = self.store.create_agent({
            "name": "Cold start light",
            "target_entity": "light.cold_start",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": .25,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.cold_start_presence"],
        })
        queue = RecordingQueue()
        self.core.TRAINING_QUEUE = queue

        result = self.manager.enqueue(cold["id"], "teach")

        self.assertEqual(result["state"], "initial_training")
        self.assertIsNone(result["candidate_id"])
        self.assertIsNone(self.manager.status(cold["id"]))
        self.assertEqual(queue.calls, [(cold["id"], True, "initial_training")])

    def test_first_queued_revision_is_pending_not_stale(self):
        status = self.manager.enqueue(self.parent["id"], "teach")
        self.assertEqual(status["feedback_revision"], 1)
        self.assertEqual(status["build_revision"], 0)
        self.assertFalse(status["stale"])
        self.assertTrue(status["build_pending"])

        requested = self.manager.request_build(self.parent["id"], "teach_train")
        self.assertEqual(requested["feedback_revision"], 1)
        self.assertEqual(requested["build_revision"], 0)
        self.assertFalse(requested["stale"])
        with self.store.conn() as c:
            row = c.execute(
                "SELECT reason,feedback_revision,build_revision FROM agent_candidates WHERE parent_agent_id=?",
                (self.parent["id"],),
            ).fetchone()
        self.assertEqual(row["reason"], "teach_train")
        self.assertEqual(row["feedback_revision"], 1)
        self.assertEqual(row["build_revision"], 0)

    def test_build_button_during_training_does_not_create_fake_feedback(self):
        self.manager.enqueue(self.parent["id"], "teach")
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE agent_candidates SET state='building',build_revision=feedback_revision,dirty=0
                   WHERE parent_agent_id=?""",
                (self.parent["id"],),
            )
        before = self.manager.status(self.parent["id"])
        self.assertEqual(before["feedback_revision"], 1)
        self.assertEqual(before["build_revision"], 1)
        self.assertFalse(before["stale"])

        after = self.manager.request_build(self.parent["id"], "teach_train")
        self.assertEqual(after["state"], "building")
        self.assertEqual(after["feedback_revision"], 1)
        self.assertEqual(after["build_revision"], 1)
        self.assertFalse(after["dirty"])
        self.assertFalse(after["stale"])

        # A real new Teach point during the build still marks the build stale.
        newer = self.manager.enqueue(self.parent["id"], "teach")
        self.assertEqual(newer["feedback_revision"], 2)
        self.assertEqual(newer["build_revision"], 1)
        self.assertTrue(newer["dirty"])
        self.assertTrue(newer["stale"])

    def test_manual_rebuild_uses_full_history_queue_without_teach_preflight(self):
        queue = RecordingQueue()
        self.core.TRAINING_QUEUE = queue
        self.engine.rl_teaching = RejectTeachPreflight()

        requested = self.manager.request_build(self.parent["id"], "manual_rebuild")
        self.assertEqual(requested["feedback_revision"], 0)
        self.assertEqual(requested["build_revision"], 0)
        row = self.manager._candidate_row(self.parent["id"])
        self.assertEqual(row["reason"], "manual_rebuild")

        self.assertTrue(self.manager._start_build(row))
        status = self.manager.status(self.parent["id"])
        self.assertEqual(status["state"], "building")
        self.assertFalse(status["dirty"])
        self.assertEqual(status["feedback_revision"], 0)
        self.assertEqual(status["build_revision"], 0)
        self.assertEqual(
            queue.calls,
            [(str(status["candidate_id"]), True, "full_rebuild")],
        )


class CandidateUiStabilityTests(unittest.TestCase):
    def test_candidate_cards_are_guarded_from_p0_reconciliation(self):
        source = (support.ROOT / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("__candidateCardRenderGuard", source)
        self.assertIn("candidates.forEach(node=>node.remove())", source)
        self.assertIn("finally{candidates.forEach(node=>root.appendChild(node));}", source)

    def test_candidate_copy_uses_correct_workflow_names(self):
        source = (support.ROOT / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        self.assertNotIn("New feedback arrived — another build is required.", source)
        self.assertIn("New Correct / Change decision feedback arrived after this build snapshot", source)
        self.assertIn(">Autonomous<", source)
        self.assertIn(">Correct<", source)
        self.assertIn(">Change decision<", source)
        self.assertNotIn(">Teach<", source)
        self.assertNotIn(">Wrong decision<", source)


if __name__ == "__main__":
    unittest.main()
