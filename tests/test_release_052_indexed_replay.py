"""0.14.52 regressions for indexed incremental observation replay."""
import tempfile
import unittest
from pathlib import Path

from context_engine import ContextEngine
from observation_contract import FeatureJournal, ObservationSQLiteTemporalTracker
from settings import DEFAULT_OPTIONS
from storage import Store
from support import state


class Release052IndexedReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "indexed-replay.db")
        self.base = 1_700_200_000.0
        self.eid = "binary_sensor.motion"
        self.ctx = ContextEngine(DEFAULT_OPTIONS)
        self.ctx.configure({self.eid: state(self.eid, "off", device_class="motion")})
        self.journal = FeatureJournal(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def _insert(self, key, event_time, received_time, value):
        self.journal.record(
            self.eid,
            state(self.eid, value, device_class="motion"),
            event_time=float(event_time),
            received_time=float(received_time),
            source="test",
            event_key=str(key),
        )

    def _legacy_reference(self, tracker, lo, hi):
        sql = (
            "SELECT event_key,entity_id,event_time,received_time,state,attributes_json,"
            "last_changed,last_updated,source,quality "
            "FROM feature_observation_events "
            "WHERE entity_id=? AND event_time<=? AND received_time<=? "
            "AND (event_time>? OR received_time>?) "
            "ORDER BY event_time DESC,received_time DESC,event_key DESC LIMIT ?"
        )
        rows = tracker.conn.execute(
            sql, (self.eid, float(hi), float(hi), float(lo), float(lo), tracker.HISTORY_SAMPLES)
        ).fetchall()
        out = [FeatureJournal.normalized_row(dict(row)) for row in rows]
        out.sort(key=tracker._row_order)
        return out

    def test_split_ranges_match_legacy_or_semantics(self):
        lo, hi = self.base + 100, self.base + 110

        # Old rows must not reappear.
        for i in range(90):
            self._insert(f"old-{i:03d}", self.base + i, self.base + i + 0.1, str(i % 2))

        # Newly occurring rows.
        for i in range(20):
            self._insert(
                f"event-{i:03d}",
                lo + 0.1 + (i * 0.2),
                lo + 0.2 + (i * 0.2),
                str(i % 2),
            )

        # Late received rows whose event_time belongs to the old prefix.
        for i in range(20):
            self._insert(
                f"late-{i:03d}",
                self.base + 20 + i,
                lo + 0.15 + (i * 0.2),
                str((i + 1) % 2),
            )

        # Future-by-receipt and future-by-event rows stay invisible.
        self._insert("future-received", lo - 10, hi + 5, "on")
        self._insert("future-event", hi + 5, hi - 1, "on")

        tracker = ObservationSQLiteTemporalTracker(
            self.store, [self.eid], self.ctx, self.base, hi + 20
        )
        try:
            expected = self._legacy_reference(tracker, lo, hi)
            actual = tracker._feature_interval_rows([self.eid], lo, hi)
        finally:
            tracker.close()

        def signature(rows):
            return [
                (
                    row["entity_id"],
                    row["id"],
                    float(row["ts"]),
                    float(row.get("_feature_received_time") or 0.0),
                    row.get("state"),
                )
                for row in rows
            ]

        self.assertEqual(signature(actual), signature(expected))

    def test_received_range_uses_entity_received_time_index(self):
        # Large old history plus an empty forward interval: the query plan must seek by
        # received_time rather than scan the entity's entire event-time prefix.
        with self.store.conn() as c:
            c.executemany(
                """INSERT INTO feature_observation_events
                   (event_key,contract_version,entity_id,event_time,received_time,state,
                    attributes_json,source,quality)
                   VALUES(?,1,?,?,?,?,?,?,1)""",
                [
                    (
                        f"row-{i}",
                        self.eid,
                        self.base + i,
                        self.base + i,
                        "off",
                        "{}",
                        "test",
                    )
                    for i in range(4096)
                ],
            )
        tracker = ObservationSQLiteTemporalTracker(
            self.store, [self.eid], self.ctx, self.base, self.base + 5000
        )
        try:
            plan = tracker.conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT event_key FROM feature_observation_events
                   WHERE entity_id=? AND received_time>? AND received_time<=?
                     AND event_time<=?
                   ORDER BY event_time DESC,received_time DESC,event_key DESC LIMIT ?""",
                (self.eid, self.base + 4500, self.base + 4501, self.base + 4500,
                 tracker.HISTORY_SAMPLES),
            ).fetchall()
            text = " ".join(str(tuple(row)) for row in plan)
            self.assertIn("idx_feature_obs_entity_received_time", text)
            self.assertEqual(
                tracker._feature_interval_rows(
                    [self.eid], self.base + 4500, self.base + 4501
                ),
                [],
            )
        finally:
            tracker.close()


if __name__ == "__main__":
    unittest.main()
