import inspect
import unittest
from unittest.mock import patch

from support import *
import history as history_module
from history import HistoryManager


class FakeArchiveStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def archive_iter(self, start_ts=None, end_ts=None, entity_ids=None, chunk_size=2000):
        ids = set(entity_ids or ())
        self.calls.append({
            "start_ts": start_ts,
            "entity_ids": ids,
            "chunk_size": chunk_size,
        })
        for row in self.rows:
            if start_ts is not None and float(row["ts"]) < float(start_ts):
                continue
            if ids and row["entity_id"] not in ids:
                continue
            yield dict(row)


def archived_row(row_id, entity_id, ts, value):
    return {
        "id": row_id,
        "entity_id": entity_id,
        "ts": float(ts),
        "state": str(value),
        "attributes_json": "{}",
        "context_user_id": None,
        "source": "test",
    }


class DiscoveryQoSTests(unittest.TestCase):
    def test_usage_summary_streams_all_targets_once_and_preserves_change_semantics(self):
        rows = [
            archived_row(1, "light.a", 10, "off"),
            archived_row(2, "light.b", 11, "off"),
            archived_row(3, "light.a", 12, "off"),
            archived_row(4, "light.a", 13, "on"),
            archived_row(5, "light.b", 14, "on"),
            archived_row(6, "light.a", 15, "off"),
            archived_row(7, "sensor.noise", 16, "123"),
        ]
        store = FakeArchiveStore(rows)
        manager = object.__new__(HistoryManager)
        current = {
            "light.a": state("light.a", "off"),
            "light.b": state("light.b", "off"),
            "sensor.noise": state("sensor.noise", "123"),
        }

        with patch.object(history_module, "STORE", store):
            summary = manager._discovery_usage_summary(current, 0)

        self.assertEqual(len(store.calls), 1, store.calls)
        self.assertEqual(store.calls[0]["entity_ids"], {"light.a", "light.b"})
        # usage_for counted the first valid value as sample 1 and only subsequent
        # >1e-6 target-value transitions as additional samples.
        self.assertEqual(summary["light.a"]["power"]["samples"], 3)
        self.assertEqual(summary["light.a"]["power"]["last_ts"], 15.0)
        self.assertEqual(summary["light.b"]["power"]["samples"], 2)
        self.assertEqual(summary["light.b"]["power"]["last_ts"], 14.0)
        self.assertEqual(manager.discovery_usage_rows, 6)

    def test_auto_discovery_no_longer_opens_per_property_usage_iterators(self):
        source = inspect.getsource(HistoryManager.auto_discover_agents)
        self.assertIn("_discovery_usage_summary", source)
        self.assertNotIn("self.usage_for(", source)

    def test_manual_lightweight_cycle_forwards_discovery_threshold_override(self):
        source = inspect.getsource(HistoryManager._manual_lightweight_cycle)
        self.assertIn("threshold_override=None", source)
        self.assertIn("threshold_override=threshold_override", source)

    def test_discovery_request_forwards_threshold_to_bootstrap(self):
        source = inspect.getsource(HistoryManager.request_discovery_rescan)
        self.assertIn("threshold_override=1", source)
        self.assertIn("self.bootstrap_and_train(threshold_override=threshold_override)", source)

    def test_training_queue_discovery_bridge_preserves_discovery_options(self):
        source = (ROOT/'adaptive_ai/src/training_queue.py').read_text(encoding='utf-8')
        self.assertIn("def priority_cycle(current, controllable, end_ts, *args, **kwargs):", source)
        self.assertIn("original_cycle(current, controllable, end_ts, *args, **kwargs)", source)

    def test_post_recorder_cycle_does_not_refresh_full_archive_stats(self):
        source = inspect.getsource(HistoryManager._manual_lightweight_cycle)
        self.assertNotIn("self.refresh_archive_cache()", source)
        self.assertIn("single-pass local archive scan", source)


if __name__ == "__main__":
    unittest.main()
