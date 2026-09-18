import tempfile
import unittest
from pathlib import Path

from context import archived_state
from context_engine import ContextEngine
from observation_contract import FeatureJournal, ObservationSQLiteTemporalTracker
from replay import HistoricalHomeView, SQLiteTemporalTracker
from settings import DEFAULT_OPTIONS
from storage import Store
from support import state


class IncrementalTemporalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "incremental-replay.db")
        self.base = 1_700_000_000.0

    def tearDown(self):
        self.temp.cleanup()

    def add(self, eid, ts, value, attrs=None):
        self.store.archive_batch([
            (eid, float(ts), str(value), attrs or {}, None, "test")
        ])

    def context(self, states, registry=None):
        ctx = ContextEngine(DEFAULT_OPTIONS)
        ctx.configure(states, entities=registry or {})
        return ctx

    def test_forward_advance_reuses_bounded_entity_history(self):
        watched = [f"sensor.ctx_{i}" for i in range(50)]
        states = {eid: state(eid, "0") for eid in watched}
        for idx, eid in enumerate(watched):
            self.add(eid, self.base, 0)
            self.add(eid, self.base + 10, 1)
            self.add(eid, self.base + 20, 2)

        tracker = SQLiteTemporalTracker(
            self.store, watched, self.context(states),
            self.base, self.base + 60,
        )
        try:
            tracker.advance(self.base + 5)
            first = tracker.stats()
            tracker.advance(self.base + 15)
            second = tracker.stats()

            self.assertEqual(tracker.state_map[watched[0]]["state"], "1")
            self.assertEqual(second["bulk_rebuilds"], 1)
            self.assertEqual(second["forward_advances"], 1)
            self.assertGreater(second["legacy_asof_queries_estimate"], 90)
            self.assertLess(second["sql_queries"], 10)
            self.assertGreater(second["query_reduction_ratio"], .85)

            before_same = tracker.stats()["sql_queries"]
            tracker.advance(self.base + 15)
            after_same = tracker.stats()
            self.assertEqual(after_same["sql_queries"], before_same)
            self.assertEqual(after_same["same_ts_hits"], 1)
            self.assertLessEqual(len(tracker.history.samples[watched[0]]), 64)
            self.assertGreaterEqual(first["bulk_rebuilds"], 1)
        finally:
            tracker.close()

    def test_rewind_is_bulk_and_never_leaks_future_state(self):
        watched = [f"sensor.ctx_{i}" for i in range(40)]
        states = {eid: state(eid, "off") for eid in watched}
        for eid in watched:
            self.add(eid, self.base, "off")
            self.add(eid, self.base + 10, "on")
            self.add(eid, self.base + 20, "off")

        tracker = SQLiteTemporalTracker(
            self.store, watched, self.context(states),
            self.base, self.base + 60,
        )
        try:
            tracker.advance(self.base + 25)
            self.assertEqual(tracker.state_map[watched[0]]["state"], "off")
            tracker.advance(self.base + 15)
            self.assertEqual(tracker.state_map[watched[0]]["state"], "on")
            rewound = tracker.stats()
            self.assertEqual(rewound["rewinds"], 1)
            self.assertEqual(rewound["bulk_rebuilds"], 2)
            # The rewind uses a partitioned bulk query, not 40 per-entity SELECTs.
            self.assertLess(rewound["sql_queries"], 10)

            tracker.advance(self.base + 25)
            self.assertEqual(tracker.state_map[watched[0]]["state"], "off")
            self.assertEqual(tracker.stats()["forward_advances"], 1)
        finally:
            tracker.close()

    def test_history_stays_last_64_samples_after_large_forward_jump(self):
        eid = "sensor.fast"
        rows = [
            (eid, self.base + i, str(i), {}, None, "test")
            for i in range(200)
        ]
        self.store.archive_batch(rows)
        tracker = SQLiteTemporalTracker(
            self.store, [eid], self.context({eid: state(eid, "0")}),
            self.base, self.base + 300,
        )
        try:
            tracker.advance(self.base + 20)
            tracker.advance(self.base + 199)
            samples = list(tracker.history.samples[eid])
            self.assertEqual(len(samples), 64)
            self.assertEqual(samples[-1][1]["state"], "199")
            self.assertEqual(samples[0][1]["state"], "136")
        finally:
            tracker.close()

    def test_bulk_home_rebuild_matches_legacy_seed_and_recent_window(self):
        motion = "binary_sensor.kitchen_motion"
        radar = "binary_sensor.kitchen_presence"
        target = "light.kitchen"
        states = {
            motion: state(motion, "off", device_class="motion"),
            radar: state(radar, "off", device_class="occupancy"),
            target: state(target, "off"),
        }
        registry = {
            motion: {"area_id": "kitchen"},
            radar: {"area_id": "kitchen"},
            target: {"area_id": "kitchen"},
        }
        ctx = self.context(states, registry)
        self.store.archive_batch([
            (motion, self.base, "off", {"device_class": "motion"}, None, "test"),
            (radar, self.base, "off", {"device_class": "occupancy"}, None, "test"),
            (motion, self.base + 10, "on", {"device_class": "motion"}, None, "test"),
            (radar, self.base + 12, "on", {"device_class": "occupancy"}, None, "test"),
            (motion, self.base + 18, "off", {"device_class": "motion"}, None, "test"),
        ])
        query_ts = self.base + 25

        tracker = SQLiteTemporalTracker(
            self.store, [motion, radar], ctx, self.base, self.base + 60
        )
        try:
            tracker.advance(query_ts)
            actual = tracker.history.home_context.forecast(target, query_ts)
        finally:
            tracker.close()

        # Reference the exact pre-0.14.25 algorithm: one seed lookup per source,
        # then raw entity_history events from the last 30 seconds.
        view = HistoricalHomeView(ctx, None)
        cutoff = query_ts - 30
        seeded = set()
        with self.store.conn() as conn:
            for eid in ctx.relevant_entities():
                row = conn.execute(
                    "SELECT * FROM entity_history WHERE entity_id=? AND ts<=? "
                    "ORDER BY ts DESC,id DESC LIMIT 1",
                    (eid, cutoff),
                ).fetchone()
                if row:
                    row = dict(row)
                    area = ctx.area_for(eid)
                    view.home.observe(
                        eid, area, ctx.sensor_probability(eid, archived_state(row)),
                        cutoff, learn=False, evidence=ctx.evidence_metadata(eid),
                    )
                    if area:
                        seeded.add(area)
            view.home.reset_movement_state()
            for area in sorted(seeded):
                view.observe_adaptive(area, cutoff)
            ids = ctx.relevant_entities()
            if ids:
                marks = ",".join("?" for _ in ids)
                rows = conn.execute(
                    "SELECT * FROM entity_history WHERE ts>? AND ts<=? "
                    f"AND entity_id IN ({marks}) ORDER BY ts,id",
                    [cutoff, query_ts, *ids],
                ).fetchall()
                for raw in rows:
                    row = dict(raw)
                    eid = row["entity_id"]
                    area = ctx.area_for(eid)
                    view.home.observe(
                        eid, area, ctx.sensor_probability(eid, archived_state(row)),
                        row["ts"], learn=False, evidence=ctx.evidence_metadata(eid),
                    )
                    view.observe_adaptive(area, row["ts"])
        expected = view.forecast(target, query_ts)

        for key in (
            "occupancy_now", "occupancy_in_1s", "occupancy_in_3s",
            "occupancy_in_5s", "arrival_probability", "departure_probability",
            "trajectory_confidence",
        ):
            self.assertAlmostEqual(
                float(actual.get(key) or 0.0),
                float(expected.get(key) or 0.0),
                places=9,
                msg=key,
            )


class ObservationIncrementalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "observation-replay.db")
        self.base = 1_700_100_000.0
        self.eid = "sensor.context"
        self.ctx = ContextEngine(DEFAULT_OPTIONS)
        self.ctx.configure({self.eid: state(self.eid, "10")})
        self.journal = FeatureJournal(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_late_received_feature_becomes_visible_without_future_leak(self):
        self.store.archive_batch([
            (self.eid, self.base, "10", {}, None, "test"),
            (self.eid, self.base + 20, "20", {}, None, "test"),
        ])
        self.journal.record(
            self.eid, state(self.eid, "15"),
            event_time=self.base + 5,
            received_time=self.base + 15,
            source="ha_state_changed",
            event_key="late-1",
        )

        tracker = ObservationSQLiteTemporalTracker(
            self.store, [self.eid], self.ctx,
            self.base, self.base + 60,
        )
        try:
            tracker.advance(self.base + 10)
            self.assertEqual(tracker.state_map[self.eid]["state"], "10")

            tracker.advance(self.base + 16)
            self.assertEqual(tracker.state_map[self.eid]["state"], "15")
            self.assertEqual(
                tracker.history.previous(self.eid, self.base + 6)["state"], "15"
            )

            tracker.advance(self.base + 25)
            self.assertEqual(tracker.state_map[self.eid]["state"], "20")
            stats = tracker.stats()
            self.assertEqual(stats["bulk_rebuilds"], 1)
            self.assertEqual(stats["forward_advances"], 2)
        finally:
            tracker.close()

    def test_same_timestamp_feature_and_archive_keep_v12_merge_contract(self):
        self.store.archive_batch([
            (self.eid, self.base + 5, "off", {}, None, "test"),
        ])
        self.journal.record(
            self.eid, state(self.eid, "off"),
            event_time=self.base + 5,
            received_time=self.base + 6,
            source="ha_state_changed",
            event_key="same-state",
        )
        tracker = ObservationSQLiteTemporalTracker(
            self.store, [self.eid], self.ctx,
            self.base, self.base + 60,
        )
        try:
            tracker.advance(self.base + 10)
            samples = list(tracker.history.samples[self.eid])
            self.assertEqual(len(samples), 1)
            attrs = samples[0][1].get("attributes") or {}
            self.assertEqual(attrs.get("__hm_received_time"), self.base + 6)
        finally:
            tracker.close()


class Release025SourceContractTests(unittest.TestCase):
    def source(self, name):
        root = Path(__file__).resolve().parents[1]
        return (root / "adaptive_ai" / "src" / name).read_text(encoding="utf-8")

    def test_tracker_has_bulk_bootstrap_forward_cursor_and_rewind_path(self):
        source = self.source("replay.py")
        self.assertIn("UNION ALL", source)
        self.assertIn("ORDER BY ts DESC,id DESC LIMIT ?", source)
        self.assertNotIn("ROW_NUMBER() OVER", source)
        self.assertIn("def _forward_watched", source)
        self.assertIn('"forward_advances"', source)
        self.assertIn('"bulk_rebuilds"', source)
        self.assertIn('"rewinds"', source)
        self.assertIn('"same_ts_hits"', source)
        self.assertIn("query_reduction_ratio", source)

    def test_history_separates_onset_and_persistence_cursor_roles(self):
        source = self.source("history.py")
        self.assertIn("persistence_timeline = SQLiteTemporalTracker", source)
        self.assertIn("for query_ts in sorted(query_times)", source)
        self.assertIn(
            "for target_time, h in sorted(persistence_tasks",
            source,
        )
        self.assertIn('"contract": "incremental_bulk_v1"', source)
        self.assertIn('"temporal_replay": dict(self.temporal_replay_stats)', source)

    def test_observation_cursor_tracks_received_time_incrementally(self):
        source = self.source("observation_contract.py")
        self.assertIn("(event_time>? OR received_time>?)", source)
        self.assertIn("temporal_feature_forward_query", source)
        self.assertIn("late packets are merged back", source)
        self.assertIn("UNION ALL", source)
        self.assertNotIn("ROW_NUMBER() OVER", source)

    def test_ui_surfaces_temporal_query_reduction(self):
        source = self.source("static/app.js")
        self.assertIn("h.temporal_replay?.totals", source)
        self.assertIn("query_reduction_ratio", source)
        self.assertIn("Temporal replay:", source)


if __name__ == "__main__":
    unittest.main()
