"""0.14.16 regressions for Raspberry-Pi startup pressure and duplicate UI reads."""
import inspect
import os
import subprocess
import sys
import tempfile
import unittest

from support import ROOT


class Release016QuietStartupTests(unittest.TestCase):
    def run_isolated(self, script):
        with tempfile.TemporaryDirectory() as data:
            env = dict(os.environ, ADAPTIVE_AI_DATA=data,
                       PYTHONPATH=str(ROOT/'adaptive_ai/src'), PYTHONIOENCODING='utf-8')
            env.pop('SUPERVISOR_TOKEN', None)
            env.pop('HA_TOKEN', None)
            result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                    env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_final_entrypoint_installs_quiet_start_resource_guard(self):
        self.run_isolated(r'''
import trial_queue_main as entry
core = entry.core
assert core._release_016_guard_installed is True
contract = core.release_016_resource_guard_contract
assert contract['startup'] == 'saved_agents_and_realtime_then_fresh_install_discovery'
assert contract['automatic_background_discovery'] == 'fresh_install_once_then_explicit_rescan'
assert contract['background_archive_cpu'] == '20pct_default_duty_cycle_when_explicit'
assert contract['recorder_timeout'] == '120s_circuit_breaker_no_recursive_burst'
snapshot = core.RELEASE_016_RESOURCE_GUARD()
assert snapshot['background_grace_seconds'] >= 60
assert snapshot['background_cpu_duty_cycle'] == 0.20
assert snapshot['automation_scan_workers'] == 1
''')

    def test_history_first_cycle_stays_quiet_but_fresh_install_gets_one_async_discovery(self):
        source = (ROOT/'adaptive_ai/src/release_016_guard.py').read_text(encoding='utf-8')
        self.assertIn('def quiet_start(history_self)', source)
        self.assertIn('Saved agents + realtime only; no Recorder/API backfill during startup', source)
        self.assertIn('INITIAL_DISCOVERY_META_KEY = "initial_discovery_complete"', source)
        self.assertIn('_initial_discovery_needed(store)', source)
        self.assertIn('threshold_override=1, reason="fresh_install"', source)
        self.assertIn('core.startup_snapshot().get("ready")', source)
        # The History thread still never runs a synchronous/periodic Recorder bootstrap.
        # Fresh-install work goes through the async single-flight request instead.
        self.assertNotIn('original_bootstrap(history_self)', source)

    def test_background_archive_and_recorder_have_separate_circuit_breakers(self):
        source = (ROOT/'adaptive_ai/src/release_016_guard.py').read_text(encoding='utf-8')
        self.assertIn('threading.current_thread().name != "adaptive-ai-history"', source)
        self.assertIn('BACKGROUND_DUTY_CYCLE = 0.20', source)
        self.assertIn('BACKGROUND_BATCH_ROWS = 128', source)
        self.assertIn('RECORDER_BACKOFF_SECONDS = 120.0', source)
        self.assertIn('if HEAVY_JOBS.owner == "discovery"', source)
        self.assertIn('history_background_backoff', source)
        self.assertIn('return 0', source)

    def test_automation_config_reads_are_serial_and_scan_time_persists(self):
        source = (ROOT/'adaptive_ai/src/release_016_guard.py').read_text(encoding='utf-8')
        self.assertIn('kwargs["max_workers"] = 1', source)
        self.assertIn('args = (1, *args[1:])', source)
        self.assertIn('automation_scan_last_ts', source)
        self.assertIn('ha_module.AUTOMATION_KNOWLEDGE.scan = persistent_scan', source)

    def test_shared_get_broker_coalesces_only_read_hotspots(self):
        static = ROOT/'adaptive_ai/src/static'
        guard = (static/'polling_guard.js').read_text(encoding='utf-8')
        index = (static/'index.html').read_text(encoding='utf-8')
        self.assertIn("if(method!=='GET'&&method!=='HEAD')return upstreamFetch(input,init);", guard)
        self.assertIn("if(base.endsWith('api/live'))return 900", guard)
        self.assertIn("if(base.endsWith('api/candidates'))return 1800", guard)
        self.assertIn("if(base.endsWith('api/agents'))return 3000", guard)
        self.assertIn('if(inflight.has(key))return inflight.get(key).then(responseFrom);', guard)
        self.assertIn('cache.set(key,{at:performance.now(),value})', guard)
        self.assertLess(index.index('home.js'), index.index('polling_guard.js'))
        self.assertLess(index.index('polling_guard.js'), index.index('app.js'))


if __name__ == '__main__':
    unittest.main()
