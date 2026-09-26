import unittest

from history import HistoryManager
from settings import OPTIONS
from support import ROOT


class RecorderCoverageReuseTests(unittest.TestCase):
    def make_manager(self, *, skip=False):
        manager = HistoryManager.__new__(HistoryManager)
        manager.training_recorder_coverage = {}
        manager.training_recorder_coverage_hits = 0
        manager.training_recorder_coverage_misses = 0
        manager.recorder_skipped_slices = 0
        manager.progress = 0.0
        manager.calls = []

        def fake_import(entity_ids, start_ts, end_ts, **kwargs):
            manager.calls.append((tuple(entity_ids), float(start_ts), float(end_ts), kwargs))
            if skip:
                manager.recorder_skipped_slices += 1
            return len(entity_ids)

        manager._import_section = fake_import
        return manager

    def test_second_identical_recorder_refresh_is_skipped_in_ram(self):
        manager = self.make_manager()
        args = dict(
            batch_size=30, minimal=True, no_attributes=True,
            source="ha_history_minimal", label="context", max_hours=12,
        )
        first = manager._training_recorder_import(
            ["sensor.a", "sensor.b"], 0.0, 604800.0, **args
        )
        second = manager._training_recorder_import(
            ["sensor.a", "sensor.b"], 0.0, 604800.0, **args
        )
        self.assertEqual(first, 2)
        self.assertEqual(second, 0)
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(manager.training_recorder_coverage_hits, 2)

    def test_new_tail_fetches_only_overlap_plus_delta(self):
        manager = self.make_manager()
        args = dict(
            batch_size=1, minimal=False, no_attributes=False,
            source="ha_history_full", label="target", max_hours=6,
        )
        manager._training_recorder_import(
            ["light.kitchen"], 0.0, 604800.0, **args
        )
        manager._training_recorder_import(
            ["light.kitchen"], 0.0, 605400.0, **args
        )
        self.assertEqual(len(manager.calls), 2)
        _, start, end, _ = manager.calls[-1]
        expected_overlap = float(
            OPTIONS.get("training_recorder_refresh_overlap_minutes", 30)
        ) * 60.0
        self.assertAlmostEqual(start, 604800.0 - expected_overlap)
        self.assertEqual(end, 605400.0)

    def test_skipped_recorder_slice_is_never_cached_as_complete(self):
        manager = self.make_manager(skip=True)
        args = dict(
            batch_size=1, minimal=False, no_attributes=False,
            source="ha_history_full", label="target", max_hours=6,
        )
        manager._training_recorder_import(
            ["switch.test"], 0.0, 604800.0, **args
        )
        manager._training_recorder_import(
            ["switch.test"], 0.0, 604800.0, **args
        )
        self.assertEqual(len(manager.calls), 2)
        self.assertEqual(manager.training_recorder_coverage, {})


class Release073TrainingThroughputContractTests(unittest.TestCase):
    def test_release_defaults_raise_throughput_without_growing_work_slice(self):
        settings = (ROOT / "adaptive_ai/src/settings.py").read_text(encoding="utf-8")
        config = (ROOT / "adaptive_ai/config.yaml").read_text(encoding="utf-8")
        runtime = (ROOT / "adaptive_ai/src/rpi_low_power_runtime.py").read_text(encoding="utf-8")
        self.assertIn('"training_cpu_duty_cycle": 0.85', settings)
        self.assertIn('"training_max_continuous_work_ms": 35', settings)
        self.assertIn('"training_experience_batch_rows": 128', settings)
        self.assertIn('"training_replay_ram_cache_rows": 65536', settings)
        self.assertIn("training_cpu_duty_cycle: 0.85", config)
        self.assertIn("training_experience_batch_rows: 128", config)
        self.assertIn("training_replay_ram_cache_rows: 65536", config)
        self.assertIn("DEFAULT_TRAINING_DUTY_CYCLE = 0.85", runtime)
        self.assertIn("DEFAULT_MAX_CONTINUOUS_WORK_MS = 35", runtime)

    def test_rebuild_refresh_routes_through_recorder_coverage_cache(self):
        source = (ROOT / "adaptive_ai/src/history.py").read_text(encoding="utf-8")
        block = source.split("    def _refresh_agent_history", 1)[1].split(
            "    def _run_agent_indexing", 1
        )[0]
        self.assertIn("self._training_recorder_import(", block)
        self.assertNotIn("self._import_section(", block)
        self.assertIn("training_recorder_coverage", source)
        self.assertIn("recorder_skipped_slices", source)


if __name__ == "__main__":
    unittest.main()
