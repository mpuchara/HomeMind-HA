"""Compare the real Recorder request target with the 0.8.0 Git baseline."""
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import unittest
from urllib.error import HTTPError

import support
from ha import HAClient


# Captured from HAClient.history in 8b3fdbc390e27e2da46691582c890c8b41fb1c41.
# The same inputs generated identical targets in 2a17e888, a178071c and b3a67c0a.
LEGACY_FULL = (
    "/api/history/period/2026-11-10T10%3A00%3A00%2B01%3A00"
    "?filter_entity_id=light.first%2Clight.second&end_time=2026-11-10T10%3A30%3A00%2B01%3A00"
)
LEGACY_COARSE = LEGACY_FULL + "&minimal_response=&no_attributes=&significant_changes_only="


class RecorderWireComparisonTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        seen = self.requests

        class RecorderHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.command, self.path, self.headers["Authorization"]))
                self.send_error(500, "Synthetic HTTP error")

            def log_message(self, *args):
                pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), RecorderHandler))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(server.shutdown)
        self.client = HAClient(f"http://127.0.0.1:{server.server_port}/api", "private-test-token")

    def history_failure(self, **flags):
        log = io.StringIO()
        with redirect_stdout(log), self.assertRaises(HTTPError) as ctx:
            self.client.history(
                ["light.first", "light.second"],
                "2026-11-10T10:00:00+01:00", "2026-11-10T10:30:00+01:00",
                **flags,
            )
        self.assertEqual(ctx.exception.code, 500)
        self.assertNotIn("private-test-token", log.getvalue())
        return json.loads(log.getvalue().removeprefix("[recorder] history_http_error "))

    def test_full_history_exposes_exact_target_and_single_factor_control(self):
        detail = self.history_failure(minimal=False, no_attributes=False, significant=False)
        target = LEGACY_FULL + "&significant_changes_only=0"
        self.assertEqual(self.requests, [("GET", target, "Bearer private-test-token")])
        self.assertEqual(detail["method"], "GET")
        self.assertEqual(detail["api_target"], target)
        self.assertEqual(detail["legacy_significance_api_target"], LEGACY_FULL)
        self.assertEqual(datetime.fromisoformat(detail["failed_at_utc"]).utcoffset().total_seconds(), 0)

    def test_coarse_history_has_same_wire_target_as_legacy(self):
        detail = self.history_failure()
        self.assertEqual(self.requests, [("GET", LEGACY_COARSE, "Bearer private-test-token")])
        self.assertEqual(detail["api_target"], LEGACY_COARSE)
        self.assertIsNone(detail["legacy_significance_api_target"])

    def test_legacy_control_retains_initial_state_option_for_right_split(self):
        detail = self.history_failure(
            minimal=False, no_attributes=False, significant=False, skip_initial_state=True,
        )
        self.assertEqual(detail["legacy_significance_api_target"], LEGACY_FULL + "&skip_initial_state=")
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
