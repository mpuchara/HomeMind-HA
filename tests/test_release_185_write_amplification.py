"""Storage no-op and evidence-window parity regressions."""
import tempfile
import unittest
from pathlib import Path

import support
from storage import Store
from observation_contract import FeatureJournal


class WriteAmplificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")

    def watch_updates(self, table):
        with self.store.conn() as c:
            c.execute("CREATE TABLE write_probe(n INTEGER)")
            c.execute(f"""CREATE TRIGGER write_probe_update AFTER UPDATE ON {table}
                          BEGIN INSERT INTO write_probe VALUES(1); END""")

    def updates(self):
        with self.store.conn() as c:
            return c.execute("SELECT COUNT(*) FROM write_probe").fetchone()[0]

    def history_row(self):
        with self.store.conn() as c:
            return dict(c.execute("SELECT * FROM entity_history").fetchone())

    def test_archive_noop_preserves_payload_and_window_revision(self):
        row = ("sensor.test", 100.0, "10", {"unit": "%"}, "user", "live", 101.0)
        self.assertEqual(self.store.archive_batch([row]), 1)
        self.watch_updates("entity_history")
        before = self.history_row()
        stamp = self.store.archive_window_fingerprint(["sensor.test"], 90, 110)
        self.assertEqual(self.store.archive_batch([row] * 64), 64)
        self.store.archive_upsert("sensor.test", 100.0, "10")
        self.assertEqual(self.history_row(), before)
        self.assertEqual(self.updates(), 0)
        self.assertEqual(self.store.archive_window_fingerprint(["sensor.test"], 90, 110), stamp)
        self.store.archive_batch([("sensor.test", 100.0, "11", {}, None, "ha_history_full", 105.0)])
        after = self.history_row()
        self.assertEqual(after["state"], "11")
        self.assertEqual(after["attributes_json"], before["attributes_json"])
        self.assertEqual(after["context_user_id"], "user")
        self.assertEqual(after["received_ts"], 101.0)
        self.assertEqual(after["source"], "ha_history_full")
        self.assertGreater(after["mutation_revision"], before["mutation_revision"])
        self.assertNotEqual(self.store.archive_window_fingerprint(["sensor.test"], 90, 110), stamp)

    def test_archive_receipt_only_and_nullable_state_changes_are_not_dropped(self):
        self.store.archive_batch([("sensor.test", 100.0, None, {}, None, "ha_history_full")])
        self.watch_updates("entity_history")
        before = self.history_row()
        self.store.archive_batch([("sensor.test", 100.0, None, {}, None, "live", 104.0)])
        self.assertEqual(self.history_row()["received_ts"], 104.0)
        self.assertGreater(self.history_row()["mutation_revision"], before["mutation_revision"])
        self.store.archive_upsert("sensor.test", 100.0, "on")
        self.assertEqual(self.history_row()["state"], "on")
        self.store.archive_upsert("sensor.test", 100.0, None)
        self.assertIsNone(self.history_row()["state"])
        self.assertEqual(self.updates(), 3)

    @staticmethod
    def sample(key="event", received=101.0, quality=1.0, entity="sensor.test"):
        return dict(event_key=key, entity_id=entity, event_time=100.0,
                    received_time=received, state="10", attributes_json="{}",
                    source="ha_state_changed", quality=quality)

    def test_feature_duplicate_and_real_merge(self):
        journal = FeatureJournal(self.store, clock=lambda: 110.0)
        self.assertEqual(journal.record_batch([self.sample(quality=.5)]), ["event"])
        self.watch_updates("feature_observation_events")
        self.assertEqual(journal.record_batch([self.sample(received=102.0, quality=.4)]), ["event"])
        self.assertEqual(self.updates(), 0)
        journal.record_batch([self.sample(received=99.0, quality=.8)])
        with self.store.conn() as c:
            row = c.execute("SELECT received_time,quality FROM feature_observation_events").fetchone()
        self.assertEqual(tuple(row), (99.0, .8))
        self.assertEqual(self.updates(), 1)

    def test_overlapping_windows_keep_all_links_and_update_each_sample_once(self):
        ticks = iter((110.0, 111.0, 112.0, 112.0))
        journal = FeatureJournal(self.store, clock=lambda: next(ticks), max_windows_per_agent=20)
        journal.record_batch([self.sample(), self.sample("other", entity="sensor.other")])
        self.watch_updates("feature_observation_events")
        windows = [
            dict(window_id="w1", agent_id="a", entities=["sensor.test"], anchor_time=100.0),
            dict(window_id="w2", agent_id="a", entities=["sensor.test", "sensor.other"], anchor_time=100.0),
            dict(window_id="w3", agent_id="a", entities=["sensor.test"], anchor_time=100.0),
        ]
        self.assertEqual(journal.open_windows_batch(windows), 3)
        self.assertEqual(self.updates(), 2)
        retention = journal.window_retention_days * 86400.0
        with self.store.conn() as c:
            actual = dict(c.execute("SELECT entity_id,protected_until FROM feature_observation_events").fetchall())
            self.assertEqual(actual, {"sensor.test": 112.0 + retention, "sensor.other": 111.0 + retention})
            self.assertEqual([r[0] for r in c.execute("SELECT window_id FROM feature_windows ORDER BY rowid")], ["w1", "w2", "w3"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM feature_window_entities").fetchone()[0], 4)
        self.assertEqual(journal.open_windows_batch([windows[-1]]), 1)
        self.assertEqual(self.updates(), 2)


if __name__ == "__main__":
    unittest.main()
