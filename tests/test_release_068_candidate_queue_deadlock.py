import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_manual_rebuild import install as install_candidate_manual_rebuild
from teaching_rl import RLTeaching


class FakeExecutor:
    @contextmanager
    def target_lock(self, entity_id):
        yield


class FakeTeaching:
    def teach(self, *args, **kwargs):
        return {"ok": True}

    def undo(self, *args, **kwargs):
        return {"ok": True}


class FakeHandler:
    def do_GET(self):
        return None

    def do_POST(self):
        return None

    def do_DELETE(self):
        return None


class ExistingPendingQueue:
    def __init__(self, *, reason="training", rebuild=False, active=False):
        self.current = {
            "state": "active" if active else "queued",
            "position": 0 if active else 1,
            "ahead": 0,
            "rebuild": bool(rebuild),
            "reason": str(reason),
        }
        self.enqueue_calls = []
        self.cancel_calls = []

    def status_for(self, agent_id):
        if self.current is None:
            return None
        return {**self.current, "agent_id": str(agent_id)}

    def enqueue(self, agent_id, rebuild=False, reason="training"):
        self.enqueue_calls.append((str(agent_id), bool(rebuild), str(reason)))
        # Match TrainingQueue's special pending Teach-RL upgrade semantics.
        if self.current and self.current.get("state") == "queued":
            if str(reason) == "teach_rl":
                self.current["reason"] = "teach_rl"
                self.current["rebuild"] = True
            elif bool(rebuild) and not self.current.get("rebuild"):
                self.current["reason"] = "full_rebuild"
                self.current["rebuild"] = True
            else:
                self.current["reason"] = str(reason)
                self.current["rebuild"] = bool(rebuild)
        else:
            self.current = {
                "state": "queued", "position": 1, "ahead": 0,
                "rebuild": bool(rebuild), "reason": str(reason),
            }
        return {**self.current, "agent_id": str(agent_id)}

    def cancel(self, agent_id):
        self.cancel_calls.append(str(agent_id))
        if self.current and self.current.get("state") == "queued":
            self.current = None
            return True
        return False


class CandidateQueuedTrainingDeadlockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "candidate-queue-deadlock.db")
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
            "input_entities": ["sensor.espen4_stationary_energy"],
        })
        self.store.save_model(
            self.parent["id"],
            {"version": 10, "schema": {"version": 11, "entities": []}, "marker": "live"},
        )
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(), rl_teaching=None, models={}, runtime={},
            executor=FakeExecutor(), process_agent=lambda *args, **kwargs: None,
            wake_event=SimpleNamespace(set=lambda: None),
            lock=threading.RLock(),
            state_map={
                "light.bathroom": {
                    "entity_id": "light.bathroom",
                    "state": "off",
                    "attributes": {},
                },
                "sensor.espen4_stationary_energy": {
                    "entity_id": "sensor.espen4_stationary_energy",
                    "state": "35",
                    "attributes": {
                        "unit_of_measurement": "%",
                        "friendly_name": "ESPEN4 Stationary Energy",
                    },
                },
            },
            entity_registry={
                "light.bathroom": {"area_id": "bathroom"},
                "sensor.espen4_stationary_energy": {"area_id": "bathroom"},
            },
        )
        self.engine.rl_teaching = RLTeaching(self.store, self.engine)
        self.core = SimpleNamespace(
            STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
            TRAINING_QUEUE=None, HISTORY=None,
        )
        self.manager = AgentCandidateManager(self.core, start_worker=False)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _queued_candidate(self, reason="teach"):
        self.manager.enqueue(self.parent["id"], reason)
        row = self.manager._candidate_row(self.parent["id"])
        self.assertEqual(row["state"], "queued")
        return row

    def test_pending_queue_job_is_adopted_and_candidate_advances_to_building(self):
        row = self._queued_candidate("teach")
        queue = ExistingPendingQueue(reason="training", rebuild=False, active=False)
        self.core.TRAINING_QUEUE = queue

        changed = self.manager._start_build(row)

        self.assertTrue(changed)
        status = self.manager.status(self.parent["id"])
        self.assertEqual(status["state"], "building")
        self.assertFalse(status["dirty"])
        self.assertEqual(
            queue.enqueue_calls,
            [(status["candidate_id"], True, "teach_rl")],
        )
        self.assertEqual(queue.current["reason"], "teach_rl")
        self.assertTrue(queue.current["rebuild"])

    def test_active_external_job_is_not_mutated_and_next_poll_can_claim(self):
        row = self._queued_candidate("teach")
        queue = ExistingPendingQueue(reason="training", rebuild=True, active=True)
        self.core.TRAINING_QUEUE = queue

        self.assertFalse(self.manager._start_build(row))
        self.assertEqual(self.manager.status(self.parent["id"])["state"], "queued")
        self.assertEqual(queue.enqueue_calls, [])

        # The external job completes. The very next manager poll must start the intended
        # Candidate Teach build instead of remaining queued forever.
        queue.current = None
        fresh = self.manager._candidate_row(self.parent["id"])
        self.assertTrue(self.manager._start_build(fresh))
        self.assertEqual(self.manager.status(self.parent["id"])["state"], "building")
        self.assertEqual(
            queue.enqueue_calls[-1][1:],
            (True, "teach_rl"),
        )

    def test_full_rebuild_replaces_incompatible_pending_job(self):
        row = self._queued_candidate("manual_feedback_undo_rebuild")
        queue = ExistingPendingQueue(reason="training", rebuild=True, active=False)
        self.core.TRAINING_QUEUE = queue
        self.manager = install_candidate_manual_rebuild(self.manager)

        changed = self.manager._start_build(row)

        self.assertTrue(changed)
        status = self.manager.status(self.parent["id"])
        self.assertEqual(status["state"], "building")
        self.assertEqual(queue.cancel_calls, [status["candidate_id"]])
        self.assertEqual(
            queue.enqueue_calls[-1],
            (status["candidate_id"], True, "full_rebuild"),
        )

    def test_shadow_contract_still_requires_comparing_or_ready_not_queued(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "adaptive_ai" / "src" / "agent_candidates.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'row.get("state") not in ("comparing", "ready")',
            source,
        )
        # The fix belongs to lifecycle advancement, not to running stale queued weights.
        self.assertIn("_claim_candidate_training_job", source)


if __name__ == "__main__":
    unittest.main()
