"""0.14.109 adaptive dual-agent replay worker contracts."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import history as history_module
import training_process
import training_queue as queue_module
from settings import DEFAULT_OPTIONS
from support import ROOT


class EffectiveParallelSlotsTests(unittest.TestCase):
    def manager(self):
        manager = history_module.HistoryManager.__new__(history_module.HistoryManager)
        manager.agent_jobs_lock = threading.RLock()
        return manager

    def test_four_core_host_with_headroom_uses_two_slots(self):
        options = {
            **history_module.OPTIONS,
            "max_concurrent_training_jobs": 2,
            "training_parallel_min_available_mb": 1800,
        }
        with (
            patch.object(history_module, "OPTIONS", options),
            patch.object(training_process, "_host_memory_mb", return_value={
                "total_mb": 3900.0, "available_mb": 2400.0
            }),
            patch("os.cpu_count", return_value=4),
        ):
            self.assertEqual(self.manager().effective_training_slots(), 2)

    def test_low_memory_or_low_cpu_falls_back_to_one_slot(self):
        options = {
            **history_module.OPTIONS,
            "max_concurrent_training_jobs": 2,
            "training_parallel_min_available_mb": 1800,
        }
        with (
            patch.object(history_module, "OPTIONS", options),
            patch.object(training_process, "_host_memory_mb", return_value={
                "total_mb": 3900.0, "available_mb": 1500.0
            }),
            patch("os.cpu_count", return_value=4),
        ):
            self.assertEqual(self.manager().effective_training_slots(), 1)
        with (
            patch.object(history_module, "OPTIONS", options),
            patch.object(training_process, "_host_memory_mb", return_value={
                "total_mb": 3900.0, "available_mb": 2600.0
            }),
            patch("os.cpu_count", return_value=2),
        ):
            self.assertEqual(self.manager().effective_training_slots(), 1)

    def test_release_defaults_target_two_slots_but_preserve_single_agent_order(self):
        self.assertEqual(DEFAULT_OPTIONS["max_concurrent_training_jobs"], 2)
        self.assertEqual(DEFAULT_OPTIONS["training_parallel_min_available_mb"], 1800)
        self.assertEqual(
            DEFAULT_OPTIONS["training_parallel_worker_memory_limit_mb"], 768
        )
        source = (ROOT / "adaptive_ai/src/history.py").read_text(encoding="utf-8")
        self.assertIn("session._run_agent_indexing(", source)
        self.assertIn('HEAVY_JOBS.acquire("agent_pool")', source)
        self.assertIn('HEAVY_JOBS.release("agent_pool")', source)
        # Parallelism is outside _run_agent_indexing; chunks of one agent remain one
        # persistent sequence and therefore keep their existing causal order.
        self.assertIn("run_isolated_training_sequence(self, chunks)", source)


class DualQueueAdmissionTests(unittest.TestCase):
    class FakeGate:
        def __init__(self):
            self.owner = None

    class FakeHistory:
        def __init__(self, gate):
            self.agent_jobs = set()
            self.agent_jobs_lock = threading.RLock()
            self.gate = gate
            self.cancelled = set()

        def effective_training_slots(self):
            return 2

        def request_agent_rebuild(self, agent_id, rebuild_reason=None):
            with self.agent_jobs_lock:
                if len(self.agent_jobs) >= 2:
                    return False
                if not self.agent_jobs:
                    self.gate.owner = "agent_pool"
                self.agent_jobs.add(str(agent_id))
            return True

        def request_agent_resume(self, agent_id):
            return self.request_agent_rebuild(agent_id)

        def cancel_agent_training(self, agent_id):
            self.cancelled.add(str(agent_id))
            return True

    class FakeStore:
        def __init__(self):
            self.agents = {
                "a": {
                    "id": "a", "name": "A", "mode": "shadow",
                    "training_state": "waiting", "training_progress": 0.0,
                    "benchmark_detail": {},
                },
                "b": {
                    "id": "b", "name": "B", "mode": "shadow",
                    "training_state": "waiting", "training_progress": 0.0,
                    "benchmark_detail": {},
                },
            }
            self.events = []

        def get_agent(self, agent_id):
            row = self.agents.get(str(agent_id))
            return None if row is None else dict(row)

        def get_agent_config(self, agent_id):
            return self.get_agent(agent_id)

        def set_training_state(self, agent_id, state, **kwargs):
            self.agents[str(agent_id)]["training_state"] = state

        def event(self, *args):
            self.events.append(args)

        def meta_set(self, *args, **kwargs):
            return None

    def test_two_regular_agents_are_admitted_concurrently(self):
        gate = self.FakeGate()
        history = self.FakeHistory(gate)
        store = self.FakeStore()
        engine = SimpleNamespace(
            executor=SimpleNamespace(release_control=lambda *args, **kwargs: None)
        )
        queue = queue_module.TrainingQueue(history, store, engine)
        with patch.object(queue_module, "HEAVY_JOBS", gate):
            queue.enqueue("a", rebuild=True, reason="training")
            queue.enqueue("b", rebuild=True, reason="training")
            self.assertTrue(queue._try_start_head())
            self.assertTrue(queue._try_start_head())
            snapshot = queue.snapshot()
            self.assertEqual(snapshot["active_count"], 2)
            self.assertEqual(snapshot["effective_slots"], 2)
            self.assertEqual(
                {row["agent_id"] for row in snapshot["active_jobs"]},
                {"a", "b"},
            )
            self.assertEqual(gate.owner, "agent_pool")

            queue.cancel_active("b")
            self.assertIn("b", history.cancelled)

            # Finish A while B remains active: the pool must remain represented by B.
            history.agent_jobs.discard("a")
            self.assertTrue(queue._finish_active_if_done())
            snapshot = queue.snapshot()
            self.assertEqual(snapshot["active_count"], 1)
            self.assertEqual(snapshot["active"]["agent_id"], "b")

    def test_teach_job_does_not_enter_second_parallel_slot(self):
        gate = self.FakeGate()
        history = self.FakeHistory(gate)
        store = self.FakeStore()
        teaching = SimpleNamespace(
            needs_context_selection=lambda _aid: False,
            mark_training=lambda _aid: None,
        )
        engine = SimpleNamespace(
            executor=SimpleNamespace(release_control=lambda *args, **kwargs: None),
            rl_teaching=teaching,
        )
        queue = queue_module.TrainingQueue(history, store, engine)
        with patch.object(queue_module, "HEAVY_JOBS", gate):
            queue.enqueue("a", rebuild=True, reason="training")
            queue.enqueue("b", rebuild=True, reason="teach_rl")
            self.assertTrue(queue._try_start_head())
            self.assertFalse(queue._try_start_head())
            self.assertEqual(queue.snapshot()["active_count"], 1)


class ParallelWorkerResourceContractTests(unittest.TestCase):
    def test_dual_slots_reduce_per_worker_memory_and_report_cpu(self):
        source = (ROOT / "adaptive_ai/src/training_process.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('training_parallel_worker_memory_limit_mb", 768', source)
        self.assertIn("parallel_memory_cap_applied", source)
        self.assertIn('"cpu_one_core_percent"', source)
        self.assertIn('"cpu_total_percent_estimate"', source)
        self.assertIn('"logical_cpu_count"', source)

    def test_recorder_backfill_is_serialized_but_replay_sessions_are_not(self):
        source = (ROOT / "adaptive_ai/src/history.py").read_text(encoding="utf-8")
        self.assertIn("self.training_recorder_lock = threading.RLock()", source)
        self.assertIn("with recorder_lock:", source)
        self.assertIn('"recorder_serialized": True', source)


if __name__ == "__main__":
    unittest.main()
