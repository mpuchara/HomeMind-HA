import json
from datetime import datetime, timedelta
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import support
import recorder_diagnostics as diag


class FakeResponse(BytesIO):
    status = 200


class RecorderDiagnosticsTests(unittest.TestCase):
    def test_five_sequential_targets_and_safe_report(self):
        seen = []

        def open_request(request, timeout):
            seen.append(request)
            if len(seen) == 2:
                raise HTTPError(request.full_url, 500, "private exception", {}, BytesIO(b"private body"))
            return FakeResponse(b'[[{"state":"private state"}]]')

        with patch.object(diag, "build_opener") as factory:
            factory.return_value.open.side_effect = open_request
            rows = list(diag.run("sensor.control", "supervisor-secret", "core-secret"))

        self.assertEqual([row["test"] for row in rows], list("ABCDE"))
        self.assertEqual([row["http"] for row in rows], [200, 500, 200, 200, 200])
        self.assertEqual(rows[0]["records"], 1)
        self.assertTrue(all(row["duration_ms"] >= 0 for row in rows))
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(request.get_method() == "GET" for request in seen))
        self.assertTrue(all(request.full_url.startswith(diag.SUPERVISOR + "/") for request in seen[:4]))
        self.assertEqual(seen[4].full_url.removeprefix(diag.CORE),
                         seen[1].full_url.removeprefix(diag.SUPERVISOR))
        self.assertEqual([request.get_header("Authorization") for request in seen],
                         ["Bearer supervisor-secret"] * 4 + ["Bearer core-secret"])

        queries = [parse_qs(urlsplit(request.full_url).query, keep_blank_values=True) for request in seen]
        self.assertEqual(queries[0], {"filter_entity_id": [diag.MILIGHT], "end_time": [diag.END]})
        self.assertEqual(queries[1], queries[0] | dict.fromkeys(diag.FLAGS, [""]))
        self.assertEqual(queries[2], queries[1] | {"filter_entity_id": ["sensor.control"]})
        self.assertEqual(queries[3], queries[1] | {"end_time": [rows[3]["end_time"]]})
        self.assertIn("43.619576%2B00%3A00", seen[0].full_url)
        self.assertEqual(datetime.fromisoformat(rows[3]["end_time"]) -
                         datetime.fromisoformat(rows[3]["start_time"]), timedelta(hours=1))
        for secret in ("private", "supervisor-secret", "core-secret"):
            self.assertNotIn(secret, json.dumps(rows))

    def test_missing_credentials_skip_without_requests(self):
        with patch.object(diag, "build_opener") as factory:
            rows = list(diag.run("sensor.control", None))
            factory.return_value.open.assert_not_called()
        self.assertEqual([row["skipped"] for row in rows],
                         ["missing_supervisor_token"] * 4 + ["missing_core_token"])
        self.assertTrue(all(row["http"] is None and row["duration_ms"] is None for row in rows))

    def test_core_redirect_is_not_followed(self):
        self.assertIsNone(diag.NoRedirect().redirect_request(
            None, None, 302, "", {}, "http://other-host/"))
