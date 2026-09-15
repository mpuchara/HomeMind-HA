import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_config_guard import install as install_config_guard


class FakeTeaching:
    def teach(self, engine, agent, desired=None, sample_ts=None):
        return {"ok": True}

    def undo(self, engine, agent):
        return {"ok": True}


class FakeExecutor:
    def __init__(self):
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


class CandidateConfigGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "candidate-config.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.parent = self.store.create_agent({
            "name": "Hall light",
            "target_entity": "light.hall",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": .25,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.hall_presence"],
        })
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=None, models={}, runtime={},
            executor=FakeExecutor(), temporal_history=None,
            process_agent=lambda *args, **kwargs: None,
            own_command_echo=lambda *args, **kwargs: False,
            wake_event=SimpleNamespace(set=lambda: None),
        )
        self.core = SimpleNamespace(STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
                                    TRAINING_QUEUE=None, HISTORY=None)
        self.manager = install_config_guard(AgentCandidateManager(self.core, start_worker=False))

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _ready_candidate(self):
        status = self.manager.enqueue(self.parent["id"], "teach")
        candidate_id = status["candidate_id"]
        self.store.save_model(self.parent["id"], {"version": 10, "schema": {"version": 11, "entities": []}, "marker": "live"})
        self.store.save_model(candidate_id, {"version": 10, "schema": {"version": 11, "entities": []}, "marker": "candidate"})
        self.store.set_training_state(candidate_id, "qualified", score=.95, samples=80, source="test", detail={})
        comparison = {
            "samples": 40,
            "live_correct": 36,
            "candidate_correct": 39,
            "candidate_wins": 3,
            "live_wins": 0,
            "both_wrong": 1,
            "per_action": {
                "0.0": {"samples": 20, "live_correct": 18, "candidate_correct": 19},
                "1.0": {"samples": 20, "live_correct": 18, "candidate_correct": 20},
            },
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

    def test_live_policy_config_change_invalidates_candidate_evidence(self):
        candidate_id = self._ready_candidate()
        before = self.manager.status(self.parent["id"])
        self.assertTrue(before["candidate_config_matches_live"])
        self.assertTrue(before["promotable"])

        self.store.update_agent(self.parent["id"], {"input_entities": ["binary_sensor.new_presence"]})

        row = self.manager._candidate_row(self.parent["id"])
        after = self.manager.status(self.parent["id"])
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["reason"], "config_change")
        self.assertEqual(int(row["dirty"]), 1)
        self.assertTrue(after["config_stale"])
        self.assertFalse(after["promotable"])
        self.assertEqual(self.store.get_agent_config(candidate_id)["input_entities"], ["binary_sensor.hall_presence"])

    def test_next_build_synchronizes_candidate_to_live_policy_config(self):
        candidate_id = self._ready_candidate()
        self.store.update_agent(self.parent["id"], {
            "input_entities": ["binary_sensor.new_presence", "sensor.lux"],
            "deadband": .25,
        })
        row = self.manager._candidate_row(self.parent["id"])

        # No TrainingQueue is installed in this unit test, but the config synchronization
        # happens immediately before queue admission and is therefore independently testable.
        self.assertFalse(self.manager._start_build(row))

        candidate = self.store.get_agent_config(candidate_id)
        self.assertEqual(candidate["input_entities"], ["binary_sensor.new_presence", "sensor.lux"])
        self.assertAlmostEqual(candidate["deadband"], .25)
        self.assertTrue(self.manager.status(self.parent["id"])["candidate_config_matches_live"])

    def test_promote_rejects_candidate_built_for_different_live_config(self):
        candidate_id = self._ready_candidate()
        # Candidate-internal edits do not dirty the parent; Promote itself must still
        # verify the structural contract immediately before the model swap.
        self.store.update_agent(candidate_id, {"deadband": .2})
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.manager.promote(self.parent["id"])
        self.assertEqual(self.store.get_model(self.parent["id"])["marker"], "live")
        self.assertIsNotNone(self.manager.status(self.parent["id"]))


if __name__ == "__main__":
    unittest.main()
