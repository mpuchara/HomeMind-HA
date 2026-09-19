"""microSD-aware RAM-first persistence regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from support import state
from provenance import ProvenanceJournal
from storage import Store


class RamFirstPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "ram-first.db")

    def tearDown(self):
        try:
            self.store.flush_events()
        except Exception:
            pass
        self.temp.cleanup()

    def test_sqlite_temp_work_uses_memory_and_larger_page_cache(self):
        with self.store.conn() as c:
            self.assertEqual(c.execute("PRAGMA temp_store").fetchone()[0], 2)
            self.assertEqual(c.execute("PRAGMA cache_size").fetchone()[0], -16384)

    def test_meta_reads_and_event_feed_poll_from_ram(self):
        self.assertTrue(self.store.meta_set("ram-key", "one"))
        self.assertFalse(self.store.meta_set("ram-key", "one"))
        original_conn = self.store.conn
        self.store.conn = Mock(side_effect=AssertionError("hot read path touched SQLite"))
        try:
            self.assertEqual(self.store.meta_get("ram-key"), "one")
            self.store.event(None, "info", "ram_only", "visible before flush", {"x": 1})
            rows = self.store.list_events(10)
        finally:
            self.store.conn = original_conn
        self.assertEqual(rows[0]["kind"], "ram_only")

    def test_flushed_event_feed_remains_ram_only(self):
        self.store.event(None, "info", "persisted_batch", "batch me", None)
        self.assertEqual(self.store.flush_events(), 1)
        original_conn = self.store.conn
        self.store.conn = Mock(side_effect=AssertionError("/api/events polling touched SQLite"))
        try:
            rows = self.store.list_events(10)
        finally:
            self.store.conn = original_conn
        self.assertEqual(rows[0]["kind"], "persisted_batch")

    def test_current_provenance_event_uses_no_sql_until_batch_flush(self):
        journal = ProvenanceJournal(self.store, clock=lambda: 100.0)
        st = state("binary_sensor.pir", "on") | {
            "context": {"id": "ctx-ram", "parent_id": None, "user_id": None}
        }
        original_conn = self.store.conn
        calls = {"count": 0}

        def counting_conn():
            calls["count"] += 1
            return original_conn()

        self.store.conn = counting_conn
        try:
            event_id, inserted = journal.record_event(
                "binary_sensor.pir", st, event_time=10.0, received_time=11.0,
                source="ha_state_changed", origin="unknown",
            )
            self.assertTrue(inserted)
            self.assertFalse(journal.event_processed(event_id))
            journal.mark_event_processed(event_id, processed_time=12.0)
            self.assertTrue(journal.event_processed(event_id))
            self.assertEqual(
                journal.history_provenance("binary_sensor.pir", 10.0)["event_id"],
                event_id,
            )
            self.assertEqual(calls["count"], 0)
            self.assertEqual(journal.flush_events_batch(), 1)
            self.assertEqual(calls["count"], 1)
        finally:
            self.store.conn = original_conn


if __name__ == "__main__":
    unittest.main()
