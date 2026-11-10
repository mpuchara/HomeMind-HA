"""0.14.234: Recorder circuit breaker classifies errors without leaking HA secrets."""
import unittest
from urllib.error import HTTPError, URLError
from pathlib import Path

from support import ROOT
from release_016_guard import recorder_failure_diagnostics


class RecorderFailureDiagnosticsTests(unittest.TestCase):
    def http(self, code):
        return HTTPError("http://ha.local/history?token=PRIVATE&filter_entity_id=light.secret", code, "failed", {}, None)

    def test_authentication_and_unsupported_are_terminal(self):
        for code, category in ((400, "unsupported_request"), (401, "authentication"),
                               (403, "authentication"), (404, "unsupported_request")):
            with self.subTest(code=code):
                result = recorder_failure_diagnostics(self.http(code))
                self.assertEqual(result["http_status"], code)
                self.assertEqual(result["category"], category)
                self.assertFalse(result["retryable"])
                self.assertFalse(result["backoff"])
                self.assertNotIn("PRIVATE", str(result))
                self.assertNotIn("light.secret", str(result))

    def test_transient_ha_errors_preserve_backoff(self):
        for code in (429, 502, 503, 504):
            with self.subTest(code=code):
                result = recorder_failure_diagnostics(self.http(code))
                self.assertTrue(result["retryable"])
                self.assertTrue(result["backoff"])
                self.assertEqual(result["http_status"], code)

    def test_splittable_responses_do_not_incur_recorder_cooldown(self):
        for error in (OSError("HA response too large; split the request"),
                      self.http(413), self.http(414), self.http(500)):
            with self.subTest(error=type(error).__name__):
                result = recorder_failure_diagnostics(error)
                self.assertTrue(result["retryable"])
                self.assertFalse(result["backoff"])

    def test_timeout_and_network_errors_remain_retryable(self):
        for error, category in ((TimeoutError("timed out"), "timeout"),
                                (URLError(TimeoutError("timed out")), "timeout"),
                                (URLError(OSError(111, "refused")), "network_error")):
            with self.subTest(error=type(error).__name__):
                result = recorder_failure_diagnostics(error)
                self.assertEqual(result["category"], category)
                self.assertTrue(result["retryable"])
                self.assertTrue(result["backoff"])

    def test_unknown_exception_requires_operator_inspection_without_secret_leak(self):
        result = recorder_failure_diagnostics(ValueError("token=PRIVATE"))
        self.assertEqual(result["category"], "unexpected_recorder_error")
        self.assertFalse(result["retryable"])
        self.assertNotIn("PRIVATE", str(result))

    def test_auto_suspension_and_manual_rescan_remain_separate_from_realtime(self):
        source = (ROOT / "adaptive_ai/src/release_016_guard.py").read_text(encoding="utf-8")
        self.assertIn('recorder_terminal = bool(state["recorder_terminal_error"])', source)
        self.assertIn('not recorder_terminal and not history_self.discovery_job_active', source)
        self.assertIn('kwargs.get("reason") != "manual"', source)
        self.assertIn('history_module.HistoryManager.request_discovery_rescan = request_discovery_with_recovery', source)
        self.assertIn('history_background_unavailable', source)
        self.assertIn('history_background_backoff', source)
        self.assertIn('if not failure["retryable"] and first:', source)
        self.assertIn('elif failure["backoff"] and first:', source)


if __name__ == "__main__":
    unittest.main()
