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
from agent_candidate_balance import install as install_balance
from agent_candidate_lifecycle_hardening import install as install_hardening
from agent_candidate_teach_status import _install_build_request
from teaching_rl import fingerprint as teach_fingerprint


class FakeTeaching:
    def teach(self, engine, agent, desired=None, sample_ts=None):
        return {"ok": True}

    def undo(self, engine, agent):
        return {"ok": True}


class FakeExecutor:
    def __init__(self):
        self.release_control = Mock(return_value=[])

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


class CandidateLifecycleHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "candidate-hardening.db")
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
            teaching=FakeTeaching(), rl_teaching=None, models={}, runtime={},
            executor=self.executor, process_agent=lambda *args, **kwargs: None,
            wake_event=SimpleNamespace(set=lambda: None),
        )
        self.core = SimpleNamespace(
            STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
            TRAINING_QUEUE=None, HISTORY=None,
        )
        self.manager = AgentCandidateManager(self.core, start_worker=False)
        self.manager = install_balance(self.manager)
        self.manager = _install_build_request(self.core, self.manager)
        self.manager = install_hardening(self.manager)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _ready_candidate(self):
        self.store.save_model(self.parent["id"], {
            "version": 10, "schema": {"version": 11, "entities": []}, "marker": "old",
        })
        status = self.manager.enqueue(self.parent["id"], "teach")
        candidate_id = status["candidate_id"]
        self.store.save_model(candidate_id, {
            "version": 10, "schema": {"version": 11, "entities": []}, "marker": "new",
        })
        self.store.set_training_state(
            candidate_id, "qualified", score=.9, samples=100,
            source="teach-rl-shadow", detail={"control_qualification": "stale"},
        )
        comparison = {
            "samples": 40,
            "live_correct": 36,
            "candidate_correct": 39,
            "on_events": 20,
            "off_events": 20,
            "live_false_early": 0,
            "candidate_false_early": 0,
        }
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE agent_candidates SET state='comparing',build_revision=feedback_revision,dirty=0,
                   comparison_json=? WHERE parent_agent_id=?""",
                (json.dumps(comparison), self.parent["id"]),
            )
        return candidate_id

    def test_promoted_generation_is_really_shadow_after_control_live(self):
        self._ready_candidate()
        self.store.update_agent(self.parent["id"], {"mode": "control"})
        result = self.manager.promote(self.parent["id"])
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(self.store.get_agent_config(self.parent["id"])["mode"], "shadow")
        self.executor.release_control.assert_called_once()
        self.assertEqual(self.store.get_model(self.parent["id"])["marker"], "new")

    def test_manual_rebuild_does_not_advance_feedback_revision(self):
        self.store.save_model(self.parent["id"], {
            "version": 10, "schema": {"version": 11, "entities": []}, "marker": "live",
        })
        first = self.manager.enqueue(self.parent["id"], "manual_rebuild")
        second = self.manager.enqueue(self.parent["id"], "manual_rebuild")
        self.assertEqual(first["feedback_revision"], 0)
        self.assertEqual(second["feedback_revision"], 0)
        self.assertEqual(second["reason"], "manual_rebuild")
        self.assertTrue(second["build_pending"])

    def test_latest_explicit_instruction_wins_for_same_teach_instant(self):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    sample_ts REAL NOT NULL,
                    desired REAL NOT NULL,
                    previous_desired REAL,
                    fingerprint TEXT NOT NULL,
                    undone_ts REAL
                )"""
            )
        status = self.manager.enqueue(self.parent["id"], "teach")
        candidate = self.store.get_agent(status["candidate_id"])
        fp = teach_fingerprint(self.parent)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts) VALUES(?,?,?,?,?,?,NULL)",
                (self.parent["id"], 10.0, 1000.0, 0.0, 1.0, fp),
            )
            c.execute(
                "INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts) VALUES(?,?,?,?,?,?,NULL)",
                (self.parent["id"], 11.0, 1000.0, 1.0, 0.0, fp),
            )
        report = self.manager._sync_feedback(self.parent, candidate)
        with self.store.conn() as c:
            rows = c.execute(
                "SELECT desired FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY id",
                (candidate["id"],),
            ).fetchall()
        self.assertEqual(report["examples"], 1)
        self.assertEqual(report["conflicts_retired"], 1)
        self.assertEqual([float(row["desired"]) for row in rows], [1.0])

    def test_discard_request_finishes_without_candidate_model(self):
        # A post-baseline Candidate may still lose its own checkpoint (crash/corruption).
        # Discard must delete it without trying to validate or rebuild the missing child model.
        status = self.manager.enqueue(self.parent["id"], "teach")
        candidate_id = status["candidate_id"]
        with self.store.lock, self.store.conn() as c:
            c.execute("DELETE FROM rl_models WHERE agent_id=?", (candidate_id,))
        self.assertIsNone(self.store.get_model(candidate_id))
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "UPDATE agent_candidates SET state='discarding',discard_requested=1,dirty=0 WHERE parent_agent_id=?",
                (self.parent["id"],),
            )
        row = self.manager._candidate_row(self.parent["id"])
        self.assertTrue(self.manager._finish_build_if_ready(row))
        self.assertIsNone(self.manager.status(self.parent["id"]))
        self.assertIsNone(self.store.get_agent(candidate_id))

    def test_upgrade_restores_interrupted_discard_intent(self):
        self.manager.enqueue(self.parent["id"], "teach")
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "UPDATE agent_candidates SET state='queued',discard_requested=1,dirty=1 WHERE parent_agent_id=?",
                (self.parent["id"],),
            )
        # Reinstall is idempotent, so exercise the migration clause directly by clearing
        # only its marker; method wrappers remain equivalent and no additional Candidate is created.
        self.manager._candidate_lifecycle_hardening = False
        install_hardening(self.manager)
        row = self.manager._candidate_row(self.parent["id"])
        self.assertEqual(row["state"], "discarding")
        self.assertEqual(int(row["dirty"]), 0)


if __name__ == "__main__":
    unittest.main()
