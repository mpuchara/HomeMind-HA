"""0.14.46 regressions for RAM-first operational hot reads."""
import inspect
import tempfile
import unittest
from pathlib import Path

import storage
import agent_candidates
import agent_candidate_shadow_runtime as shadow_runtime
import agent_candidate_card_summary as card_summary


class Release046RamHotPathTests(unittest.TestCase):
    def test_candidate_membership_is_ram_cached_after_warmup(self):
        with tempfile.TemporaryDirectory() as td:
            store = storage.Store(Path(td) / "ram-cache.db")
            agent_candidates.ensure_tables(store)
            agent_candidates.refresh_candidate_ids_cache(store)
            original = store.conn
            store.conn = lambda: (_ for _ in ()).throw(
                AssertionError("candidate-id hot read touched SQLite")
            )
            try:
                self.assertEqual(agent_candidates._candidate_ids(store), set())
                self.assertFalse(agent_candidates.is_candidate(store, "missing"))
            finally:
                store.conn = original

    def test_candidate_live_endpoint_prefers_runtime_ram_snapshot(self):
        class Manager:
            def candidate_live_runtime_snapshots(self):
                return [{
                    "generation_id": "g1",
                    "candidate_desired": 1.0,
                    "read_source": "ram_candidate_runtime",
                }]
        rows = card_summary.live_candidate_snapshots(Manager())
        self.assertEqual(rows[0]["read_source"], "ram_candidate_runtime")
        self.assertEqual(rows[0]["candidate_desired"], 1.0)

    def test_shadow_runtime_keeps_latest_generation_and_active_edge_in_ram(self):
        source = inspect.getsource(shadow_runtime.install)
        self.assertIn("latest_generation_runtime = {}", source)
        self.assertIn("active_edge_cache = {}", source)
        self.assertIn("candidate_live_runtime_snapshots", source)
        self.assertIn("ram_candidate_runtime", source)
        self.assertIn("_cached_active_edge", source)
        self.assertIn("latest_generation_runtime[str(gid)]", source)

    def test_passive_root_prefers_engine_config_ram(self):
        source = inspect.getsource(shadow_runtime.install)
        helper = source.split("def _root_config", 1)[1].split(
            "def _rebuild_candidate_dependency_index", 1
        )[0]
        self.assertIn("all_agent_configs", helper)
        self.assertIn("manager.store.get_agent_config(root_id)", helper)
        passive = source.split("def _observe_passive_root", 1)[1].split(
            "def drain_candidate_shadow_events", 1
        )[0]
        self.assertIn("root = _root_config(root_id)", passive)

    def test_live_candidate_poll_has_early_ram_return(self):
        source = inspect.getsource(card_summary.live_candidate_snapshots)
        hot_path = source.split("if callable(hot):", 1)[1].split("now = time.time()", 1)[0]
        self.assertIn("return list(hot() or [])", hot_path)
        self.assertNotIn("manager.store.conn", hot_path)


if __name__ == "__main__":
    unittest.main()
