"""0.14.120 persistent training-session cache/profiling regressions."""
import unittest
from pathlib import Path

import history as history_module
from settings import DEFAULT_OPTIONS
from training_process import (
    aggregate_training_sequence_profile,
    training_options_fingerprint,
)


class TrainingSequenceProfileTests(unittest.TestCase):
    def test_profile_aggregates_chunk_timings_without_claiming_exclusive_cpu(self):
        reports = [
            {
                "index": 0,
                "elapsed_seconds": 10.0,
                "training_phase_timings": {
                    "screening_seconds": 1.5,
                    "replay_seconds": 7.0,
                    "finalization_seconds": 1.0,
                    "total_seconds": 9.8,
                    "feature_snapshot_build_seconds": 2.0,
                    "feature_snapshot_builds": 100,
                    "replay_driver_rows_processed": 20,
                },
                "training_feature_snapshot_cache": {
                    "session_persistent": True,
                    "entries": 10,
                    "units": 100,
                    "hits": 5,
                    "misses": 100,
                    "builds": 100,
                    "evictions": 0,
                    "build_seconds": 2.0,
                    "hit_rate": 5 / 105,
                },
            },
            {
                "index": 1,
                "elapsed_seconds": 8.0,
                "training_phase_timings": {
                    "screening_seconds": 0.0,
                    "replay_seconds": 6.0,
                    "finalization_seconds": 1.0,
                    "total_seconds": 7.8,
                    "feature_snapshot_build_seconds": 1.0,
                    "feature_snapshot_builds": 40,
                    "feature_snapshot_cache_hits": 25,
                    "replay_driver_rows_processed": 15,
                },
                "training_feature_snapshot_cache": {
                    "session_persistent": True,
                    "entries": 20,
                    "units": 180,
                    "hits": 30,
                    "misses": 140,
                    "builds": 140,
                    "evictions": 1,
                    "build_seconds": 3.0,
                    "hit_rate": 30 / 170,
                },
            },
        ]
        profile = aggregate_training_sequence_profile(reports)
        self.assertEqual(profile["contract"], "persistent_training_session_profile_v1")
        self.assertEqual(profile["chunks"], 2)
        self.assertEqual(profile["chunk_elapsed_seconds"], 18.0)
        self.assertEqual(profile["phase_seconds"]["replay_seconds"], 13.0)
        self.assertEqual(profile["phase_seconds"]["feature_snapshot_build_seconds"], 3.0)
        self.assertEqual(profile["counters"]["feature_snapshot_builds"], 140)
        self.assertEqual(profile["counters"]["replay_driver_rows_processed"], 35)
        self.assertEqual(profile["feature_snapshot_session"]["hits"], 30)
        self.assertTrue(profile["feature_snapshot_session"]["persistent"])
        self.assertIn("nested", profile["note"])
        self.assertEqual(profile["slowest_chunks"][0]["index"], 0)

    def test_empty_profile_is_bounded_and_well_formed(self):
        profile = aggregate_training_sequence_profile([])
        self.assertEqual(profile["chunks"], 0)
        self.assertEqual(profile["chunk_elapsed_seconds"], 0.0)
        self.assertEqual(profile["slowest_chunks"], [])
        self.assertEqual(profile["feature_snapshot_session"]["hits"], 0)


class PersistentFeatureCacheLifecycleTests(unittest.TestCase):
    def test_close_releases_session_feature_cache(self):
        manager = history_module.HistoryManager.__new__(history_module.HistoryManager)
        manager._persistent_replay_sqlite_connection = None
        manager._persistent_replay_query_cache = object()
        manager._persistent_home_context_cache = object()
        manager._persistent_ram_replay_index = object()
        manager._persistent_transition_edge_index = object()
        manager._persistent_feature_snapshot_cache = object()
        manager.close_persistent_training_resources()
        self.assertIsNone(manager._persistent_feature_snapshot_cache)
        self.assertIsNone(manager._persistent_replay_query_cache)
        self.assertIsNone(manager._persistent_home_context_cache)
        self.assertIsNone(manager._persistent_ram_replay_index)
        self.assertIsNone(manager._persistent_transition_edge_index)


class Release120SourceContractTests(unittest.TestCase):
    def test_faster_batches_remain_nonsemantic_scheduler_knobs(self):
        self.assertEqual(DEFAULT_OPTIONS["training_archive_batch_rows"], 64)
        self.assertEqual(DEFAULT_OPTIONS["training_experience_batch_rows"], 256)
        changed = dict(DEFAULT_OPTIONS)
        changed["training_archive_batch_rows"] = 8
        changed["training_experience_batch_rows"] = 8
        self.assertEqual(
            training_options_fingerprint(DEFAULT_OPTIONS),
            training_options_fingerprint(changed),
        )

    def test_addon_defaults_match_runtime_defaults(self):
        root = Path(__file__).resolve().parents[1]
        config = (root / "adaptive_ai" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("training_archive_batch_rows: 64", config)
        self.assertIn("training_experience_batch_rows: 256", config)

    def test_history_keeps_feature_cache_for_persistent_worker_sequence(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "adaptive_ai" / "src" / "history.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_persistent_feature_snapshot_cache", source)
        self.assertIn("feature_snapshot_cache_reused = False", source)
        self.assertIn("feature_snapshot_cache_diagnostics", source)
        self.assertIn("full_archive_count_skipped", source)

    def test_low_power_runtime_migrates_shipped_batch_defaults(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "adaptive_ai" / "src" / "rpi_low_power_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn("DEFAULT_ARCHIVE_BATCH_ROWS = 64", source)
        self.assertIn("DEFAULT_EXPERIENCE_BATCH_ROWS = 256", source)
        self.assertIn("current_archive_batch == 16", source)
        self.assertIn("current_experience_batch in (64, 128)", source)


if __name__ == "__main__":
    unittest.main()
