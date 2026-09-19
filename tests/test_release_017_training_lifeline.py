"""0.14.17 regressions for Pi-safe explicit training and UI lifeline reads."""
import os
import subprocess
import sys
import tempfile
import unittest

from support import ROOT


class Release017TrainingLifelineTests(unittest.TestCase):
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

    def test_final_entrypoint_installs_release_017_lifeline(self):
        self.run_isolated(
            r'''
import trial_queue_main as entry
core = entry.core
assert core._release_016_guard_installed is True
assert core._release_017_ui_lifeline_installed is True
contract = core.release_017_ui_lifeline_contract
assert contract['status_periodic'] == 'always_hot_state_without_engine_status_or_history_aggregates'
assert contract['agents_periodic'] == 'config_plus_hot_runtime_without_history_aggregates'
assert contract['queue_labels'] == 'config_only'
assert contract['physical_control'] == 'unchanged'
'''
        )

    def test_training_budget_pause_matches_requested_duty_cycle(self):
        self.run_isolated(
            r'''
from rpi_low_power_runtime import _budget_pause
pause = _budget_pause(0.160, 0.25, 2.0)
assert abs(pause - 0.480) < 1e-9, pause
assert _budget_pause(2.0, 0.25, 2.0) == 2.0
'''
        )

    def test_low_power_training_defaults_are_pi_safe_and_migrate_only_old_default(self):
        source = (ROOT / "adaptive_ai/src/rpi_low_power_runtime.py").read_text(encoding="utf-8")
        budget_source = (ROOT / "adaptive_ai/src/training_budget.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_ARCHIVE_BATCH_ROWS = 16", source)
        self.assertIn("DEFAULT_TRAINING_DUTY_CYCLE = 0.20", source)
        self.assertIn("DEFAULT_MAX_THROTTLE_SLEEP_SECONDS = 2.0", source)
        self.assertIn('current_duty in (0.55, 0.25)', source)
        self.assertIn('core.OPTIONS["training_cpu_duty_cycle"] = DEFAULT_TRAINING_DUTY_CYCLE', source)
        self.assertIn('effective_training_duty_cycle', budget_source)

    def test_periodic_status_never_calls_rich_engine_status(self):
        source = (ROOT / "adaptive_ai/src/release_017_ui_lifeline.py").read_text(encoding="utf-8")
        self.assertIn('if not startup.get("ready") or not core.runtime_available():', source)
        self.assertIn("payload = previous_status_payload(handler_self)", source)
        self.assertIn("Operational status must remain O(number of agents + in-memory runtime)", source)
        self.assertIn('"status_read_mode": "operational_hot"', source)
        self.assertIn("def hot_configs():", source)
        self.assertIn("core.ENGINE._refresh_agent_index()", source)
        self.assertIn("core.ENGINE.agent_configs.values()", source)
        self.assertNotIn("configs = core.STORE.list_agent_configs()", source)
        self.assertNotIn("core.ENGINE.status()", source)

    def test_agent_lifeline_uses_config_and_cached_runtime_not_history_aggregates(self):
        source = (ROOT / "adaptive_ai/src/release_017_ui_lifeline.py").read_text(encoding="utf-8")
        self.assertIn("configs = hot_configs()", source)
        self.assertNotIn("configs = core.STORE.list_agent_configs()", source)
        self.assertIn("cached = {aid: dict(value) for aid, value in rich_agents.items()}", source)
        self.assertIn("TrainingQueue._agent_label = cheap_agent_label", source)
        self.assertIn("queue_self.store.get_agent_config(agent_id)", source)
        self.assertNotIn("queue_self.store.get_agent(agent_id)", source)

    def test_release_defaults_and_schema_expose_training_budget_controls(self):
        config = (ROOT / "adaptive_ai/config.yaml").read_text(encoding="utf-8")
        settings = (ROOT / "adaptive_ai/src/settings.py").read_text(encoding="utf-8")
        self.assertRegex(config, r'version: "0\.14\.\d+"')
        self.assertIn("training_cpu_duty_cycle: 0.20", config)
        self.assertIn("training_archive_batch_rows: 16", config)
        self.assertIn("training_throttle_max_sleep_seconds: 2.0", config)
        self.assertIn("training_max_continuous_work_ms: 50", config)
        self.assertIn('training_cpu_duty_cycle: "float(0.15,0.70)"', config)
        self.assertRegex(settings, r'APP_VERSION = "0\.14\.\d+"')
        self.assertIn('if data.get("training_cpu_duty_cycle") in (0.55, 0.25):', settings)
        self.assertIn('if data.get("training_max_continuous_work_ms") == 75:', settings)

    def test_ui_surfaces_active_training_budget_instead_of_system_ready(self):
        source = (ROOT / "adaptive_ai/src/static/runtime_activity_ui.js").read_text(encoding="utf-8")
        self.assertIn("const duty=Math.round(Number(lp.training_cpu_duty_cycle||0)*100);", source)
        self.assertIn("CPU budget ${duty}%", source)
        self.assertIn("Training yields between bounded work slices so Ingress and realtime control keep CPU priority.", source)
        self.assertIn("Autonomous Candidate training", source)


if __name__ == "__main__":
    unittest.main()
