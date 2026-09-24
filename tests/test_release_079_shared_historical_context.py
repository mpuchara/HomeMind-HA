import tempfile
import threading
import unittest
from pathlib import Path

from context_engine import ContextEngine
from observation_contract import FeatureJournal, ObservationSQLiteTemporalTracker
from replay import HistoricalContextCache, ReplayQueryCache, SQLiteTemporalTracker
from settings import DEFAULT_OPTIONS
from storage import Store
from support import state


FORECAST_KEYS = (
    "occupancy_now", "occupancy_in_1s", "occupancy_in_3s",
    "occupancy_in_5s", "arrival_probability", "departure_probability",
    "trajectory_confidence", "known", "area_id",
)


class SharedHistoricalContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "shared-context.db")
        self.base = 1_701_000_000.0
        self.motion = "binary_sensor.kitchen_motion"
        self.radar = "binary_sensor.kitchen_presence"
        self.target = "light.kitchen"
        self.states = {
            self.motion: state(self.motion, "off", device_class="motion"),
            self.radar: state(self.radar, "off", device_class="occupancy"),
            self.target: state(self.target, "off"),
        }
        self.registry = {
            self.motion: {"area_id": "kitchen"},
            self.radar: {"area_id": "kitchen"},
            self.target: {"area_id": "kitchen"},
        }
        self.ctx = ContextEngine(DEFAULT_OPTIONS)
        self.ctx.configure(self.states, entities=self.registry)
        self.store.archive_batch([
            (self.motion, self.base + 0, "off", {"device_class": "motion"}, None, "test"),
            (self.radar, self.base + 0, "off", {"device_class": "occupancy"}, None, "test"),
            (self.motion, self.base + 10, "on", {"device_class": "motion"}, None, "test"),
            (self.radar, self.base + 12, "on", {"device_class": "occupancy"}, None, "test"),
            (self.motion, self.base + 18, "off", {"device_class": "motion"}, None, "test"),
            (self.radar, self.base + 42, "off", {"device_class": "occupancy"}, None, "test"),
        ])

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def signature(forecast):
        out = {}
        for key in FORECAST_KEYS:
            value = forecast.get(key)
            out[key] = round(float(value), 12) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
        return out

    def tracker(self, cache=None, contract="policy:11:schema:12:feature:2"):
        return SQLiteTemporalTracker(
            self.store,
            [self.motion, self.radar],
            self.ctx,
            self.base,
            self.base + 120,
            query_cache=ReplayQueryCache(max_rows=4096),
            home_context_cache=cache,
            context_cache_contract=contract,
        )

    def test_two_trackers_share_exact_immutable_snapshot_with_forecast_parity(self):
        cache = HistoricalContextCache(max_entries=8, max_units=2048)
        first = self.tracker(cache)
        second = self.tracker(cache)
        try:
            ts = self.base + 35
            first.advance(ts)
            expected = self.signature(first.history.home_context.forecast(self.target, ts))

            second.advance(ts)
            actual = self.signature(second.history.home_context.forecast(self.target, ts))

            self.assertEqual(actual, expected)
            self.assertEqual(second.stats()["home_context_cache_hits"], 1)
            self.assertEqual(second.stats()["home_render_executes"], 0)
            self.assertEqual(cache.status()["hits"], 1)
            self.assertEqual(cache.status()["entries"], 1)
        finally:
            first.close()
            second.close()

    def test_rewind_reuses_exact_snapshot_without_future_leak(self):
        cache = HistoricalContextCache(max_entries=8, max_units=2048)
        tracker = self.tracker(cache)
        try:
            early = self.base + 25
            late = self.base + 55
            tracker.advance(early)
            expected = self.signature(
                tracker.history.home_context.forecast(self.target, early)
            )
            tracker.advance(late)
            tracker.advance(early)
            actual = self.signature(
                tracker.history.home_context.forecast(self.target, early)
            )
            self.assertEqual(actual, expected)
            self.assertEqual(tracker.stats()["rewinds"], 1)
            self.assertGreaterEqual(tracker.stats()["home_context_cache_hits"], 1)
        finally:
            tracker.close()

    def test_late_received_observation_changes_key_and_forces_miss(self):
        journal = FeatureJournal(self.store)
        cache = HistoricalContextCache(max_entries=8, max_units=2048)
        ts = self.base + 35

        first = ObservationSQLiteTemporalTracker(
            self.store, [self.motion, self.radar], self.ctx,
            self.base, self.base + 120,
            home_context_cache=cache,
            context_cache_contract="feature-contract-v2",
        )
        try:
            first.advance(ts)
            first_forecast = self.signature(
                first.history.home_context.forecast(self.target, ts)
            )
            first_seed_id = first._home_seed_rows[self.radar]["id"]
        finally:
            first.close()

        # RoomBelief's causal window starts at ts-30. A fast-journal event received
        # *after* that cutoff is intentionally not part of the RoomBelief seed even if
        # policy feature history can see it later. To test cache invalidation itself,
        # inject a genuinely late packet (receipt > event time) that is nevertheless
        # causally visible by the cutoff and therefore changes the seed input.
        journal.record(
            self.radar,
            state(self.radar, "on", device_class="occupancy"),
            event_time=self.base + 1,
            received_time=self.base + 4,
            source="late-test",
            event_key="late-visible-before-cutoff",
        )

        # Authoritative reference after the late packet: no shared home-context cache.
        reference = ObservationSQLiteTemporalTracker(
            self.store, [self.motion, self.radar], self.ctx,
            self.base, self.base + 120,
            home_context_cache=None,
            context_cache_contract="feature-contract-v2",
        )
        try:
            reference.advance(ts)
            reference_forecast = self.signature(
                reference.history.home_context.forecast(self.target, ts)
            )
            reference_seed = dict(reference._home_seed_rows[self.radar])
        finally:
            reference.close()

        second = ObservationSQLiteTemporalTracker(
            self.store, [self.motion, self.radar], self.ctx,
            self.base, self.base + 120,
            home_context_cache=cache,
            context_cache_contract="feature-contract-v2",
        )
        try:
            second.advance(ts)
            second_forecast = self.signature(
                second.history.home_context.forecast(self.target, ts)
            )
            self.assertEqual(second.stats()["home_context_cache_hits"], 0)
            self.assertEqual(second.stats()["home_context_cache_misses"], 1)
            self.assertEqual(cache.status()["hits"], 0)
            self.assertEqual(cache.status()["misses"], 2)
            self.assertNotEqual(first_seed_id, reference_seed["id"])
            self.assertEqual(
                float(reference_seed.get("_feature_received_time") or 0.0),
                self.base + 4,
            )
            self.assertEqual(
                second._home_seed_rows[self.radar]["id"], reference_seed["id"]
            )
            self.assertEqual(second_forecast, reference_forecast)
        finally:
            second.close()

    def test_future_by_receipt_stays_invisible_and_exact_snapshot_can_be_reused(self):
        journal = FeatureJournal(self.store)
        cache = HistoricalContextCache(max_entries=8, max_units=2048)
        ts = self.base + 35

        journal.record(
            self.radar,
            state(self.radar, "off", device_class="occupancy"),
            event_time=ts - 5,
            received_time=ts + 10,
            source="future-receipt",
            event_key="not-visible-yet",
        )

        first = ObservationSQLiteTemporalTracker(
            self.store, [self.motion, self.radar], self.ctx,
            self.base, self.base + 120,
            home_context_cache=cache,
            context_cache_contract="feature-contract-v2",
        )
        second = ObservationSQLiteTemporalTracker(
            self.store, [self.motion, self.radar], self.ctx,
            self.base, self.base + 120,
            home_context_cache=cache,
            context_cache_contract="feature-contract-v2",
        )
        try:
            first.advance(ts)
            expected = self.signature(first.history.home_context.forecast(self.target, ts))
            first_state = first.state_map[self.radar]["state"]

            second.advance(ts)
            actual = self.signature(second.history.home_context.forecast(self.target, ts))
            self.assertEqual(actual, expected)
            self.assertEqual(second.state_map[self.radar]["state"], first_state)
            self.assertEqual(second.stats()["home_context_cache_hits"], 1)
        finally:
            first.close()
            second.close()

    def test_topology_revision_invalidates_snapshot(self):
        cache = HistoricalContextCache(max_entries=8, max_units=2048)
        ts = self.base + 35
        first = self.tracker(cache)
        try:
            first.advance(ts)
        finally:
            first.close()

        moved_registry = dict(self.registry)
        moved_registry[self.radar] = {"area_id": "hall"}
        self.ctx.configure(self.states, entities=moved_registry)

        second = self.tracker(cache)
        try:
            second.advance(ts)
            self.assertEqual(second.stats()["home_context_cache_hits"], 0)
            self.assertEqual(second.stats()["home_context_cache_misses"], 1)
        finally:
            second.close()

    def test_feature_contract_namespace_prevents_cross_contract_reuse(self):
        cache = HistoricalContextCache(max_entries=8, max_units=2048)
        ts = self.base + 35
        first = self.tracker(cache, contract="policy:11:schema:12:feature:1")
        second = self.tracker(cache, contract="policy:11:schema:12:feature:2")
        try:
            first.advance(ts)
            second.advance(ts)
            self.assertEqual(second.stats()["home_context_cache_hits"], 0)
            self.assertEqual(cache.status()["hits"], 0)
            self.assertEqual(cache.status()["misses"], 2)
        finally:
            first.close()
            second.close()

    def test_cache_is_bounded_and_thread_safe(self):
        cache = HistoricalContextCache(max_entries=2, max_units=4096)
        errors = []

        def run(offset):
            tracker = self.tracker(cache, contract="parallel-v1")
            try:
                tracker.advance(self.base + 25 + offset)
                tracker.history.home_context.forecast(
                    self.target, self.base + 25 + offset
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                tracker.close()

        threads = [
            threading.Thread(target=run, args=(float(i * 10),), daemon=True)
            for i in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3.0)

        self.assertEqual(errors, [])
        status = cache.status()
        self.assertLessEqual(status["entries"], 2)
        self.assertGreaterEqual(status["evictions"], 1)
        self.assertLessEqual(status["units"], status["max_units"])


if __name__ == "__main__":
    unittest.main()
