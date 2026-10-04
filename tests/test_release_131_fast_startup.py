"""0.14.131 warm-startup performance regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine as engine_module
from engine import Engine
from storage import Store


class StartupWarmupSchedulerTests(unittest.TestCase):
    def make_engine(self):
        runtime = Engine()
        self.addCleanup(
            lambda: runtime.control_workers.shutdown(wait=False, cancel_futures=True)
        )
        self.addCleanup(
            lambda: runtime.poll_worker.shutdown(wait=False, cancel_futures=True)
        )
        self.addCleanup(
            lambda: runtime.registry_worker.shutdown(wait=False, cancel_futures=True)
        )
        self.addCleanup(
            lambda: runtime.housekeeping_worker.shutdown(wait=False, cancel_futures=True)
        )
        return runtime

    def test_warmup_schedules_cold_targets_without_waiting_for_idle_event_loop(self):
        runtime = self.make_engine()
        runtime.active_agents_by_target = {
            "light.a": ["a"],
            "light.b": ["b"],
        }
        runtime.runtime = {}
        runtime.control_worker_count = 2

        with (
            patch.object(runtime, "_refresh_agent_index"),
            patch.object(runtime, "process") as process,
        ):
            scheduled = runtime._startup_warmup_step({"light.a": {}, "light.b": {}})

        self.assertEqual(scheduled, 2)
        process.assert_called_once()
        args, kwargs = process.call_args
        self.assertEqual(args[1], {"light.a", "light.b"})
        self.assertFalse(kwargs["event_driven"])
        self.assertFalse(runtime.initial_inference_pending)
        self.assertEqual(runtime.inference_scheduler["initial_full_passes"], 1)
        self.assertEqual(
            runtime.inference_scheduler["startup_warmup_scheduled_targets"], 2
        )
        self.assertEqual(
            runtime.inference_scheduler["startup_warmup_remaining_targets"], 0
        )

    def test_realtime_inflight_target_counts_as_warm_and_only_free_slot_is_filled(self):
        runtime = self.make_engine()
        runtime.active_agents_by_target = {
            "light.event": ["event-agent"],
            "light.cold": ["cold-agent"],
        }
        runtime.runtime = {}
        runtime.control_worker_count = 2

        class InFlight:
            def done(self):
                return False

        runtime.in_flight["light.event"] = InFlight()

        with (
            patch.object(runtime, "_refresh_agent_index"),
            patch.object(runtime, "process") as process,
        ):
            scheduled = runtime._startup_warmup_step({})

        self.assertEqual(scheduled, 1)
        args, kwargs = process.call_args
        self.assertEqual(args[1], {"light.cold"})
        self.assertFalse(kwargs["event_driven"])
        self.assertFalse(runtime.initial_inference_pending)


class LightweightStatusTests(unittest.TestCase):
    def make_engine(self):
        runtime = Engine()
        self.addCleanup(
            lambda: runtime.control_workers.shutdown(wait=False, cancel_futures=True)
        )
        self.addCleanup(
            lambda: runtime.poll_worker.shutdown(wait=False, cancel_futures=True)
        )
        self.addCleanup(
            lambda: runtime.registry_worker.shutdown(wait=False, cancel_futures=True)
        )
        self.addCleanup(
            lambda: runtime.housekeeping_worker.shutdown(wait=False, cancel_futures=True)
        )
        return runtime

    def test_status_never_calls_full_agent_history_aggregates(self):
        runtime = self.make_engine()

        class FakeStore:
            def list_agent_configs(self):
                return [
                    {
                        "id": "agent-a",
                        "enabled": True,
                        "mode": "shadow",
                        "training_state": "qualified",
                    }
                ]

            def list_agents(self):
                raise AssertionError("status must not run full agent aggregates")

        with patch.object(engine_module, "STORE", FakeStore()):
            status = runtime.status()

        self.assertEqual(status["agent_count"], 1)
        self.assertIsNone(status["historical_experience_count"])
        self.assertTrue(status["agent_metrics_deferred"])


class AgentAggregateQueryTests(unittest.TestCase):
    def test_list_agents_preserves_metrics_with_single_grouped_table_passes(self):
        with tempfile.TemporaryDirectory(prefix="hm-startup-metrics-") as root:
            store = Store(Path(root) / "adaptive_ai.db")
            created = store.create_agent(
                {
                    "name": "Test light",
                    "target_entity": "light.test",
                    "target_property": "power",
                    "min_value": 0.0,
                    "max_value": 1.0,
                    "deadband": 0.5,
                    "action_interval": 1.0,
                    "exploration_step": 1.0,
                }
            )
            aid = created["id"]
            store.add_feedback(
                aid, 0, 0.0, 1.0, "positive", {0: 1.0}, source="test"
            )
            store.add_feedback(
                aid, 1, 1.0, -0.5, "negative", {0: 0.0}, source="test"
            )
            store.add_historical_experience(
                aid, 101, 0, 0.0, 1.0, 30.0, {0: 1.0}
            )
            store.add_historical_experience(
                aid, 102, 1, 1.0, 0.5, 40.0, {0: 0.0}
            )

            rows = store.list_agents()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["feedback_count"], 2)
        self.assertEqual(row["positive_count"], 1)
        self.assertEqual(row["negative_count"], 1)
        self.assertAlmostEqual(row["average_reward"], 0.25)
        self.assertEqual(row["historical_count"], 2)
        self.assertAlmostEqual(row["historical_average_reward"], 0.75)


class StartupDiagnosticsSourceContractTests(unittest.TestCase):
    def test_startup_phase_timings_and_frontend_hydration_are_shipped(self):
        root = Path(__file__).resolve().parents[1]
        main = (root / "adaptive_ai" / "src" / "main.py").read_text(
            encoding="utf-8"
        )
        app = (
            root / "adaptive_ai" / "src" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        settings = (
            root / "adaptive_ai" / "src" / "settings.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"timings": {}', main)
        self.assertIn('"current_state_seconds"', main)
        self.assertIn("startup_warmup_targets_per_tick", settings)
        self.assertIn("agent_metrics_deferred", app)
        self.assertIn("startup_warmup_remaining_targets", app)


if __name__ == "__main__":
    unittest.main()
