"""0.14.110 target-only single-agent replay contracts."""
import tempfile
import unittest
from pathlib import Path

from storage import Store
from support import ROOT


class TargetOnlyReplayStreamTests(unittest.TestCase):
    def test_target_only_stream_preserves_exact_target_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "adaptive_ai.db")
            rows = []
            # Chatty context rows deliberately dominate the fixture.
            for idx in range(1, 301):
                ts = float(idx)
                rows.append(("sensor.radar", ts, str(idx % 100), {}, None, "live"))
                rows.append(("sensor.lux", ts + 0.01, str(idx * 2), {}, None, "live"))
                if idx in (25, 100, 175, 250):
                    rows.append((
                        "light.target", ts + 0.02,
                        "on" if idx in (25, 175) else "off",
                        {}, None, "live",
                    ))
            store.archive_batch(rows)

            broad = [
                (row["id"], row["entity_id"], row["ts"], row["state"])
                for row in store.archive_iter(
                    0, 400,
                    {"light.target", "sensor.radar", "sensor.lux"},
                    chunk_size=100,
                )
                if row["entity_id"] == "light.target"
            ]
            target_only = [
                (row["id"], row["entity_id"], row["ts"], row["state"])
                for row in store.archive_iter(
                    0, 400, {"light.target"}, chunk_size=100
                )
            ]

            self.assertEqual(target_only, broad)
            self.assertEqual(len(target_only), 4)

    def test_product_replay_driver_is_target_only_but_trackers_keep_context(self):
        source = (ROOT / "adaptive_ai/src/history.py").read_text(encoding="utf-8")
        self.assertIn("replay_entities = set(target_map.keys())", source)
        self.assertIn(
            '"replay_stream_contract": "target_only_driver_v1"', source
        )
        self.assertIn(
            '"context_entities_reconstructed_by_trackers": len(watched_entities)',
            source,
        )
        self.assertIn(
            "SQLiteTemporalTracker(\n            STORE, watched_entities",
            source,
        )

    def test_phase_timing_is_returned_by_persistent_worker(self):
        source = (ROOT / "adaptive_ai/src/training_process.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"training_phase_timings": dict(', source)
        self.assertIn(
            'result.get("training_phase_timings") or {}',
            source,
        )


if __name__ == "__main__":
    unittest.main()
