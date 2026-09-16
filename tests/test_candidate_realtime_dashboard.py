from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CandidateRealtimeDashboardContract(unittest.TestCase):
    def test_realtime_extension_wraps_shadow_bundle_without_control(self):
        runtime = (ROOT / "adaptive_ai/src/candidate_realtime_dashboard.py").read_text(encoding="utf-8")
        boot = (ROOT / "adaptive_ai/src/preference_queue_main.py").read_text(encoding="utf-8")
        self.assertIn("original_after = manager.after_live_process", runtime)
        self.assertIn("/api/candidate-live", runtime)
        self.assertIn('"parent_desired"', runtime)
        self.assertIn('"candidate_desired"', runtime)
        self.assertNotIn("executor", runtime.lower())
        self.assertIn("install_candidate_realtime_dashboard", boot)

    def test_candidate_decision_strip_uses_200ms_ram_endpoint_and_rejects_stale_status(self):
        ui = (ROOT / "adaptive_ai/src/static/candidate_preference_ui.js").read_text(encoding="utf-8")
        self.assertIn("api/candidate-live", ui)
        self.assertIn("setTimeout(realtimeLoop,200)", ui)
        self.assertIn("_realtime_ts", ui)
        self.assertIn("realtimeTs>persistedTs", ui)
        self.assertIn("ensureDecisionCell", ui)

    def test_current_chart_overlay_keeps_physical_trace_visible(self):
        ui = (ROOT / "adaptive_ai/src/static/chart_current_visibility.js").read_text(encoding="utf-8")
        self.assertIn('path[data-series="current"]', ui)
        self.assertIn("current-outline", ui)
        self.assertIn("appendChild(current)", ui)
        html = (ROOT / "adaptive_ai/src/static/index.html").read_text(encoding="utf-8")
        self.assertIn("chart_current_visibility.js?v=0.14.9", html)



if __name__ == "__main__":
    unittest.main()
