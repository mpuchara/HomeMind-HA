"""Regression coverage for the 0.14.236 Recorder discovery transport."""
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

from test_release_235_recorder_discovery import manager
import ha
import history
from storage import Store


class RecorderHTTP500Tests(unittest.TestCase):
    def setUp(self):
        self.obj = manager()
        del self.obj._fetch_history_resilient
        self.client = ha.HAClient("http://ha/supervisor/core/api", "secret-token")
        self.requests = []
        self.covered = []
        self.local_logs = self.enterContext(patch("builtins.print"))
        self.enterContext(patch.object(history, "HEAVY_JOBS", SimpleNamespace(owner="discovery")))
        self.enterContext(patch.object(history, "HA", self.client))
        self.enterContext(patch.object(history, "rss_mb", return_value=0))
        tmp = self.enterContext(TemporaryDirectory())
        self.store = Store(Path(tmp) / "history.db")
        self.enterContext(patch.object(history, "STORE", self.store))

    def run_fetch(self, ids, span=1800, minimal=False):
        return self.obj._fetch_history_resilient(
            ids, 100, 100 + span, minimal=minimal, no_attributes=minimal,
            source="ha_history_discovery_coarse" if minimal else "ha_history_full",
            on_successful_leaf=lambda ids, lo, hi: self.covered.append((tuple(ids), lo, hi)),
        )

    def simulate(self, fails, rows=None, error_body=b"500 Internal Server Error\nsecret-token"):
        def open_request(request, timeout):
            url = urlsplit(request.full_url)
            query = parse_qs(url.query, keep_blank_values=True)
            ids = tuple(query["filter_entity_id"][0].split(","))
            start = datetime.fromisoformat(unquote(url.path.rsplit("/", 1)[-1])).timestamp()
            end = datetime.fromisoformat(query["end_time"][0]).timestamp()
            self.requests.append((ids, start, end, query))
            if fails(ids, start, end):
                raise HTTPError(request.full_url, 500, "secret-token", {}, io.BytesIO(error_body))
            return io.BytesIO(json.dumps(rows(ids, start, end, query) if rows else []).encode())

        self.enterContext(patch.object(ha, "urlopen", side_effect=open_request))

    def test_large_batch_splits_past_previous_three_error_limit(self):
        self.simulate(lambda ids, lo, hi: len(ids) > 1)
        ids = [f"light.test_{i}" for i in range(8)]
        self.run_fetch(ids, minimal=True)
        self.assertEqual(len(self.requests), 15)
        self.assertEqual(len(self.covered), len(ids))
        self.assertTrue(all(q["minimal_response"] == [""] and q["no_attributes"] == [""]
                            and q["significant_changes_only"] == [""] for _, _, _, q in self.requests))

    def test_entity_failure_retains_only_successful_coverage(self):
        self.simulate(lambda ids, lo, hi: "light.bad" in ids)
        with self.assertRaises(history.DiscoveryRecorderHTTP500):
            self.run_fetch(["light.good", "light.bad"])
        self.assertEqual(self.covered, [(("light.good",), 100, 1900)])
        line = self.local_logs.call_args.args[0]
        detail = json.loads(line.removeprefix("[recorder] history_http_error "))
        self.assertEqual(detail["status_code"], 500)
        self.assertEqual(detail["entity_ids"], ["light.bad"])
        self.assertEqual(detail["entity_count"], 1)
        self.assertEqual(detail["response_error_excerpt"], "500 Internal Server Error")
        self.assertGreater(detail["request_size"], 0)
        self.assertGreaterEqual(detail["duration_ms"], 0)
        self.assertIn("/api/history/period", detail["endpoint"])
        # Skip initial state needs one microsecond of overlap so events exactly
        # on a split boundary are still included without a synthetic seed.
        self.assertAlmostEqual(datetime.fromisoformat(detail["start_time"]).timestamp(),
                               100 - 0.000001, places=6)
        self.assertEqual(datetime.fromisoformat(detail["end_time"]).timestamp(), 1900)
        self.assertNotIn("secret-token", str(self.local_logs.call_args_list))

    def test_bad_time_slice_isolated_after_first_half_succeeds(self):
        self.simulate(lambda ids, lo, hi: hi > 1900)
        with self.assertRaises(history.DiscoveryRecorderHTTP500):
            self.run_fetch(["light.a"], span=3600)
        self.assertEqual(self.covered, [(("light.a",), 100, 1900)])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[-1][1:3], (1899.999999, 3700))

    def test_split_preserves_exact_boundary_and_attribute_only_changes(self):
        def rows(ids, lo, hi, query):
            self.assertEqual(query["significant_changes_only"], ["0"])
            self.assertNotIn("minimal_response", query)
            self.assertNotIn("no_attributes", query)
            self.assertIn("skip_initial_state", query)
            return [[{
                "entity_id": ids[0], "state": "on", "attributes": {"brightness": ts},
                "last_changed": datetime.fromtimestamp(500, timezone.utc).isoformat(),
                "last_updated": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            } for ts in (500, 1900, 2000) if lo < ts < hi]]
        self.simulate(lambda ids, lo, hi: hi - lo > 2000, rows)
        self.run_fetch(["light.a"], span=3600)
        self.run_fetch(["light.a"], span=3600)
        self.assertEqual([row["ts"] for row in self.store.archive_iter()], [500, 1900, 2000])
        self.assertEqual(len({(ids, lo, hi) for ids, lo, hi, _ in self.requests}), 3)

    def test_first_discovery_slice_avoids_recorder_start_state_sql_lookup(self):
        """A HA Core start-state query may 500 even for one entity and 30 min.

        This synthetic server reproduces that failure only when
        skip_initial_state is omitted. Discovery must never request it.
        """
        requested = []

        def open_request(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query, keep_blank_values=True)
            requested.append(query)
            if "skip_initial_state" not in query:
                raise HTTPError(request.full_url, 500, "start-state SQL failed", {},
                                io.BytesIO(b"500 Internal Server Error"))
            return io.BytesIO(b"[]")

        self.enterContext(patch.object(ha, "urlopen", side_effect=open_request))
        self.assertEqual(self.run_fetch(["light.a"], minimal=True), 0)
        self.assertEqual(len(requested), 1)
        self.assertIn("skip_initial_state", requested[0])

    def test_full_attribute_history_still_requests_non_significant_changes(self):
        requested = []

        def open_request(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query, keep_blank_values=True)
            requested.append(query)
            return io.BytesIO(b"[]")

        self.enterContext(patch.object(ha, "urlopen", side_effect=open_request))
        self.run_fetch(["light.a"], minimal=False)
        self.assertEqual(requested[0].get("significant_changes_only"), ["0"])
        self.assertIn("skip_initial_state", requested[0])
        self.assertNotIn("minimal_response", requested[0])
        self.assertNotIn("no_attributes", requested[0])

    def test_minimum_request_fails_once_and_opaque_body_is_never_logged(self):
        self.simulate(lambda *args: True, error_body=b"Bearer secret-token and private response content")
        with self.assertRaises(history.DiscoveryRecorderHTTP500):
            self.run_fetch(["light.a"])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.covered)
        detail = json.loads(self.local_logs.call_args.args[0].split(" ", 2)[-1])
        self.assertIsNone(detail["response_error_excerpt"])
        self.assertNotIn("secret-token", str(self.local_logs.call_args_list))

    def test_http500_budget_bounds_total_failures_in_split_tree(self):
        self.simulate(lambda ids, lo, hi: len(ids) > 1)
        with patch.object(history, "DISCOVERY_HTTP_500_LIMIT", 4):
            with self.assertRaisesRegex(history.DiscoveryRecorderHTTP500, "budget exhausted"):
                self.run_fetch([f"light.test_{i}" for i in range(8)])
        self.assertEqual(len(self.requests), 6)
        self.assertEqual(len(self.covered), 2)


if __name__ == "__main__":
    unittest.main()
