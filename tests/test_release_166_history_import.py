"""Bounded Recorder writes preserve data, revisions and resumable durability."""
import random
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT
import history
from storage import Store
from tools.benchmark_history_import import canonical, legacy_rows, manager, payload


class HistoryImport166Tests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="hm-import-test-")
        self.addCleanup(scratch.cleanup)
        self.store = Store(Path(scratch.name) / "archive.db")
        self.old = Store(Path(scratch.name) / "reference.db")
        self.manager = manager()
        self.patchers = [patch.object(history, "STORE", self.store),
                         patch.dict(history.OPTIONS, history_context_import_interval_seconds=60),
                         patch.object(history.TRAINING_BUDGET, "checkpoint", return_value=0)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def compare(self, data, source, *, retried=False):
        expected = legacy_rows(data, source)
        self.old.archive_batch(expected)
        self.assertEqual(self.manager._archive_history_payload(data, source), len(expected))
        actual, reference = canonical(self.store), canonical(self.old)
        if retried:
            # SQLite AUTOINCREMENT consumes IDs for conflicting retry inserts too.
            # Verify every persisted content field; IDs need not match a clean import.
            actual = [{k: v for k, v in r.items() if k != "id"} for r in actual]
            reference = [{k: v for k, v in r.items() if k != "id"} for r in reference]
        self.assertEqual(actual, reference)

    def test_large_full_response_preserves_every_row_and_bounds_each_write(self):
        sizes = []
        original = self.store.archive_batch
        def record(rows):
            sizes.append(len(rows))
            return original(rows)
        with patch.object(self.store, "archive_batch", record):
            self.compare(payload(301), "ha_history_full")
        self.assertEqual(sum(sizes), 1204)
        self.assertEqual(max(sizes), 128)
        self.assertGreater(len(sizes), 1)

    def test_minimal_numeric_final_state_is_retained_at_batch_boundary(self):
        with patch.object(history, "HISTORY_IMPORT_BATCH_ROWS", 2):
            self.compare(payload(122), "ha_history_minimal")
        sensor = list(self.store.archive_iter(entity_ids=["sensor.room_0"]))
        self.assertEqual([r["state"] for r in sensor], ["0", "60", "20", "21"])

    def test_random_mixed_missing_invalid_and_duplicate_rows_match_legacy(self):
        rng = random.Random(166)
        data = payload(175)
        for group in data:
            group.extend([{}, {"entity_id": group[0]["entity_id"], "last_changed": "invalid"}])
            for i, row in enumerate(group):
                if rng.random() < .15:
                    row.pop("entity_id", None)
                if "last_changed" in row and rng.random() < .15:
                    row["last_updated"] = row.pop("last_changed")
                if "state" in row and rng.random() < .3:
                    row["state"] = rng.choice([None, "unknown", "on", "12.5"])
                if i % 17 == 0:
                    row["context"] = {"user_id": "human"}
            group.append(dict(group[20], state="off", attributes={"corrected": True}))
        data.insert(1, [])
        with patch.object(history, "HISTORY_IMPORT_BATCH_ROWS", 7):
            self.compare(data, "ha_history_minimal")

    def test_conflicts_preserve_live_receive_time_attributes_user_and_source(self):
        live = ("sensor.room_0", 1700000000., "on", {"live": True}, "human", "live", 1700000000.5)
        for store in (self.store, self.old):
            store.archive_batch([live])
        data = payload(3)
        data[0][0]["attributes"] = {}
        with patch.object(history, "HISTORY_IMPORT_BATCH_ROWS", 2):
            self.compare(data, "ha_history_full")
        row = list(self.store.archive_iter(entity_ids=["sensor.room_0"]))[0]
        self.assertEqual(row["received_ts"], 1700000000.5)
        self.assertEqual(row["context_user_id"], "human")
        self.assertEqual(row["attributes_json"], '{"live":true}')
        self.assertEqual(row["source"], "ha_history_full")

    def test_pause_occurs_after_commit_without_store_lock_or_pinned_reader(self):
        calls = []
        def checkpoint(label, **kwargs):
            if label != "history_import_committed_batch":
                return 0
            result = []
            def other_writer():
                c = None
                try:
                    with self.store.lock:
                        c = sqlite3.connect(self.store.path, timeout=.1)
                        with c:
                            count = c.execute("SELECT COUNT(*) FROM entity_history").fetchone()[0]
                            c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('other-writer','ok')")
                        busy, frames, copied = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                        result.append((count, busy, frames - copied))
                except BaseException as exc:
                    result.append(str(exc))
                finally:
                    if c is not None:
                        c.close()
            witness = threading.Thread(target=other_writer)
            witness.start()
            witness.join(2)
            self.assertFalse(witness.is_alive(), "import must release lock before checkpoint")
            self.assertEqual(result, [(128 * (len(calls) + 1), 0, 0)])
            self.assertEqual(kwargs, {"force": True})
            calls.append(result[0][0])
        with patch.object(history.TRAINING_BUDGET, "checkpoint", side_effect=checkpoint):
            self.manager._archive_history_payload(payload(64), "ha_history_full")
        self.assertEqual(calls, [128, 256])

    def test_cancellation_after_commit_keeps_prefix_and_retry_completes_all_rows(self):
        def checkpoint(label, **kwargs):
            if label == "history_import_committed_batch":
                self.manager.job_cancel_event.set()
        with patch.object(history.TRAINING_BUDGET, "checkpoint", side_effect=checkpoint):
            with self.assertRaises(InterruptedError):
                self.manager._archive_history_payload(payload(100), "ha_history_full")
        self.assertEqual(self.store.archive_count(), 128)
        self.manager.job_cancel_event.clear()
        self.compare(payload(100), "ha_history_full", retried=True)

    def test_failed_later_batch_rolls_back_only_that_batch_then_retry_is_complete(self):
        original = self.store.archive_batch
        calls = []
        def fail_second(rows):
            calls.append(len(rows))
            if len(calls) == 2:
                with self.store.conn() as c:
                    c.execute("UPDATE entity_history SET state='bad'")
                    raise sqlite3.OperationalError("injected batch failure")
            return original(rows)
        with patch.object(self.store, "archive_batch", side_effect=fail_second):
            with self.assertRaises(sqlite3.OperationalError):
                self.manager._archive_history_payload(payload(100), "ha_history_full")
        self.assertEqual(self.store.archive_count(), 128)
        self.assertNotIn("bad", [r["state"] for r in self.store.archive_iter()])
        self.compare(payload(100), "ha_history_full", retried=True)

    def test_all_commits_receive_durable_revisions_and_late_update_invalidates_overlap(self):
        self.manager._archive_history_payload(payload(100), "ha_history_full")
        before = self.store.archive_window_fingerprint(["sensor.room_0"], 1700000000, 1700000100)
        data = [[dict(payload(1)[0][0], state="changed")]]
        self.manager._archive_history_payload(data, "ha_history_full")
        after = self.store.archive_window_fingerprint(["sensor.room_0"], 1700000000, 1700000100)
        self.assertEqual(before[0], after[0])
        self.assertGreater(after[1], before[1])
        self.assertTrue(all(r["mutation_revision"] > 0 for r in self.store.archive_iter()))

    def test_empty_response_does_not_write_or_force_pause_and_stop_prevents_first_write(self):
        with patch.object(self.store, "archive_batch") as archive:
            self.assertEqual(self.manager._archive_history_payload([[], None], "ha_history_full"), 0)
            archive.assert_not_called()
            self.manager.stop_event.set()
            with self.assertRaises(InterruptedError):
                self.manager._archive_history_payload(payload(1), "ha_history_full")
            archive.assert_not_called()

    def test_trace_reports_only_successfully_committed_rows_on_partial_failure(self):
        def checkpoint(label, **kwargs):
            if label == "history_import_committed_batch":
                self.manager.job_cancel_event.set()
        with patch.object(history.RUNTIME_DEBUG, "begin", return_value="trace"), \
                patch.object(history.RUNTIME_DEBUG, "end") as end, \
                patch.object(history.TRAINING_BUDGET, "checkpoint", side_effect=checkpoint):
            with self.assertRaises(InterruptedError):
                self.manager._archive_history_payload(payload(100), "ha_history_full")
        calls = [call for call in end.call_args_list if call.args == ("trace",)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs, dict(status="error", rows=128, batches=1,
                                              max_batch_rows=128, error_type="InterruptedError"))

    def test_import_reuses_one_connection_and_closes_it_even_when_cancelled(self):
        original = sqlite3.connect
        opened = []
        def connect(*args, **kwargs):
            c = original(*args, **kwargs)
            opened.append(c)
            return c
        with patch("storage.sqlite3.connect", side_effect=connect):
            self.manager._archive_history_payload(payload(100), "ha_history_full")
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        opened.clear()
        def checkpoint(label, **kwargs):
            if label == "history_import_committed_batch":
                self.manager.job_cancel_event.set()
        with patch("storage.sqlite3.connect", side_effect=connect), \
                patch.object(history.TRAINING_BUDGET, "checkpoint", side_effect=checkpoint):
            with self.assertRaises(InterruptedError):
                self.manager._archive_history_payload(payload(100), "ha_history_full")
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
