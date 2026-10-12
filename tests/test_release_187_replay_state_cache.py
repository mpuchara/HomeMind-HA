import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import state
from context import archived_state
from context_engine import ContextEngine
from observation_contract import FeatureJournal, ObservationSQLiteTemporalTracker
from replay import ReplayStateCache
from settings import DEFAULT_OPTIONS
from storage import Store


class ReplayStateCacheTests(unittest.TestCase):
    def row(self, **changes):
        return dict(entity_id="sensor.radar_still_energy", ts=1700000000.0,
                    state="24", attributes_json='{"unit_of_measurement":"%"}',
                    context_user_id=None, id=1, source="live") | changes

    def test_repeated_sample_is_decoded_once(self):
        cache = ReplayStateCache()
        row = self.row()
        with patch("replay.archived_state", wraps=archived_state) as decode:
            for _ in range(100):
                self.assertEqual(cache.state(dict(row)), archived_state(row))
            self.assertEqual(decode.call_count, 1)
        self.assertEqual(cache.status()["hits"], 99)

    def test_same_id_corrections_and_receipt_metadata_never_reuse_old_state(self):
        cache = ReplayStateCache()
        original = self.row()
        variants = [original, self.row(state="8"), self.row(ts=1700000001.0),
                    self.row(context_user_id="manual-user"),
                    self.row(entity_id="sensor.other"),
                    self.row(attributes_json=json.dumps({"__hm_received_time": 1700000035.0})),
                    self.row(attributes_json="invalid JSON")]
        for row in variants:
            self.assertEqual(cache.state(row), archived_state(row))
        self.assertEqual(cache.status()["misses"], len(variants))

    def test_entry_and_payload_limits_and_large_attribute_bypass(self):
        cache = ReplayStateCache(max_entries=3, max_units=5000)
        for i in range(200):
            row = self.row(state=str(i))
            self.assertEqual(cache.state(row), archived_state(row))
            self.assertLessEqual(cache.status()["entries"], 3)
            self.assertLessEqual(cache.status()["units"], 5000)
        before = cache.status()["entries"]
        large = self.row(attributes_json=json.dumps({"payload": "x" * 50000}))
        self.assertEqual(cache.state(large), archived_state(large))
        self.assertEqual(cache.status()["entries"], before)
        self.assertGreater(cache.status()["evictions"], 0)
        cache.clear()
        self.assertEqual(cache.status()["units"], 0)
        self.assertEqual(cache.status()["entries"], 0)

    def test_disabled_cache_preserves_decode_behavior(self):
        cache = ReplayStateCache(max_entries=0)
        with patch("replay.archived_state", wraps=archived_state) as decode:
            for _ in range(3):
                cache.state(self.row())
            self.assertEqual(decode.call_count, 3)
        self.assertEqual(cache.status()["entries"], 0)

    def test_causal_tracker_and_room_belief_parity_with_late_journal_and_rewind(self):
        base = 1700000000.0
        eid = "binary_sensor.bathroom_presence"
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "history.db")
            attrs = {"device_class": "occupancy"}
            store.archive_batch([(eid, base + i, "on" if 10 <= i < 80 else "off",
                                  attrs, None, "live", base + i) for i in range(120)])
            journal = FeatureJournal(store, clock=lambda: base + 150)
            journal.record(eid, state(eid, "off", **attrs), event_time=base + 20,
                           received_time=base + 55, source="live", event_key="late")
            registry = {eid: {"area_id": "bathroom"}}
            def context():
                ctx = ContextEngine(DEFAULT_OPTIONS)
                ctx.configure({eid: state(eid, "off", **attrs)}, entities=registry)
                return ctx
            cached = ObservationSQLiteTemporalTracker(store, [eid], context(), base, base + 150)
            reference = ObservationSQLiteTemporalTracker(store, [eid], context(), base, base + 150)
            reference.state_cache = ReplayStateCache(max_entries=0)
            try:
                for offset in (9, 22, 54, 56, 85, 115, 22, 56, 115):
                    cached.advance(base + offset)
                    reference.advance(base + offset)
                    self.assertEqual(cached.state_map, reference.state_map)
                    self.assertEqual(list(cached.history.samples[eid]), list(reference.history.samples[eid]))
                    self.assertEqual(cached.home_view.home.forecast("bathroom", base + offset),
                                     reference.home_view.home.forecast("bathroom", base + offset))
                self.assertGreater(cached.state_cache.hits, 0)
                self.assertLess(cached.state_cache.misses, reference.state_cache.misses)
                self.assertLessEqual(len(cached.history.samples[eid]), 64)
            finally:
                cached.close()
                reference.close()
            self.assertEqual(cached.state_cache.status()["entries"], 0)


if __name__ == "__main__":
    unittest.main()
