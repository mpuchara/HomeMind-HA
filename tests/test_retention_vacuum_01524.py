"""SQLite freelist reuse without changing logical retention or pressure guards."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from storage import Store
from storage_budget import MIB
from storage_retention import StorageRetention
from training_snapshot import protect_training_inputs


class RetentionVacuumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict("storage.OPTIONS", storage_budget_mode="manual"):
            self.store = Store(Path(self.temp.name)/"data.db")
        self.worker = StorageRetention(self.store)
        self.budget = self.store.storage_budget

    def pages(self):
        with self.store.conn() as c:
            return tuple(int(c.execute("PRAGMA "+key).fetchone()[0])
                         for key in ("page_count", "freelist_count"))

    def populate_and_delete(self, count):
        with self.store.conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS vacuum_probe(id INTEGER PRIMARY KEY, payload TEXT)")
            c.executemany("INSERT INTO vacuum_probe(payload) VALUES(?)", [("x"*4096,)]*count)
            c.execute("DELETE FROM vacuum_probe")
        return self.pages()

    def vacuum(self, tick, free=None):
        if free is None:
            free = self.budget.reserve+1024*MIB
        with patch("storage_budget.shutil.disk_usage", return_value=SimpleNamespace(free=free)):
            with self.store.background_sqlite(), self.budget.maintenance():
                return self.worker._vacuum_once(tick)

    def test_empty_and_small_freelist_remain_available_for_reuse(self):
        self.vacuum(0)
        initial_calls = self.worker.snapshot()["vacuum_calls"]
        before, free = self.populate_and_delete(16)
        self.assertGreater(free, 0)
        self.assertLessEqual(free, self.worker.VACUUM_MIN_REUSE_PAGES)
        self.assertEqual(self.vacuum(60), 0)
        self.assertEqual(self.pages(), (before, free))
        self.assertEqual(self.worker.snapshot()["vacuum_calls"], initial_calls)
        self.assertEqual(self.worker.snapshot()["vacuum_reason"], "reuse_free_pages")
        with self.store.conn() as c:
            c.executemany("INSERT INTO vacuum_probe(payload) VALUES(?)", [("y"*4096,)]*8)
        self.assertEqual(self.pages()[0], before)

    def test_normal_excess_is_bounded_and_cooldown_leaves_pages_reusable(self):
        before, free = self.populate_and_delete(300)
        self.assertGreater(free, 100)
        reclaimed = self.vacuum(0)
        self.assertGreater(reclaimed, 0)
        self.assertLessEqual(reclaimed, self.worker.VACUUM_MAX_PAGES+1)
        first = self.worker.snapshot()
        self.assertEqual(first["vacuum_calls"], 32)
        after = self.pages()
        self.assertLess(after[0], before)
        self.assertGreaterEqual(after[1], first["vacuum_reuse_pages"])
        for tick in range(60, 900, 60):
            self.assertEqual(self.vacuum(tick), 0)
        self.assertEqual(self.pages(), after)
        self.assertEqual(self.worker.snapshot()["vacuum_calls"], 32)
        self.assertEqual(self.worker.snapshot()["vacuum_reason"], "normal_cooldown")
        self.assertGreater(self.vacuum(900), 0)
        self.assertEqual(self.worker.snapshot()["vacuum_calls"], 64)

    def test_soft_disk_pressure_can_reclaim_even_during_cooldown(self):
        self.populate_and_delete(300)
        self.assertGreater(self.vacuum(0), 0)
        self.assertGreater(self.vacuum(60, self.budget.reserve-MIB), 0)
        result = self.worker.snapshot()
        self.assertEqual(result["vacuum_reason"], "disk_reserve")
        self.assertEqual(result["vacuum_reuse_pages"], 0)
        self.assertEqual(result["vacuum_calls"], 64)

    def test_normal_active_training_defers_compaction_without_mutation(self):
        before = self.populate_and_delete(300)
        with protect_training_inputs(self.store, "agent-a"):
            self.assertEqual(self.vacuum(0), 0)
            self.assertEqual(self.worker.snapshot()["vacuum_reason"], "active_training")
            self.assertEqual(self.pages(), before)
        self.assertGreater(self.vacuum(60), 0)

    def test_wal_and_unknown_pressure_do_not_add_compaction_writes(self):
        before = self.populate_and_delete(300)
        sampled = dict(level="paused", reason="wal_budget",
                       free_bytes=self.budget.reserve+MIB, sample_error=None,
                       wal_bytes=self.budget.wal_limit)
        with patch.object(self.budget, "sample", return_value=sampled):
            self.assertEqual(self.worker._vacuum_once(0), 0)
        self.assertEqual(self.worker.snapshot()["vacuum_reason"], "waiting_for_checkpoint")
        with patch("storage_budget.shutil.disk_usage", side_effect=OSError("unavailable")):
            self.assertEqual(self.worker._vacuum_once(60), 0)
        self.assertEqual(self.worker.snapshot()["vacuum_reason"], "storage_sample_unavailable")
        self.assertEqual(self.pages(), before)
        self.assertEqual(self.worker.snapshot()["vacuum_calls"], 0)

    def test_snapshot_is_ram_only(self):
        with patch.object(self.store, "conn", side_effect=AssertionError("SQL")), \
             patch.object(self.budget, "sample", side_effect=AssertionError("disk")):
            status = self.worker.snapshot()
        self.assertEqual(status["vacuum_calls"], 0)
        self.assertEqual(status["vacuum_normal_interval_seconds"], 900.)
