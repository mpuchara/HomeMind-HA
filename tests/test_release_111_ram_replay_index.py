"""0.14.111 bounded RAM replay index parity and fallback tests."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from context_engine import ContextEngine
from replay import RAMReplayIndex, SQLiteTemporalTracker
from settings import DEFAULT_OPTIONS
from storage import Store
from support import state
from training_process import resolve_training_resource_profile


def context_stub():
    return SimpleNamespace(options={}, relevant_entities=lambda: [])


class RAMReplayIndexParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hm-ram-replay-")
        self.store = Store(Path(self.tmp.name) / "adaptive_ai.db")
        rows = [
            ("sensor.a", 5.0, "off", {"v": 0}, None, "live", 5.0),
            ("sensor.a", 10.0, "on", {"v": 1}, None, "live", 10.0),
            # Event happened at 12 but must not become visible until received at 35.
            ("sensor.a", 12.0, "off", {"v": 2}, None, "live", 35.0),
            ("sensor.a", 20.0, "on", {"v": 3}, None, "live", 20.0),
            ("sensor.a", 40.0, "off", {"v": 4}, None, "live", None),
            ("sensor.b", 7.0, "1", {"n": 1}, None, "live", 7.0),
            ("sensor.b", 25.0, "2", {"n": 2}, None, "live", 25.0),
            ("sensor.b", 45.0, "3", {"n": 3}, None, "live", 45.0),
        ]
        self.store.archive_batch(rows)

    def tearDown(self):
        self.tmp.cleanup()

    def build_index(self, max_bytes=8 * 1024 * 1024):
        connection = sqlite3.connect(self.store.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            return RAMReplayIndex.build(
                connection,
                ["sensor.a", "sensor.b"],
                4.0,
                50.0,
                max_bytes=max_bytes,
            )
        finally:
            connection.close()

    def test_bulk_and_interval_queries_match_sqlite_with_late_receipt(self):
        index = self.build_index()
        base = SQLiteTemporalTracker(
            self.store, ["sensor.a", "sensor.b"], context_stub(), 4.0, 50.0
        )
        ram = SQLiteTemporalTracker(
            self.store, ["sensor.a", "sensor.b"], context_stub(), 4.0, 50.0,
            ram_replay_index=index,
        )
        try:
            for ts in (9.0, 15.0, 30.0, 36.0, 50.0):
                self.assertEqual(
                    base._base_bulk_before(["sensor.a", "sensor.b"], ts, 64),
                    ram._base_bulk_before(["sensor.a", "sensor.b"], ts, 64),
                )
            for lo, hi in ((4.0, 11.0), (11.0, 30.0), (30.0, 36.0), (36.0, 50.0)):
                self.assertEqual(
                    base._base_interval_rows(
                        ["sensor.a", "sensor.b"], lo, hi, per_entity_limit=64
                    ),
                    ram._base_interval_rows(
                        ["sensor.a", "sensor.b"], lo, hi, per_entity_limit=64
                    ),
                )
                self.assertEqual(
                    base._base_interval_rows(
                        ["sensor.a", "sensor.b"], lo, hi, per_entity_limit=None
                    ),
                    ram._base_interval_rows(
                        ["sensor.a", "sensor.b"], lo, hi, per_entity_limit=None
                    ),
                )

            before_receive = ram._base_bulk_before(["sensor.a"], 30.0, 64)
            after_receive = ram._base_bulk_before(["sensor.a"], 36.0, 64)
            self.assertNotIn(12.0, [row["ts"] for row in before_receive])
            self.assertIn(12.0, [row["ts"] for row in after_receive])
            self.assertEqual(ram.stats()["sqlite_fallback_lookups"], 0)
        finally:
            base.close()
            ram.close()

    def test_tiny_budget_uses_exact_sqlite_fallback(self):
        index = self.build_index(max_bytes=1024)
        self.assertEqual(index.status()["indexed_entities"], 0)
        self.assertEqual(index.status()["fallback_entities"], 2)

        base = SQLiteTemporalTracker(
            self.store, ["sensor.a", "sensor.b"], context_stub(), 4.0, 50.0
        )
        ram = SQLiteTemporalTracker(
            self.store, ["sensor.a", "sensor.b"], context_stub(), 4.0, 50.0,
            ram_replay_index=index,
        )
        try:
            self.assertEqual(
                base._base_bulk_before(["sensor.a", "sensor.b"], 50.0, 64),
                ram._base_bulk_before(["sensor.a", "sensor.b"], 50.0, 64),
            )
            self.assertGreater(ram.stats()["sqlite_fallback_lookups"], 0)
        finally:
            base.close()
            ram.close()


    def test_partial_budget_mixes_ram_and_sqlite_without_order_change(self):
        # Leave just over the admission guard: sensor.a fits first, then the remaining
        # budget is too small for sensor.b and the same lookup mixes both transports.
        index = self.build_index(max_bytes=256 * 1024 + 512)
        status = index.status()
        self.assertEqual(status["indexed_entities"], 1)
        self.assertEqual(status["fallback_entities"], 1)

        base = SQLiteTemporalTracker(
            self.store, ["sensor.a", "sensor.b"], context_stub(), 4.0, 50.0
        )
        ram = SQLiteTemporalTracker(
            self.store, ["sensor.a", "sensor.b"], context_stub(), 4.0, 50.0,
            ram_replay_index=index,
        )
        try:
            self.assertEqual(
                base._base_bulk_before(["sensor.a", "sensor.b"], 36.0, 64),
                ram._base_bulk_before(["sensor.a", "sensor.b"], 36.0, 64),
            )
            self.assertEqual(
                base._base_interval_rows(
                    ["sensor.a", "sensor.b"], 11.0, 36.0, per_entity_limit=64
                ),
                ram._base_interval_rows(
                    ["sensor.a", "sensor.b"], 11.0, 36.0, per_entity_limit=64
                ),
            )
            self.assertGreater(ram.stats()["ram_index_rows"], 0)
            self.assertGreater(ram.stats()["sqlite_fallback_lookups"], 0)
        finally:
            base.close()
            ram.close()

    def test_advance_rewind_and_home_forecast_match_sqlite_tracker(self):
        motion = "binary_sensor.motion"
        radar = "binary_sensor.presence"
        target = "light.target"
        base_ts = 1000.0
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
        ctx_sql = ContextEngine(DEFAULT_OPTIONS)
        ctx_sql.configure(states, entities=registry)
        ctx_ram = ContextEngine(DEFAULT_OPTIONS)
        ctx_ram.configure(states, entities=registry)
        self.store.archive_batch([
            (motion, base_ts + 0, "off", {"device_class": "motion"}, None, "test", base_ts + 0),
            (radar, base_ts + 0, "off", {"device_class": "occupancy"}, None, "test", base_ts + 0),
            (motion, base_ts + 10, "on", {"device_class": "motion"}, None, "test", base_ts + 10),
            # Late presence evidence must not leak into the t=20 view.
            (radar, base_ts + 12, "on", {"device_class": "occupancy"}, None, "test", base_ts + 28),
            (motion, base_ts + 24, "off", {"device_class": "motion"}, None, "test", base_ts + 24),
            (radar, base_ts + 42, "off", {"device_class": "occupancy"}, None, "test", base_ts + 42),
        ])
        connection = sqlite3.connect(self.store.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            index = RAMReplayIndex.build(
                connection, [motion, radar], base_ts - 1, base_ts + 60,
                max_bytes=8 * 1024 * 1024,
            )
        finally:
            connection.close()

        sql_tracker = SQLiteTemporalTracker(
            self.store, [motion, radar], ctx_sql, base_ts - 1, base_ts + 60
        )
        ram_tracker = SQLiteTemporalTracker(
            self.store, [motion, radar], ctx_ram, base_ts - 1, base_ts + 60,
            ram_replay_index=index,
        )
        try:
            for query_ts in (
                base_ts + 20,
                base_ts + 35,
                base_ts + 50,
                base_ts + 18,  # explicit rewind
                base_ts + 50,
            ):
                sql_tracker.advance(query_ts)
                ram_tracker.advance(query_ts)
                self.assertEqual(sql_tracker.state_map, ram_tracker.state_map)
                self.assertEqual(
                    {
                        eid: list(samples)
                        for eid, samples in sql_tracker.history.samples.items()
                    },
                    {
                        eid: list(samples)
                        for eid, samples in ram_tracker.history.samples.items()
                    },
                )
                expected = sql_tracker.history.home_context.forecast(target, query_ts)
                actual = ram_tracker.history.home_context.forecast(target, query_ts)
                for key in (
                    "occupancy_now", "occupancy_in_1s", "occupancy_in_3s",
                    "occupancy_in_5s", "arrival_probability",
                    "departure_probability", "trajectory_confidence",
                ):
                    self.assertAlmostEqual(
                        float(expected.get(key) or 0.0),
                        float(actual.get(key) or 0.0),
                        places=9,
                        msg=f"{query_ts}:{key}",
                    )
        finally:
            sql_tracker.close()
            ram_tracker.close()

    def test_out_of_coverage_query_falls_back_without_changing_result(self):
        index = self.build_index()
        base = SQLiteTemporalTracker(
            self.store, ["sensor.a"], context_stub(), 0.0, 80.0
        )
        ram = SQLiteTemporalTracker(
            self.store, ["sensor.a"], context_stub(), 0.0, 80.0,
            ram_replay_index=index,
        )
        try:
            self.assertEqual(
                base._base_bulk_before(["sensor.a"], 70.0, 64),
                ram._base_bulk_before(["sensor.a"], 70.0, 64),
            )
            self.assertGreater(ram.stats()["sqlite_fallback_lookups"], 0)
        finally:
            base.close()
            ram.close()


class RAMReplayResourceProfileTests(unittest.TestCase):
    def test_addon_config_exposes_index_budget(self):
        config = (Path(__file__).resolve().parents[1] / "adaptive_ai" / "config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("training_ram_replay_index_mb: 192", config)
        self.assertIn('training_ram_replay_index_mb: "int(0,384)"', config)

    def test_large_worker_gets_bounded_index_budget(self):
        profile = resolve_training_resource_profile(
            DEFAULT_OPTIONS,
            {"total_mb": 3900.0, "available_mb": 2400.0},
        )
        self.assertEqual(
            profile["worker_options"]["training_worker_effective_ram_replay_index_mb"],
            192,
        )

    def test_small_worker_reduces_index_budget(self):
        profile = resolve_training_resource_profile(
            DEFAULT_OPTIONS,
            {"total_mb": 1024.0, "available_mb": 600.0},
        )
        self.assertEqual(
            profile["worker_options"]["training_worker_effective_ram_replay_index_mb"],
            24,
        )

    def test_index_budget_is_nonsemantic(self):
        from training_process import training_options_fingerprint
        base = dict(DEFAULT_OPTIONS)
        changed = dict(base)
        changed["training_ram_replay_index_mb"] = 32
        self.assertEqual(
            training_options_fingerprint(base),
            training_options_fingerprint(changed),
        )


if __name__ == "__main__":
    unittest.main()
