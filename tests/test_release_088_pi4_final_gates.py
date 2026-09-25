"""0.14.88 Stage-9 Raspberry Pi 4 final performance-gate regressions."""
import copy
import unittest

from support import ROOT

import sys
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pi4_release_gate import evaluate_reports


def profile(scenario, *, event_ms=180.0, correct=False, worker=False):
    runtime_metrics = {
        "telemetry.event_to_intent.p95": {
            "p50": event_ms,
            "p95": event_ms,
            "p99": event_ms,
            "max": event_ms,
            "samples": 20,
        }
    }
    return {
        "contract": "pi_training_profile_v2",
        "scenario": scenario,
        "release_versions_seen": ["0.14.88"],
        "duration_seconds": 120.0,
        "host": {
            "model": "Raspberry Pi 4 Model B Rev 1.5",
            "machine": "aarch64",
            "logical_cpu_count": 4,
            "mem_total_mb": 3900.0,
        },
        "host_runtime": {
            "temperature_c_max": 61.0,
            "mem_available_mb_min": 2200.0,
            "system_cpu_percent_p95": 42.0 if worker else 18.0,
        },
        "connectivity": {
            "samples": 240,
            "ha_disconnect_rate": 0.0,
            "ha_max_consecutive_disconnect_samples": 0,
            "realtime_disconnect_rate": 0.0,
            "realtime_max_consecutive_disconnect_samples": 0,
        },
        "status_probe": {
            "attempts": 240,
            "successes": 240,
            "failures": 0,
            "failure_rate": 0.0,
            "max_consecutive_failures": 0,
        },
        "http_status_latency_ms": {
            "p50": 18.0, "p95": 45.0, "p99": 70.0, "max": 90.0, "samples": 240,
        },
        "correct_http_latency_ms": {
            "path": "/api/agents/test/teach-rl-history?start=1&end=2" if correct else None,
            "p50": 120.0 if correct else None,
            "p95": 220.0 if correct else None,
            "p99": 300.0 if correct else None,
            "max": 340.0 if correct else None,
            "samples": 30 if correct else 0,
        },
        "runtime": {"cpu_one_core_percent": 22.0, "rss_mb_p95": 120.0},
        "worker": (
            {"cpu_one_core_percent": 60.0, "rss_mb_p95": 280.0}
            if worker else None
        ),
        "combined": {
            "cpu_one_core_percent": 82.0 if worker else 22.0,
            "cpu_host_percent": 20.5 if worker else 5.5,
            "rss_p95_mb_sum": 400.0 if worker else 120.0,
        },
        "worker_pids_seen": [321] if worker else [],
        "worker_concurrency": {
            "max": 1 if worker else 0,
            "samples_over_one": 0,
            "pids_seen": [321] if worker else [],
        },
        "training_progress": {
            "first": 0.2 if worker else None,
            "last": 0.7 if worker else None,
            "delta": 0.5 if worker else None,
        },
        "runtime_latency_metrics": runtime_metrics,
        "poll_failures": [],
    }


def green_suite():
    return {
        "idle": profile("idle", event_ms=160.0),
        "training": profile("training", event_ms=240.0, worker=True),
        "correct": profile("correct", event_ms=190.0, correct=True),
        "training-correct": profile(
            "training-correct", event_ms=260.0, correct=True, worker=True
        ),
    }


class Release088Pi4GateTests(unittest.TestCase):
    def test_green_real_pi4_suite_is_eligible_but_does_not_unlock_offline_rl(self):
        result = evaluate_reports(green_suite())
        self.assertEqual(result["decision"], "pass")
        self.assertTrue(result["controlled_rollout"]["eligible"])
        self.assertFalse(result["controlled_rollout"]["offline_rl_control_unlock"])

    def test_non_pi4_hardware_fails_gate(self):
        suite = green_suite()
        suite["idle"]["host"]["model"] = "Generic x86_64"
        result = evaluate_reports(suite)
        self.assertEqual(result["decision"], "fail")
        self.assertIn("raspberry_pi_4_hardware", result["failures"])

    def test_missing_event_evidence_is_inconclusive_not_synthetic_pass(self):
        suite = green_suite()
        suite["training"]["runtime_latency_metrics"] = {}
        result = evaluate_reports(suite)
        self.assertEqual(result["decision"], "inconclusive")
        self.assertIn("training.event_to_intent_p95", result["inconclusive"])

    def test_training_realtime_regression_over_two_x_fails(self):
        suite = green_suite()
        suite["idle"]["runtime_latency_metrics"]["telemetry.event_to_intent.p95"]["p95"] = 100.0
        suite["training"]["runtime_latency_metrics"]["telemetry.event_to_intent.p95"]["p95"] = 260.0
        result = evaluate_reports(suite)
        self.assertEqual(result["decision"], "fail")
        self.assertIn("training.realtime_ratio_vs_idle", result["failures"])

    def test_correct_latency_over_500_ms_fails(self):
        suite = green_suite()
        suite["training-correct"]["correct_http_latency_ms"]["p95"] = 650.0
        result = evaluate_reports(suite)
        self.assertEqual(result["decision"], "fail")
        self.assertIn("training-correct.correct_http_p95", result["failures"])

    def test_two_training_workers_fail_single_heavy_job_gate(self):
        suite = green_suite()
        suite["training"]["worker_concurrency"]["max"] = 2
        result = evaluate_reports(suite)
        self.assertEqual(result["decision"], "fail")
        self.assertIn("training.single_training_worker", result["failures"])

    def test_realtime_disconnects_fail_gate(self):
        suite = green_suite()
        suite["training"]["connectivity"]["realtime_disconnect_rate"] = 0.05
        suite["training"]["connectivity"]["realtime_max_consecutive_disconnect_samples"] = 4
        result = evaluate_reports(suite)
        self.assertEqual(result["decision"], "fail")
        self.assertIn("training.realtime_disconnect_rate", result["failures"])

    def test_whole_host_cpu_saturation_fails_gate(self):
        suite = green_suite()
        suite["training-correct"]["host_runtime"]["system_cpu_percent_p95"] = 96.0
        result = evaluate_reports(suite)
        self.assertEqual(result["decision"], "fail")
        self.assertIn("training-correct.system_cpu_p95", result["failures"])

    def test_one_missing_hardware_identity_is_inconclusive(self):
        suite = green_suite()
        suite["training"]["host"]["model"] = None
        result = evaluate_reports(suite)
        self.assertEqual(result["decision"], "inconclusive")
        self.assertIn("raspberry_pi_4_hardware", result["inconclusive"])

    def test_source_and_workflow_ship_stage9_tools(self):
        profiler = (SRC / "pi_training_profile.py").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        self.assertIn('"pi_training_profile_v2"', profiler)
        self.assertIn('"host_runtime"', profiler)
        self.assertIn('"worker_concurrency"', profiler)
        self.assertIn('"connectivity"', profiler)
        self.assertIn("system_cpu_percent_p95", profiler)
        self.assertIn("tools/evaluate_pi4_release.py", workflow)


if __name__ == "__main__":
    unittest.main()
