import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import *
import engine as engine_module
from engine import Engine


class FakeStore:
    def __init__(self, rows):
        self.rows = {row["id"]: dict(row) for row in rows}

    def get_agent_config(self, agent_id):
        row = self.rows.get(agent_id)
        return dict(row) if row else None


class EventDrivenInferenceTests(unittest.TestCase):
    def make_engine(self):
        runtime = Engine()
        self.addCleanup(
            lambda: runtime.control_workers.shutdown(wait=False, cancel_futures=True)
        )
        self.addCleanup(
            lambda: runtime.poll_worker.shutdown(wait=False, cancel_futures=True)
        )
        return runtime

    def test_agent_dependencies_are_local_not_all_presence_sources(self):
        runtime = self.make_engine()
        runtime.context.mapping = {
            "light.kitchen": "kitchen",
            "binary_sensor.kitchen_presence": "kitchen",
            "binary_sensor.living_presence": "living",
            "binary_sensor.hall_motion": "hall",
        }
        runtime.context.home.area_sources = {
            "kitchen": {"binary_sensor.kitchen_presence"},
            "living": {"binary_sensor.living_presence"},
            "hall": {"binary_sensor.hall_motion"},
        }
        policy = SimpleNamespace(
            schema=SimpleNamespace(entities=("binary_sensor.hall_motion",))
        )
        configured = agent(
            id="kitchen",
            input_entities=["binary_sensor.manual_hint"],
        )

        deps = runtime.event_dependencies(configured, policy)

        self.assertIn("light.kitchen", deps)
        self.assertIn("binary_sensor.kitchen_presence", deps)
        self.assertIn("binary_sensor.hall_motion", deps)
        self.assertIn("binary_sensor.manual_hint", deps)
        self.assertNotIn("binary_sensor.living_presence", deps)

    def test_fast_idle_heartbeat_is_ten_seconds_but_off_deadline_is_exact(self):
        runtime = self.make_engine()
        configured = agent(id="lamp")
        rt = {}

        due = runtime._schedule_next_inference(configured, rt, timestamp=100.0)
        self.assertEqual(due, 110.0)

        rt.update(
            fast_off_confirmation_active=True,
            fast_off_candidate_since=101.0,
            fast_off_confirmation_required=6.0,
        )
        due = runtime._schedule_next_inference(configured, rt, timestamp=102.0)
        self.assertEqual(due, 107.0)

    def test_manual_hold_suppresses_idle_policy_recomputation(self):
        runtime = self.make_engine()
        configured = agent(id="lamp")
        rt = {"manual_override_until": 400.0}

        due = runtime._schedule_next_inference(configured, rt, timestamp=100.0)

        self.assertEqual(due, 400.0)

    def test_timer_wheel_dispatches_only_due_targets_and_claims_deadline(self):
        runtime = self.make_engine()
        a = agent(id="a", target_entity="light.a")
        b = agent(id="b", target_entity="light.b")
        runtime.runtime = {
            "a": {"next_periodic_inference_ts": 99.0},
            "b": {"next_periodic_inference_ts": 130.0},
        }
        fake = FakeStore([a, b])

        with patch.object(engine_module, "STORE", fake):
            due = runtime._due_inference_targets(timestamp=100.0)

        self.assertEqual(due, {"light.a"})
        self.assertEqual(runtime.runtime["a"]["next_periodic_inference_ts"], 160.0)
        self.assertEqual(runtime.runtime["b"]["next_periodic_inference_ts"], 130.0)

    def test_scheduler_starts_with_exactly_one_initial_full_pass_pending(self):
        runtime = self.make_engine()
        self.assertTrue(runtime.initial_inference_pending)
        self.assertEqual(runtime.inference_scheduler["initial_full_passes"], 0)
        self.assertEqual(runtime.inference_scheduler["event_passes"], 0)
        self.assertEqual(runtime.inference_scheduler["timer_passes"], 0)


if __name__ == "__main__":
    unittest.main()
