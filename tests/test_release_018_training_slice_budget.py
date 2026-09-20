"""0.14.18 regressions for hard UI responsiveness during explicit training."""
import os
import subprocess
import sys
import tempfile
import unittest

from support import ROOT


class Release018TrainingSliceBudgetTests(unittest.TestCase):
    def run_isolated(self, script):
        with tempfile.TemporaryDirectory() as data:
            env = dict(
                os.environ,
                ADAPTIVE_AI_DATA=data,
                PYTHONPATH=str(ROOT / "adaptive_ai/src"),
                PYTHONIOENCODING="utf-8",
            )
            env.pop("SUPERVISOR_TOKEN", None)
            env.pop("HA_TOKEN", None)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wall_clock_budget_yields_after_75ms_slice(self):
        self.run_isolated(
            r'''
from training_budget import CooperativeTrainingBudget

now = [100.0]
def clock():
    return now[0]
def sleep(seconds):
    now[0] += float(seconds)

budget = CooperativeTrainingBudget(clock=clock, sleeper=sleep)
budget.configure(
    duty_cycle=0.25,
    max_slice_seconds=0.075,
    max_sleep_seconds=2.0,
    thread_prefixes=("test-worker",),
)
assert budget.begin(thread_name="test-worker-1")
now[0] += 0.050
assert budget.checkpoint("under", thread_name="test-worker-1") == 0.0
now[0] += 0.030
pause = budget.checkpoint("over", thread_name="test-worker-1")
assert abs(pause - 0.240) < 1e-9, pause
snap = budget.snapshot()
assert snap["max_continuous_work_ms"] == 75.0
assert snap["max_observed_slice_ms"] == 80.0
assert snap["slice_overruns"] == 1
assert snap["throttle_batches"] == 1
assert snap["last_checkpoint"] == "over"
'''
        )

    def test_budget_is_inert_for_http_and_realtime_threads(self):
        self.run_isolated(
            r'''
from training_budget import CooperativeTrainingBudget

now = [0.0]
slept = []
budget = CooperativeTrainingBudget(clock=lambda: now[0], sleeper=lambda s: slept.append(s))
budget.configure(thread_prefixes=("adaptive-ai-index-",))
assert budget.begin(thread_name="ThreadingHTTPServer") is False
now[0] = 10.0
assert budget.checkpoint("http", force=True, thread_name="ThreadingHTTPServer") == 0.0
assert slept == []
'''
        )

    def test_temporal_tracker_has_inner_checkpoints(self):
        source = (ROOT / "adaptive_ai/src/replay.py").read_text(encoding="utf-8")
        self.assertIn('from training_budget import TRAINING_BUDGET', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("temporal_before_query")', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("temporal_watched_entity")', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("temporal_home_event")', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("temporal_edge_scan")', source)

    def test_post_replay_finalization_is_inside_budget(self):
        source = (ROOT / "adaptive_ai/src/history.py").read_text(encoding="utf-8")
        self.assertIn('TRAINING_BUDGET.checkpoint("replay_complete", force=True)', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("before_policy_serialize")', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("after_model_save")', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("before_partial_benchmark")', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("qualification_agent")', source)
        self.assertIn('TRAINING_BUDGET.checkpoint("finalization_complete", force=True)', source)
        self.assertIn("replay complete; yielding before finalization", source)
        self.assertIn("training checkpoint complete", source)

    def test_ui_lifeline_stays_active_through_final_gc(self):
        source = (ROOT / "adaptive_ai/src/history.py").read_text(encoding="utf-8")
        pre_gc = source.index('TRAINING_BUDGET.checkpoint("pre_training_gc", force=True)')
        collect = source.index("gc.collect()", pre_gc)
        post_gc = source.index('TRAINING_BUDGET.checkpoint("post_training_gc", force=True)', collect)
        release = source.index('HEAVY_JOBS.release("agent:" + agent_id)', post_gc)
        self.assertLess(pre_gc, collect)
        self.assertLess(collect, post_gc)
        self.assertLess(post_gc, release)

    def test_benchmark_finalization_uses_config_only_agent_read(self):
        source = (ROOT / "adaptive_ai/src/storage.py").read_text(encoding="utf-8")
        start = source.index("    def set_partial_benchmark")
        end = source.index("    def training_agent_ids", start)
        body = source[start:end]
        self.assertIn("self.get_agent_config(agent_id)", body)
        self.assertNotIn("self.get_agent(agent_id)", body)

    def test_release_exposes_short_realtime_slice_control(self):
        config = (ROOT / "adaptive_ai/config.yaml").read_text(encoding="utf-8")
        settings = (ROOT / "adaptive_ai/src/settings.py").read_text(encoding="utf-8")
        runtime = (ROOT / "adaptive_ai/src/rpi_low_power_runtime.py").read_text(encoding="utf-8")
        self.assertRegex(config, r'version: "0\.14\.\d+"')
        self.assertRegex(settings, r'APP_VERSION = "0\.14\.\d+"')
        self.assertIn("training_max_continuous_work_ms: 35", config)
        self.assertIn('training_max_continuous_work_ms: "int(25,500)"', config)
        self.assertIn('"training_max_continuous_work_ms": 35', settings)
        self.assertIn("DEFAULT_MAX_CONTINUOUS_WORK_MS = 35", runtime)
        self.assertIn("DEFAULT_TRAINING_DUTY_CYCLE = 0.55", runtime)
        self.assertIn("training_cpu_duty_cycle: 0.55", config)
        self.assertIn('"training_cpu_duty_cycle": 0.55', settings)
        self.assertIn("TRAINING_BUDGET.configure(", runtime)
        self.assertIn('TRAINING_BUDGET.checkpoint("archive_iter_row")', runtime)

    def test_activity_ui_surfaces_slice_budget(self):
        source = (ROOT / "adaptive_ai/src/static/runtime_activity_ui.js").read_text(encoding="utf-8")
        self.assertIn("lp.max_continuous_work_ms", source)
        self.assertIn("max slice", source)
        self.assertIn("lp.max_observed_slice_ms", source)
        self.assertIn("lp.slice_overruns", source)


if __name__ == "__main__":
    unittest.main()
