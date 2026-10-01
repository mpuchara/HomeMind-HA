"""0.14.121 persistent replay-session transport regressions."""
import unittest
from pathlib import Path

import history as history_module
from training_process import aggregate_training_sequence_profile


class PersistentTransportCacheTests(unittest.TestCase):
    def test_experience_ids_load_once_per_agent_and_keep_same_set(self):
        manager = history_module.HistoryManager.__new__(history_module.HistoryManager)
        manager._persistent_experience_ids = {}
        calls = []

        def loader(agent_id):
            calls.append(str(agent_id))
            return {11, 12}

        first, first_reused = manager._persistent_experience_ids_for("agent-a", loader)
        first.add(13)
        second, second_reused = manager._persistent_experience_ids_for("agent-a", loader)

        self.assertFalse(first_reused)
        self.assertTrue(second_reused)
        self.assertIs(first, second)
        self.assertEqual(second, {11, 12, 13})
        self.assertEqual(calls, ["agent-a"])

    def test_provenance_window_loads_only_uncovered_tail(self):
        manager = history_module.HistoryManager.__new__(history_module.HistoryManager)
        manager._persistent_replay_provenance_state = None
        calls = []

        def loader(start_ts, end_ts, entity_ids):
            calls.append((float(start_ts), float(end_ts), tuple(entity_ids)))
            marker = int(end_ts)
            return {
                marker: {
                    "event_id": f"event-{marker}",
                    "origin": "user",
                    "source": "test",
                }
            }

        first, first_diag = manager._persistent_replay_provenance_for(
            loader, 0.0, 6.0, ["light.a"]
        )
        second, second_diag = manager._persistent_replay_provenance_for(
            loader, 3.0, 12.0, ["light.a"]
        )
        third, third_diag = manager._persistent_replay_provenance_for(
            loader, 9.0, 11.0, ["light.a"]
        )

        self.assertEqual(
            calls,
            [
                (0.0, 6.0, ("light.a",)),
                (6.0, 12.0, ("light.a",)),
            ],
        )
        self.assertFalse(first_diag["reused"])
        self.assertTrue(second_diag["reused"])
        self.assertTrue(third_diag["reused"])
        self.assertEqual(second_diag["loaded_rows"], 1)
        self.assertEqual(third_diag["db_loads"], 0)
        self.assertEqual(set(first) | set(second) | set(third), {6, 12})
        self.assertEqual(third_diag["coverage_start_ts"], 0.0)
        self.assertEqual(third_diag["coverage_end_ts"], 12.0)

    def test_provenance_scope_change_or_backwards_range_forces_exact_reload(self):
        manager = history_module.HistoryManager.__new__(history_module.HistoryManager)
        manager._persistent_replay_provenance_state = None
        calls = []

        def loader(start_ts, end_ts, entity_ids):
            calls.append((float(start_ts), float(end_ts), tuple(entity_ids)))
            return {}

        manager._persistent_replay_provenance_for(
            loader, 10.0, 20.0, ["light.a"]
        )
        manager._persistent_replay_provenance_for(
            loader, 5.0, 15.0, ["light.a"]
        )
        manager._persistent_replay_provenance_for(
            loader, 5.0, 15.0, ["switch.b"]
        )

        self.assertEqual(
            calls,
            [
                (10.0, 20.0, ("light.a",)),
                (5.0, 15.0, ("light.a",)),
                (5.0, 15.0, ("switch.b",)),
            ],
        )

    def test_close_releases_new_session_transport_caches(self):
        manager = history_module.HistoryManager.__new__(history_module.HistoryManager)
        manager._persistent_replay_sqlite_connection = None
        manager._persistent_replay_query_cache = object()
        manager._persistent_home_context_cache = object()
        manager._persistent_ram_replay_index = object()
        manager._persistent_transition_edge_index = object()
        manager._persistent_feature_snapshot_cache = object()
        manager._persistent_experience_ids = {"agent-a": {1, 2}}
        manager._persistent_replay_provenance_state = {"rows": {1: {}}}

        manager.close_persistent_training_resources()

        self.assertEqual(manager._persistent_experience_ids, {})
        self.assertIsNone(manager._persistent_replay_provenance_state)


class PersistentTransportProfileTests(unittest.TestCase):
    def test_sequence_profile_aggregates_transport_and_finalization_counters(self):
        reports = [
            {
                "elapsed_seconds": 2.0,
                "training_phase_timings": {
                    "experience_dedup_load_seconds": 0.20,
                    "experience_dedup_db_loads": 1,
                    "experience_dedup_cache_hits": 0,
                    "experience_dedup_cached_ids": 10,
                    "provenance_load_seconds": 0.30,
                    "provenance_db_loads": 1,
                    "provenance_loaded_rows": 8,
                    "tracker_init_seconds": 0.10,
                    "tiny_mlp_finalization_seconds": 0.40,
                },
            },
            {
                "elapsed_seconds": 1.0,
                "training_phase_timings": {
                    "experience_dedup_load_seconds": 0.01,
                    "experience_dedup_db_loads": 0,
                    "experience_dedup_cache_hits": 1,
                    "experience_dedup_cached_ids": 14,
                    "provenance_load_seconds": 0.05,
                    "provenance_db_loads": 1,
                    "provenance_loaded_rows": 3,
                    "tracker_init_seconds": 0.08,
                    "tiny_mlp_finalization_seconds": 0.20,
                },
            },
        ]

        profile = aggregate_training_sequence_profile(reports)

        self.assertAlmostEqual(
            profile["phase_seconds"]["experience_dedup_load_seconds"], 0.21
        )
        self.assertAlmostEqual(
            profile["phase_seconds"]["provenance_load_seconds"], 0.35
        )
        self.assertEqual(profile["counters"]["experience_dedup_db_loads"], 1)
        self.assertEqual(profile["counters"]["experience_dedup_cache_hits"], 1)
        self.assertEqual(profile["counters"]["provenance_db_loads"], 2)
        self.assertEqual(profile["counters"]["provenance_loaded_rows"], 11)


class Release121SourceContractTests(unittest.TestCase):
    def test_release_contains_persistent_transport_and_granular_timers(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "adaptive_ai" / "src" / "history.py").read_text(
            encoding="utf-8"
        )
        for marker in (
            "_persistent_experience_ids",
            "_persistent_replay_provenance_state",
            "experience_dedup_load_seconds",
            "provenance_load_seconds",
            "tracker_init_seconds",
            "tiny_mlp_finalization_seconds",
            "heldout_fold_seconds",
            "policy_serialize_persist_seconds",
            "benchmark_persist_seconds",
            "qualification_seconds",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
