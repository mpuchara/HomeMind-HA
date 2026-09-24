import threading
import unittest

from training_budget import CooperativeTrainingBudget
from support import ROOT


class FakeClock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += float(seconds)


class RealtimePreemptionTests(unittest.TestCase):
    def test_interactive_window_preempts_training_checkpoint(self):
        clock = FakeClock()
        budget = CooperativeTrainingBudget(clock=clock, sleeper=clock.sleep)
        budget.configure(
            duty_cycle=.2,
            max_slice_seconds=.05,
            max_sleep_seconds=2.0,
            thread_prefixes=("worker",),
            realtime_max_burst_seconds=.45,
            realtime_cooldown_seconds=.20,
        )
        self.assertTrue(budget.begin(thread_name="worker"))
        clock.now = .02
        budget.request_interactive_window(.5, reason="ha_state_changed")
        slept = budget.checkpoint("unit", thread_name="worker")
        # One strict scheduler hand-off uses the configured work quantum, not a fixed
        # 250 ms sleep and not one sleep per micro-checkpoint.
        self.assertAlmostEqual(slept, .05, places=6)
        self.assertAlmostEqual(clock.now, .07, places=6)
        slept2 = budget.checkpoint("unit2", thread_name="worker")
        self.assertAlmostEqual(slept2, 0.0, places=6)
        self.assertAlmostEqual(clock.now, .07, places=6)
        stats = budget.snapshot()
        self.assertEqual(stats["interactive_preemptions"], 1)
        self.assertAlmostEqual(stats["interactive_sleep_seconds"], .05, places=6)
        self.assertEqual(stats["interactive_priority_epochs"], 1)
        self.assertEqual(stats["interactive_yield_quantum_ms"], 50.0)
        self.assertTrue(str(stats["last_checkpoint"]).startswith("interactive:"))

    def test_many_micro_checkpoints_pay_one_priority_yield_per_burst(self):
        clock = FakeClock()
        budget = CooperativeTrainingBudget(clock=clock, sleeper=clock.sleep)
        budget.configure(
            duty_cycle=.65,
            max_slice_seconds=.035,
            max_sleep_seconds=.5,
            thread_prefixes=("worker",),
            realtime_max_burst_seconds=.45,
            realtime_cooldown_seconds=.20,
        )
        budget.begin(thread_name="worker")
        budget.request_interactive_window(.30, reason="ha_state_changed")
        total_sleep = 0.0
        for _ in range(64):
            clock.now += .0001
            total_sleep += budget.checkpoint("micro", thread_name="worker")
        stats = budget.snapshot()
        self.assertLessEqual(total_sleep, .055)
        self.assertEqual(stats["interactive_preemptions"], 1)
        self.assertEqual(stats["interactive_priority_epochs"], 1)
        self.assertGreater(stats["slice_checkpoints"], 0)
        self.assertLess(stats["interactive_sleep_seconds"], .055)

    def test_realtime_extensions_do_not_create_new_priority_epochs(self):
        clock = FakeClock()
        budget = CooperativeTrainingBudget(clock=clock, sleeper=clock.sleep)
        budget.configure(
            duty_cycle=.65,
            max_slice_seconds=.035,
            max_sleep_seconds=.5,
            thread_prefixes=("worker",),
            realtime_max_burst_seconds=.45,
            realtime_cooldown_seconds=.20,
        )
        budget.begin(thread_name="worker")
        for _ in range(5):
            budget.request_interactive_window(.30, reason="ha_state_changed")
            clock.now += .01
            budget.checkpoint("event", thread_name="worker")
        stats = budget.snapshot()
        self.assertEqual(stats["interactive_priority_epochs"], 1)
        self.assertEqual(stats["interactive_preemptions"], 1)

    def test_explicit_user_action_escalates_active_realtime_epoch(self):
        clock = FakeClock()
        budget = CooperativeTrainingBudget(clock=clock, sleeper=clock.sleep)
        budget.configure(
            duty_cycle=.65,
            max_slice_seconds=.035,
            max_sleep_seconds=.5,
            thread_prefixes=("worker",),
            realtime_max_burst_seconds=.45,
            realtime_cooldown_seconds=.20,
        )
        budget.begin(thread_name="worker")
        budget.request_interactive_window(.30, reason="ha_state_changed")
        first = budget.checkpoint("event", thread_name="worker")
        self.assertGreater(first, 0.0)
        budget.request_interactive_window(.90, reason="correct_label")
        second = budget.checkpoint("correct", thread_name="worker")
        self.assertGreater(second, 0.0)
        stats = budget.snapshot()
        self.assertEqual(stats["interactive_priority_epochs"], 2)
        self.assertEqual(stats["interactive_epoch_escalations"], 1)
        self.assertEqual(stats["interactive_preemptions"], 2)

    def test_steady_realtime_traffic_still_allows_training_progress(self):
        for hz in (2, 4, 10):
            with self.subTest(hz=hz):
                clock = FakeClock()
                budget = CooperativeTrainingBudget(clock=clock, sleeper=clock.sleep)
                budget.configure(
                    duty_cycle=.65,
                    max_slice_seconds=.035,
                    max_sleep_seconds=.5,
                    thread_prefixes=("worker",),
                    realtime_max_burst_seconds=.45,
                    realtime_cooldown_seconds=.20,
                )
                budget.begin(thread_name="worker")
                active_work = 0.0
                next_event = 0.0
                end = 4.0
                while clock.now < end:
                    if clock.now + 1e-12 >= next_event:
                        budget.request_interactive_window(.30, reason="ha_state_changed")
                        next_event += 1.0 / hz
                    # Deterministic 5 ms unit of useful replay work.
                    clock.now += .005
                    active_work += .005
                    budget.checkpoint("steady", thread_name="worker")
                stats = budget.snapshot()
                self.assertGreater(active_work, .25)
                self.assertGreater(stats["slice_checkpoints"], 0)
                self.assertLess(stats["interactive_sleep_seconds"], end * .45)
                self.assertGreater(stats["interactive_requests_suppressed"], 0 if hz >= 4 else -1)

    def test_realtime_event_storm_hits_short_cap_and_cooldown(self):
        clock = FakeClock()
        budget = CooperativeTrainingBudget(clock=clock, sleeper=clock.sleep)
        budget.configure(
            duty_cycle=.65,
            max_slice_seconds=.035,
            max_sleep_seconds=.5,
            thread_prefixes=("worker",),
            realtime_max_burst_seconds=.45,
            realtime_cooldown_seconds=.20,
        )
        clock.now = .10
        self.assertAlmostEqual(
            budget.request_interactive_window(.30, reason="ha_state_changed"),
            .40, places=6,
        )
        clock.now = .20
        self.assertAlmostEqual(
            budget.request_interactive_window(.40, reason="realtime_inference"),
            .55, places=6,
        )
        clock.now = .30
        self.assertAlmostEqual(
            budget.request_interactive_window(.30, reason="ha_state_changed"),
            .55, places=6,
        )
        clock.now = .56
        self.assertAlmostEqual(
            budget.request_interactive_window(.30, reason="ha_state_changed"),
            .55, places=6,
        )
        self.assertEqual(budget.snapshot()["interactive_requests_suppressed"], 1)

    def test_non_realtime_interaction_keeps_longer_priority_window(self):
        clock = FakeClock()
        budget = CooperativeTrainingBudget(clock=clock, sleeper=clock.sleep)
        clock.now = .10
        until = budget.request_interactive_window(.90, reason="correct_label")
        self.assertAlmostEqual(until, 1.00, places=6)

    def test_engine_requests_priority_window_on_ha_event(self):
        source = (ROOT / "adaptive_ai" / "src" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("TRAINING_BUDGET.request_interactive_window(", source)
        self.assertIn('reason="ha_state_changed"', source)
        self.assertIn('reason="realtime_inference"', source)
        self.assertIn('"training_realtime_event_priority_seconds"', source)
        self.assertIn('"training_realtime_inference_priority_seconds"', source)

    def test_ui_uses_only_recent_60s_event_latency(self):
        p0 = (ROOT / "adaptive_ai" / "src" / "static" / "p0.js").read_text(encoding="utf-8")
        home = (ROOT / "adaptive_ai" / "src" / "static" / "home.js").read_text(encoding="utf-8")
        self.assertIn("const p95=latency.recent_p95_ms;", p0)
        self.assertNotIn("recent_p95_ms??latency.p95_ms", p0)
        self.assertIn("const inferenceP95=inf.recent_p95_ms, eventP95=latency.recent_p95_ms;", home)

    def test_correct_label_context_reconstruction_has_interactive_priority(self):
        source = (ROOT / "adaptive_ai" / "src" / "manual_feedback_workflow.py").read_text(encoding="utf-8")
        self.assertIn('reason="correct_label"', source)
        self.assertIn("TRAINING_BUDGET.request_interactive_window(2.0", source)

    def test_correct_history_requests_priority_and_never_calls_policy_history(self):
        source = (ROOT / "adaptive_ai" / "src" / "agent_correct_generation_history.py").read_text(encoding="utf-8")
        self.assertIn('reason="correct_history"', source)
        self.assertIn('reason="correct_point"', source)
        self.assertIn("_live_observed_history", source)
        live_block = source.split('if generation.get("generation_type") == "live":', 1)[1].split(
            'parent_id = generation.get("parent_generation_id")', 1
        )[0]
        self.assertNotIn("legacy_history(", live_block)
        point_block = source.split("def build_correct_point", 1)[1].split("def install", 1)[0]
        self.assertNotIn("legacy_point(", point_block)


if __name__ == "__main__":
    unittest.main()
