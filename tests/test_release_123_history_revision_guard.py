"""0.14.123 continuation-cache history revision guard regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import history as history_module
from history import stateful_continuation_seed_rows_from_rows
from storage import Store


def agent():
    return {
        "id": "agent-a",
        "target_entity": "light.test",
        "target_property": "power",
        "deadband": 0.5,
    }


class EntityHistoryRevisionTests(unittest.TestCase):
    def make_store(self):
        scratch = tempfile.TemporaryDirectory(prefix="hm-history-revision-")
        self.addCleanup(scratch.cleanup)
        return Store(Path(scratch.name) / "history.db")

    @staticmethod
    def batch_row(ts, state, attrs=None, source="ha_history_full"):
        return ("light.test", float(ts), state, attrs or {}, None, source)

    def cached_manager(self, store, start_ts=100.0, end_ts=130.0):
        a = agent()
        target_map = {"light.test": [a]}
        rows = list(
            store.archive_iter(start_ts, end_ts, ["light.test"], chunk_size=100)
        )
        seeds, scanned = stateful_continuation_seed_rows_from_rows(
            rows, [a], target_map, start_ts, end_ts
        )
        fingerprint = (
            len([row for row in rows if float(row["ts"]) < float(end_ts)]),
            max(
                (
                    int(row.get("mutation_revision") or 0)
                    for row in rows
                    if float(row["ts"]) < float(end_ts)
                ),
                default=0,
            ),
        )
        manager = history_module.HistoryManager.__new__(
            history_module.HistoryManager
        )
        manager._persistent_continuation_seed_state = None
        manager._remember_persistent_continuation_seed(
            [a],
            seeds,
            scanned,
            start_ts,
            end_ts,
            history_fingerprint=fingerprint,
        )
        return manager, a, target_map, fingerprint

    def test_late_insert_inside_cached_overlap_forces_sqlite_fallback(self):
        store = self.make_store()
        store.archive_batch([
            self.batch_row(100.0, "off"),
            self.batch_row(110.0, "on"),
        ])
        manager, a, target_map, before = self.cached_manager(store)

        with patch.object(history_module, "STORE", store):
            self.assertIsNotNone(
                manager._persistent_continuation_seed_rows_for(
                    [a], target_map, 100.0, 130.0
                )
            )
            store.archive_batch([self.batch_row(120.0, "off")])
            self.assertNotEqual(
                store.archive_window_fingerprint(
                    ["light.test"], 100.0, 130.0
                ),
                before,
            )
            self.assertIsNone(
                manager._persistent_continuation_seed_rows_for(
                    [a], target_map, 100.0, 130.0
                )
            )

    def test_on_conflict_update_changes_revision_even_when_row_count_is_stable(self):
        store = self.make_store()
        store.archive_batch([
            self.batch_row(100.0, "off"),
            self.batch_row(110.0, "on"),
        ])
        manager, a, target_map, before = self.cached_manager(store)

        store.archive_batch([
            self.batch_row(110.0, "off", {"source": "late-recorder"})
        ])
        after = store.archive_window_fingerprint(
            ["light.test"], 100.0, 130.0
        )

        self.assertEqual(after[0], before[0])
        self.assertGreater(after[1], before[1])
        with patch.object(history_module, "STORE", store):
            self.assertIsNone(
                manager._persistent_continuation_seed_rows_for(
                    [a], target_map, 100.0, 130.0
                )
            )

    def test_newer_target_event_outside_overlap_does_not_invalidate_cache(self):
        store = self.make_store()
        store.archive_batch([
            self.batch_row(100.0, "off"),
            self.batch_row(110.0, "on"),
        ])
        manager, a, target_map, before = self.cached_manager(store)

        store.archive_batch([self.batch_row(140.0, "off")])
        self.assertEqual(
            store.archive_window_fingerprint(
                ["light.test"], 100.0, 130.0
            ),
            before,
        )
        with patch.object(history_module, "STORE", store):
            self.assertIsNotNone(
                manager._persistent_continuation_seed_rows_for(
                    [a], target_map, 100.0, 130.0
                )
            )

    def test_raw_sql_update_is_guarded_by_revision_trigger(self):
        store = self.make_store()
        store.archive_batch([
            self.batch_row(100.0, "off"),
            self.batch_row(110.0, "on"),
        ])
        before = store.archive_window_fingerprint(
            ["light.test"], 100.0, 130.0
        )

        with store.conn() as c:
            c.execute(
                "UPDATE entity_history SET state=? "
                "WHERE entity_id=? AND ts=?",
                ("off", "light.test", 110.0),
            )

        after = store.archive_window_fingerprint(
            ["light.test"], 100.0, 130.0
        )
        self.assertEqual(after[0], before[0])
        self.assertGreater(after[1], before[1])

    def test_raw_sql_insert_is_guarded_by_revision_trigger(self):
        store = self.make_store()
        store.archive_batch([self.batch_row(100.0, "off")])
        before = store.archive_window_fingerprint(
            ["light.test"], 100.0, 130.0
        )

        with store.conn() as c:
            c.execute(
                """INSERT INTO entity_history(
                       entity_id,ts,state,attributes_json,context_user_id,source
                   ) VALUES(?,?,?,?,?,?)""",
                ("light.test", 120.0, "on", "{}", None, "legacy"),
            )

        after = store.archive_window_fingerprint(
            ["light.test"], 100.0, 130.0
        )
        self.assertEqual(after[0], before[0] + 1)
        self.assertGreater(after[1], before[1])


class Release123SourceContractTests(unittest.TestCase):
    def test_history_cache_is_bound_to_exact_mutation_fingerprint(self):
        root = Path(__file__).resolve().parents[1]
        history_source = (
            root / "adaptive_ai" / "src" / "history.py"
        ).read_text(encoding="utf-8")
        storage_source = (
            root / "adaptive_ai" / "src" / "storage.py"
        ).read_text(encoding="utf-8")

        for marker in (
            "history_fingerprint",
            "archive_window_fingerprint",
            "continuation_capture_revision",
            'row["entity_id"] in target_map',
        ):
            self.assertIn(marker, history_source)
        for marker in (
            "mutation_revision",
            "entity_history_revision",
            "trg_entity_history_insert_revision",
            "trg_entity_history_update_revision",
            "_next_entity_history_revision",
        ):
            self.assertIn(marker, storage_source)


if __name__ == "__main__":
    unittest.main()
