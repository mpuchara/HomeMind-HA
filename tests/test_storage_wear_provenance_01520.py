"""Regression tests for durable provenance no-op suppression."""
import tempfile
import unittest
from pathlib import Path

import support
from provenance import ProvenanceJournal
from storage import Store


class ProvenanceWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.journal = ProvenanceJournal(self.store, clock=lambda: 1000.0)
        with self.store.conn() as c:
            c.execute("CREATE TABLE write_probe(kind TEXT)")
            for table in ("provenance_events", "provenance_history_links"):
                c.execute(f"""CREATE TRIGGER probe_{table} AFTER UPDATE ON {table}
                            BEGIN INSERT INTO write_probe VALUES('{table}'); END""")

    def forget_cached_events(self):
        # Simulate bounded cache eviction: the persisted row remains, and the
        # next delivery must exercise the real SQL conflict path.
        with self.journal._event_cache_lock:
            self.assertEqual(self.journal.pending_event_count(), 0)
            self.journal._event_cache.clear()
            self.journal._history_event_cache.clear()

    def record(self, event_id="event-a", received=11.0, origin="unknown", state=None, processed=None):
        key, accepted = self.journal.record_event(
            "sensor.test", state, event_time=10.0, received_time=received,
            origin=origin, event_id=event_id)
        self.assertTrue(accepted)
        self.assertEqual(key, event_id)
        if processed is not None:
            self.journal.mark_event_processed(key, processed_time=processed)
        self.assertEqual(self.journal.flush_events_batch(), 1)

    def durable(self, event_id="event-a"):
        with self.store.conn() as c:
            return dict(c.execute("SELECT * FROM provenance_events WHERE event_id=?", (event_id,)).fetchone())

    def updates(self):
        with self.store.conn() as c:
            return dict(c.execute("SELECT kind,COUNT(*) FROM write_probe GROUP BY kind").fetchall())

    def test_duplicate_replay_skips_updates_without_changing_accepted_count(self):
        self.record(origin="user", state={"state": "on"}, processed=12.0)
        before = self.durable()
        self.forget_cached_events()
        self.record(received=15.0, origin="unknown", state=None, processed=16.0)
        self.assertEqual(self.durable(), before)
        self.assertEqual(self.updates(), {})

    def test_new_evidence_merges_and_history_link_replacement_is_not_dropped(self):
        self.record()
        self.forget_cached_events()
        self.record(received=9.0, origin="own_command", state={"state": "on"}, processed=12.0)
        row = self.durable()
        self.assertEqual(row["origin"], "own_command")
        self.assertEqual(row["received_time"], 9.0)
        self.assertIn('"on"', row["state_json"])
        self.assertEqual(row["processed_time"], 12.0)
        self.assertEqual(self.updates(), {"provenance_events": 1})
        # A different event at the same entity/time must still replace the link.
        self.record(event_id="event-b", origin="user", state={"state": "off"})
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT event_id FROM provenance_history_links").fetchone()[0], "event-b")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM provenance_events").fetchone()[0], 2)
        self.assertEqual(self.updates(), {"provenance_events": 1, "provenance_history_links": 1})
        self.forget_cached_events()
        self.record(received=14.0, origin="user", state={"state": "off"}, processed=20.0)
        self.assertEqual(self.durable(), row)
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT event_id FROM provenance_history_links").fetchone()[0], "event-a")
        self.assertEqual(self.updates(), {"provenance_events": 1, "provenance_history_links": 2})

    def test_processed_time_durable_fallback_keeps_first_value(self):
        self.record()
        self.forget_cached_events()
        self.journal.mark_event_processed("event-a", processed_time=12.0)
        self.journal.mark_event_processed("event-a", processed_time=13.0)
        self.assertEqual(self.durable()["processed_time"], 12.0)
        self.assertEqual(self.updates(), {"provenance_events": 1})


if __name__ == "__main__":
    unittest.main()
