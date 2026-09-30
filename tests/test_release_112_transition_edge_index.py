"""0.14.112 causal transition-edge index parity and fallback regressions."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from observation_contract import (
    FeatureJournal,
    ObservationSQLiteTemporalTracker,
)
from replay import RAMReplayIndex
from settings import DEFAULT_OPTIONS
from storage import Store
from training_process import training_options_fingerprint


def context_stub():
    return SimpleNamespace(options={}, relevant_entities=lambda: [])


def ha_state(value, *, device_class="occupancy"):
    return {
        "state": str(value),
        "attributes": {"device_class": device_class},
    }


class TransitionEdgeIndexParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hm-edge-index-")
        self.store = Store(Path(self.tmp.name) / "adaptive_ai.db")
        self.journal = FeatureJournal(self.store)
        self.entity = "binary_sensor.presence"
        self.start = 0.0
        self.end = 80.0

        # Base archive contains the durable long-history view.
        self.store.archive_batch([
            (self.entity, 0.0, "off", {"device_class": "occupancy"}, None, "test", 0.0),
            (self.entity, 10.0, "on", {"device_class": "occupancy"}, None, "test", 10.0),
            (self.entity, 20.0, "off", {"device_class": "occupancy"}, None, "test", 20.0),
            (self.entity, 30.0, "on", {"device_class": "occupancy"}, None, "test", 30.0),
            (self.entity, 40.0, "off", {"device_class": "occupancy"}, None, "test", 40.0),
            (self.entity, 55.0, "on", {"device_class": "occupancy"}, None, "test", 55.0),
        ])

        # Feature journal contains a late observation and a same-event confirmation.
        self.journal.record_batch([
            {
                "entity_id": self.entity,
                "state": ha_state("off"),
                "event_time": 12.0,
                "received_time": 35.0,
                "source": "ha_state_changed",
                "event_key": "late-12",
            },
            {
                "entity_id": self.entity,
                "state": ha_state("on"),
                "event_time": 30.0,
                "received_time": 30.5,
                "source": "ha_state_changed",
                "event_key": "confirm-30",
            },
            {
                "entity_id": self.entity,
                "state": ha_state("off"),
                "event_time": 47.0,
                "received_time": 47.2,
                "source": "ha_state_changed",
                "event_key": "feature-47",
            },
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def build_ram_index(self):
        connection = sqlite3.connect(self.store.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            return RAMReplayIndex.build(
                connection,
                [self.entity],
                self.start,
                self.end,
                max_bytes=8 * 1024 * 1024,
            )
        finally:
            connection.close()

    def build_edge_index(self, ram_index=None, max_rows_per_entity=None):
        return ObservationSQLiteTemporalTracker.build_transition_edge_index(
            self.store,
            [self.entity],
            self.start,
            self.end,
            ram_replay_index=ram_index,
            max_rows_per_entity=max_rows_per_entity,
        )

    def trackers(self, *, ram_index=None, edge_index=None):
        old = ObservationSQLiteTemporalTracker(
            self.store,
            [self.entity],
            context_stub(),
            self.start,
            self.end,
            ram_replay_index=ram_index,
        )
        indexed = ObservationSQLiteTemporalTracker(
            self.store,
            [self.entity],
            context_stub(),
            self.start,
            self.end,
            ram_replay_index=ram_index,
            transition_edge_index=edge_index,
        )
        return old, indexed

    def assert_query_parity(self, old, indexed, at_ts, window):
        for positive in (False, True):
            self.assertEqual(
                old.directional_transition_before(
                    self.entity, at_ts, positive, window
                ),
                indexed.directional_transition_before(
                    self.entity, at_ts, positive, window
                ),
                msg=("before", at_ts, window, positive),
            )
            start = float(at_ts) - float(window)
            self.assertEqual(
                old.first_directional_transition_after(
                    self.entity, start, at_ts, positive
                ),
                indexed.first_directional_transition_after(
                    self.entity, start, at_ts, positive
                ),
                msg=("after", at_ts, window, positive),
            )

    def test_exact_parity_across_late_receipt_forward_and_rewind_queries(self):
        ram_index = self.build_ram_index()
        edge_index = self.build_edge_index(ram_index)
        self.assertEqual(edge_index.status()["fallback_entities"], 0)

        old, indexed = self.trackers(
            ram_index=ram_index, edge_index=edge_index
        )
        try:
            for at_ts, window in (
                (15.0, 12.0),
                (25.0, 20.0),
                (34.0, 30.0),  # late event at 12 is not visible yet
                (36.0, 30.0),  # same event becomes causally visible
                (50.0, 40.0),
                (70.0, 30.0),
                (28.0, 24.0),  # explicit rewind after later queries
                (70.0, 60.0),
            ):
                self.assert_query_parity(old, indexed, at_ts, window)

            stats = indexed.stats()
            self.assertGreater(stats["transition_edge_index_hits"], 0)
            self.assertEqual(stats["transition_edge_index_fallbacks"], 0)
            self.assertGreater(stats["transition_edge_cursor_rewinds"], 0)
            self.assertGreater(
                stats["transition_edge_scan_rows_avoided_estimate"], 0
            )
        finally:
            old.close()
            indexed.close()

    def test_invalid_boundary_suppresses_first_transition_exactly(self):
        entity = "binary_sensor.boundary"
        self.store.archive_batch([
            (entity, 0.0, "off", {"device_class": "occupancy"}, None, "test", 0.0),
            (entity, 5.0, "unknown", {"device_class": "occupancy"}, None, "test", 5.0),
            (entity, 7.0, "on", {"device_class": "occupancy"}, None, "test", 7.0),
            (entity, 9.0, "off", {"device_class": "occupancy"}, None, "test", 9.0),
        ])
        edge_index = ObservationSQLiteTemporalTracker.build_transition_edge_index(
            self.store, [entity], 0.0, 20.0
        )
        old = ObservationSQLiteTemporalTracker(
            self.store, [entity], context_stub(), 0.0, 20.0
        )
        indexed = ObservationSQLiteTemporalTracker(
            self.store, [entity], context_stub(), 0.0, 20.0,
            transition_edge_index=edge_index,
        )
        try:
            # Window begins after the invalid sample. The ON at t=7 establishes state
            # but must not be reported as a transition from the earlier OFF at t=0.
            self.assertIsNone(
                old.directional_transition_before(entity, 8.0, True, 2.0)
            )
            self.assertEqual(
                old.directional_transition_before(entity, 8.0, True, 2.0),
                indexed.directional_transition_before(entity, 8.0, True, 2.0),
            )
            self.assertEqual(
                old.first_directional_transition_after(entity, 6.0, 10.0, False),
                indexed.first_directional_transition_after(entity, 6.0, 10.0, False),
            )
        finally:
            old.close()
            indexed.close()

    def test_source_bound_falls_back_to_legacy_path(self):
        entity = "binary_sensor.chatty"
        rows = []
        for i in range(700):
            rows.append((
                entity, float(i),
                "on" if i % 2 else "off",
                {"device_class": "occupancy"}, None, "test", float(i),
            ))
        self.store.archive_batch(rows)
        edge_index = ObservationSQLiteTemporalTracker.build_transition_edge_index(
            self.store, [entity], 0.0, 700.0, max_rows_per_entity=512
        )
        self.assertEqual(edge_index.status()["indexed_entities"], 0)
        self.assertEqual(edge_index.status()["fallback_entities"], 1)

        old = ObservationSQLiteTemporalTracker(
            self.store, [entity], context_stub(), 0.0, 700.0
        )
        indexed = ObservationSQLiteTemporalTracker(
            self.store, [entity], context_stub(), 0.0, 700.0,
            transition_edge_index=edge_index,
        )
        try:
            expected = old.directional_transition_before(
                entity, 699.0, True, 120.0
            )
            actual = indexed.directional_transition_before(
                entity, 699.0, True, 120.0
            )
            self.assertEqual(expected, actual)
            self.assertGreater(
                indexed.stats()["transition_edge_index_fallbacks"], 0
            )
        finally:
            old.close()
            indexed.close()


class TransitionEdgeOptionContractTests(unittest.TestCase):
    def test_edge_index_bound_is_exposed_and_nonsemantic(self):
        root = Path(__file__).resolve().parents[1]
        config = (root / "adaptive_ai" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn(
            "training_transition_edge_max_rows_per_entity: 65536", config
        )
        self.assertIn(
            'training_transition_edge_max_rows_per_entity: "int(512,262144)"',
            config,
        )

        base = dict(DEFAULT_OPTIONS)
        changed = dict(base)
        changed["training_transition_edge_max_rows_per_entity"] = 4096
        self.assertEqual(
            training_options_fingerprint(base),
            training_options_fingerprint(changed),
        )


if __name__ == "__main__":
    unittest.main()
