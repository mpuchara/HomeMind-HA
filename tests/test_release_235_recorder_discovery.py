"""0.14.235: discovery must not misreport incomplete Recorder history."""
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import history as history_module
from history import DiscoveryRecorderIncomplete, HistoryManager
from release_016_guard import _initial_discovery_needed
from support import state


class MemoryMetadata:
    def __init__(self):
        self.values = {}
        self.events = []

    def meta_get(self, key, default=None):
        return self.values.get(key, default)

    def meta_set(self, key, value):
        self.values[key] = value

    def event(self, *args):
        self.events.append(args)

    def qualified_agents(self):
        return []

    def list_agent_configs(self):
        return []


def manager(fetch=None):
    obj = object.__new__(HistoryManager)
    obj.stop_event = threading.Event()
    obj.job_cancel_event = None
    obj.recorder_skipped_slices = 0
    obj._discovery_recorder_failures = 0
    obj.set_status = lambda *args, **kwargs: None
    obj.progress = 0.0
    obj.history_rows_per_second = 0.0
    obj.lock = threading.RLock()
    obj.auto_created = 0
    obj._eligible_rebuild_context = lambda: []
    obj.auto_discover_agents = lambda *a, **k: 0
    obj._fetch_history_resilient = fetch or (lambda *a, **k: 0)
    return obj


def import_section(obj):
    return obj._import_section(
        ["light.a"], 0.0, 7200.0, batch_size=1,
        minimal=False, no_attributes=False, source="ha_history_full",
        progress_lo=0.0, progress_hi=1.0, label="test", max_hours=1,
        require_complete=True, resume_discovery=True,
    )


class RecorderDiscovery235Tests(unittest.TestCase):
    def test_incomplete_slice_never_advances_successful_interval(self):
        store = MemoryMetadata()
        requests = []
        obj = manager()

        def fetch(ids, lo, hi, **kw):
            requests.append((lo, hi))
            if len(requests) == 2:
                obj.recorder_skipped_slices += 1
                return 0
            return 1

        obj._fetch_history_resilient = fetch
        with patch.object(history_module, "STORE", store):
            with self.assertRaises(DiscoveryRecorderIncomplete):
                import_section(obj)
            self.assertEqual(requests, [(3600.0, 7200.0), (0.0, 3600.0)])
            self.assertEqual(obj._discovery_missing_intervals("ha_history_full", "light.a", 0, 7200),
                             [(0.0, 3600.0)])
            resumed_requests = []
            obj2 = manager(lambda ids, lo, hi, **kw: (resumed_requests.append((lo, hi)) or 1))
            self.assertEqual(import_section(obj2), 1)
            self.assertEqual(resumed_requests, [(0.0, 3600.0)])
            self.assertEqual(obj2._discovery_missing_intervals("ha_history_full", "light.a", 0, 7200), [])
            self.assertEqual(import_section(obj2), 0)
            self.assertEqual(len(resumed_requests), 1)

    def test_new_end_time_requests_only_uncovered_tail(self):
        store = MemoryMetadata()
        obj = manager()
        with patch.object(history_module, "STORE", store):
            obj.discovery_recorder_coverage = {}
            obj._mark_discovery_coverage("ha_history_full", ["light.a"], 0, 3600)
            obj._mark_discovery_coverage("ha_history_full", ["light.a"], 7200, 10800)
            obj2 = manager()
            obj2.discovery_recorder_coverage = obj2._load_discovery_coverage()
            self.assertEqual(obj2._discovery_missing_intervals(
                "ha_history_full", "light.a", 0, 14400), [(3600.0, 7200.0), (10800.0, 14400.0)])
            self.assertEqual(obj2._discovery_missing_intervals(
                "ha_history_discovery_coarse", "light.a", 0, 14400), [(0.0, 14400.0)])

    def test_interrupted_discovery_with_created_agents_resumes(self):
        store = MemoryMetadata()
        store.list_agent_configs = lambda: [{"id": "agent-from-interrupted-scan"}]
        self.assertFalse(_initial_discovery_needed(store))
        store.values["discovery_recorder_coverage_v1"] = '{"contract":"discovery_recorder_coverage_v1","rows":[]}'
        self.assertTrue(_initial_discovery_needed(store))
        store.values["initial_discovery_complete"] = "older_valid_marker"
        self.assertFalse(_initial_discovery_needed(store))

    def test_recursive_successful_children_survive_failed_sibling(self):
        store = MemoryMetadata()
        obj = manager()
        # Use the real recursive method, not manager()'s test-only stub.
        del obj._fetch_history_resilient
        obj.training_job_agent_id = None
        obj.training_preparation_profile = {}
        called = []
        def fake_history(ids, start, end, **kw):
            called.append(tuple(ids))
            if len(ids) == 2 or ids == ["light.b"]:
                raise TimeoutError("timeout")
            return []
        with patch.object(history_module, "STORE", store), patch.object(
            history_module, "rss_mb", return_value=0
        ), patch.object(history_module.HA, "history", side_effect=fake_history):
            obj.discovery_recorder_coverage = {}
            obj._archive_history_payload = lambda data, source: 0
            self.assertEqual(obj.__class__._fetch_history_resilient(
                obj, ["light.a", "light.b"], 0, 1800, minimal=True,
                no_attributes=True, source="ha_history_discovery_coarse",
                on_successful_leaf=lambda ids, lo, hi: obj._mark_discovery_coverage(
                    "ha_history_discovery_coarse", ids, lo, hi
                )), 0)
        self.assertEqual(obj._discovery_missing_intervals(
            "ha_history_discovery_coarse", "light.a", 0, 1800), [])
        self.assertEqual(obj._discovery_missing_intervals(
            "ha_history_discovery_coarse", "light.b", 0, 1800), [(0.0, 1800.0)])
        self.assertEqual(called, [("light.a", "light.b"), ("light.a",), ("light.b",)])

    def test_invalid_progress_metadata_is_only_cache_miss(self):
        store = MemoryMetadata()
        store.values["discovery_recorder_coverage_v1"] = "{invalid-json"
        with patch.object(history_module, "STORE", store):
            self.assertEqual(manager()._load_discovery_coverage(), {})

    def test_deep_failure_does_not_mark_discovery_as_complete(self):
        store = MemoryMetadata()
        obj = manager()
        obj._import_section = lambda *a, **k: (_ for _ in ()).throw(
            DiscoveryRecorderIncomplete("missing slice"))
        with patch.object(history_module, "STORE", store), patch.dict(
            history_module.OPTIONS, {"history_bootstrap_days": 2, "manual_discovery_hours": 6}
        ):
            with self.assertRaises(DiscoveryRecorderIncomplete):
                obj._manual_lightweight_cycle({"light.a": state("light.a", "off")},
                                              ["light.a"], 1700000000.0)
            self.assertNotIn("discovery_deep_history_complete", store.values)
            self.assertNotIn("initial_discovery_complete", store.values)
            self.assertNotIn("manual_discovery_refresh", store.values)

    def test_recent_failure_preserves_correct_existing_marker(self):
        store = MemoryMetadata()
        store.values["discovery_deep_history_complete"] = "existing"
        obj = manager()
        obj._import_section = lambda *a, **k: (_ for _ in ()).throw(
            DiscoveryRecorderIncomplete("missing recent"))
        with patch.object(history_module, "STORE", store), patch.dict(
            history_module.OPTIONS, {"history_bootstrap_days": 2, "manual_discovery_hours": 6}
        ):
            with self.assertRaises(DiscoveryRecorderIncomplete):
                obj._manual_lightweight_cycle({"light.a": state("light.a", "off")},
                                              ["light.a"], 1700000000.0)
            self.assertEqual(store.meta_get("discovery_deep_history_complete"), "existing")
            self.assertNotIn("initial_discovery_complete", store.values)

    def test_full_scan_sets_deep_and_initial_only_after_import(self):
        store = MemoryMetadata()
        obj = manager()
        calls = []
        obj._import_section = lambda *a, **k: calls.append(k)
        with patch.object(history_module, "STORE", store), patch.dict(
            history_module.OPTIONS, {"history_bootstrap_days": 2, "manual_discovery_hours": 6}
        ):
            obj._manual_lightweight_cycle({"light.a": state("light.a", "off")},
                                          ["light.a"], 1700000000.0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(k["require_complete"] and k["resume_discovery"] for k in calls))
        self.assertIn("discovery_deep_history_complete", store.values)
        self.assertIn("initial_discovery_complete", store.values)
        self.assertIn("manual_discovery_refresh", store.values)

    def test_cancellation_after_last_chunk_must_not_advance(self):
        store = MemoryMetadata()
        obj = manager()
        def fetch(ids, lo, hi, **kw):
            obj.stop_event.set()
            return 1
        obj._fetch_history_resilient = fetch
        with patch.object(history_module, "STORE", store):
            with self.assertRaises(InterruptedError):
                import_section(obj)
            self.assertNotIn("initial_discovery_complete", store.values)

    def test_http500_and_timeout_terminal_slices_do_not_leak_entity_or_url(self):
        store = MemoryMetadata()
        obj = manager()
        obj.training_job_agent_id = None
        obj.training_preparation_profile = {}
        with patch.object(history_module, "STORE", store), patch.object(
            history_module, "rss_mb", return_value=0
        ), patch.object(history_module.HA, "history", side_effect=[
            HTTPError("http://ha/history?token=PRIVATE", 500, "failure", {}, None),
            TimeoutError("secret token=PRIVATE"),
        ]):
            self.assertEqual(obj.__class__._fetch_history_resilient(
                obj, ["light.secret"], 0, 1800, minimal=False,
                no_attributes=False, source="ha_history_full"), 0)
            self.assertEqual(obj.__class__._fetch_history_resilient(
                obj, ["light.secret"], 0, 1800, minimal=False,
                no_attributes=False, source="ha_history_full"), 0)
        self.assertEqual(obj.recorder_skipped_slices, 2)
        self.assertNotIn("PRIVATE", str(store.events))
        self.assertNotIn("light.secret", str(store.events))


if __name__ == "__main__":
    unittest.main()
