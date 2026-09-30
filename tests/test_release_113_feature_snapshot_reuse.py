"""0.14.113 exact historical feature snapshot reuse regressions."""
from array import array
import tempfile
import unittest
from pathlib import Path

from context_engine import ContextEngine
from replay import (
    HistoricalContextCache,
    HistoricalFeatureSnapshot,
    HistoricalFeatureSnapshotCache,
    SQLiteTemporalTracker,
)
from settings import DEFAULT_OPTIONS
from storage import Store
from training_process import (
    resolve_training_resource_profile,
    training_options_fingerprint,
)


class FeatureSnapshotCacheTests(unittest.TestCase):
    def test_snapshot_restores_private_mutable_copies(self):
        snapshot = HistoricalFeatureSnapshot.capture(
            {0: 1.0, 7: -0.25},
            {
                "home_forecast": {
                    "known": True,
                    "occupancy_now": 0.75,
                    "arrival_probability": 0.2,
                },
                "home_known": True,
            },
            {
                "feature_ids": ("a", "b", "c"),
                "values": array("f", [1.0, 2.0, 3.0]),
            },
        )
        first_features, first_meta, first_neural = snapshot.restore()
        first_features[7] = 999.0
        first_meta["home_forecast"]["occupancy_now"] = 999.0
        first_neural["values"][0] = 999.0

        features, meta, neural = snapshot.restore()
        self.assertEqual(features, {0: 1.0, 7: -0.25})
        self.assertEqual(meta["home_forecast"]["occupancy_now"], 0.75)
        self.assertEqual(list(neural["values"]), [1.0, 2.0, 3.0])
        self.assertEqual(neural["feature_ids"], ("a", "b", "c"))

    def test_unique_timestamp_diagnostic_is_bounded(self):
        cache = HistoricalFeatureSnapshotCache(max_entries=1, max_units=1000)
        snap = HistoricalFeatureSnapshot.capture(
            {0: 1.0}, {"home_forecast": {}}, None
        )
        for idx in range(200):
            cache.put(("k", idx), snap, timestamp=float(idx))
        status = cache.status()
        self.assertLessEqual(
            status["unique_feature_timestamps"],
            status["unique_feature_timestamp_cap"],
        )
        self.assertTrue(status["unique_feature_timestamps_saturated"])
        self.assertGreater(status["unique_feature_timestamp_overflow"], 0)

    def test_lru_is_bounded_and_reports_duplicate_hits(self):
        cache = HistoricalFeatureSnapshotCache(max_entries=2, max_units=1000)
        snap = HistoricalFeatureSnapshot.capture(
            {0: 1.0}, {"home_forecast": {}}, None
        )
        cache.put(("a", 1), snap, timestamp=1.0)
        self.assertIsNotNone(cache.get(("a", 1)))
        cache.put(("b", 2), snap, timestamp=2.0)
        cache.put(("c", 3), snap, timestamp=3.0)

        self.assertIsNone(cache.get(("a", 1)))
        status = cache.status()
        self.assertLessEqual(status["entries"], 2)
        self.assertGreaterEqual(status["evictions"], 1)
        self.assertGreaterEqual(status["duplicate_hits"], 1)
        self.assertEqual(status["contract"], "historical_feature_snapshot_cache_v1")


class FeatureSnapshotRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hm-feature-snapshot-")
        self.store = Store(Path(self.tmp.name) / "adaptive_ai.db")
        self.sensor = "binary_sensor.motion"
        self.target = "light.target"
        self.store.archive_batch([
            (
                self.sensor, 0.0, "off", {"device_class": "motion"},
                None, "test", 0.0,
            ),
            (
                self.sensor, 10.0, "on", {"device_class": "motion"},
                None, "test", 10.0,
            ),
            # Happened at t=12 but is not causally visible until local receive t=35.
            (
                self.sensor, 12.0, "off", {"device_class": "motion"},
                None, "test", 35.0,
            ),
            (
                self.sensor, 40.0, "on", {"device_class": "motion"},
                None, "test", 40.0,
            ),
        ])
        state_map = {
            self.sensor: {
                "entity_id": self.sensor,
                "state": "off",
                "attributes": {"device_class": "motion"},
            },
            self.target: {
                "entity_id": self.target,
                "state": "off",
                "attributes": {},
            },
        }
        registry = {
            self.sensor: {"area_id": "room"},
            self.target: {"area_id": "room"},
        }
        self.context = ContextEngine(DEFAULT_OPTIONS)
        self.context.configure(state_map, entities=registry)

    def tearDown(self):
        self.tmp.cleanup()

    def tracker(self, shared_cache):
        return SQLiteTemporalTracker(
            self.store,
            [self.sensor],
            self.context,
            0.0,
            60.0,
            home_context_cache=shared_cache,
            context_cache_contract="feature-snapshot-test-v1",
        )

    def test_two_trackers_share_revision_only_for_same_causal_view(self):
        shared = HistoricalContextCache(max_entries=16, max_units=4096)
        left = self.tracker(shared)
        right = self.tracker(shared)
        try:
            left.advance(20.0)
            right.advance(20.0)
            before_left = left.feature_snapshot_revision([self.sensor], 20.0)
            before_right = right.feature_snapshot_revision([self.sensor], 20.0)
            self.assertEqual(before_left, before_right)

            left.advance(36.0)
            right.advance(36.0)
            after_left = left.feature_snapshot_revision([self.sensor], 36.0)
            after_right = right.feature_snapshot_revision([self.sensor], 36.0)
            self.assertEqual(after_left, after_right)
            self.assertNotEqual(before_left, after_left)

            # Rewind must reconstruct the exact earlier causal token; the late t=12
            # sample received at 35 must disappear again.
            left.advance(20.0)
            rewind = left.feature_snapshot_revision([self.sensor], 20.0)
            self.assertEqual(rewind, before_left)
        finally:
            left.close()
            right.close()


class FeatureSnapshotResourceProfileTests(unittest.TestCase):
    def test_large_worker_gets_bounded_feature_cache(self):
        profile = resolve_training_resource_profile(
            DEFAULT_OPTIONS,
            {"total_mb": 3900.0, "available_mb": 2400.0},
        )
        worker = profile["worker_options"]
        self.assertEqual(
            worker["training_worker_effective_feature_snapshot_cache_entries"],
            512,
        )
        self.assertEqual(
            worker["training_worker_effective_feature_snapshot_cache_units"],
            65536,
        )

    def test_small_worker_reduces_feature_cache(self):
        profile = resolve_training_resource_profile(
            DEFAULT_OPTIONS,
            {"total_mb": 1024.0, "available_mb": 600.0},
        )
        worker = profile["worker_options"]
        self.assertEqual(
            worker["training_worker_effective_feature_snapshot_cache_entries"],
            64,
        )
        self.assertEqual(
            worker["training_worker_effective_feature_snapshot_cache_units"],
            8192,
        )

    def test_feature_cache_bounds_are_nonsemantic(self):
        base = dict(DEFAULT_OPTIONS)
        changed = dict(base)
        changed["training_feature_snapshot_cache_entries"] = 7
        changed["training_feature_snapshot_cache_units"] = 777
        self.assertEqual(
            training_options_fingerprint(base),
            training_options_fingerprint(changed),
        )

    def test_addon_config_exposes_feature_cache_bounds(self):
        root = Path(__file__).resolve().parents[1]
        config = (root / "adaptive_ai" / "config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("training_feature_snapshot_cache_entries: 512", config)
        self.assertIn("training_feature_snapshot_cache_units: 65536", config)
        self.assertIn(
            'training_feature_snapshot_cache_entries: "int(0,2048)"',
            config,
        )
        self.assertIn(
            'training_feature_snapshot_cache_units: "int(0,262144)"',
            config,
        )


if __name__ == "__main__":
    unittest.main()
