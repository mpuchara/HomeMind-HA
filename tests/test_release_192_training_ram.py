"""Resource sharing and dense transition-index parity with the legacy reader."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import support
from settings import DEFAULT_OPTIONS
from training_process import parallel_profile_options, resolve_training_resource_profile, training_options_fingerprint
from observation_contract import ObservationTransitionEdgeIndex, ObservationSQLiteTemporalTracker, FeatureJournal
from storage import Store


class TrainingRam192Tests(unittest.TestCase):
    def test_logged_eight_gib_host_uses_more_index_ram_inside_existing_ceiling(self):
        memory = {'total_mb': 7806.3, 'available_mb': 5788.1}
        options = parallel_profile_options(DEFAULT_OPTIONS, 2, memory)
        profile = resolve_training_resource_profile(options, memory)
        self.assertEqual(profile['effective_memory_limit_mb'], 768)
        self.assertEqual(profile['worker_options']['training_worker_effective_ram_replay_index_mb'], 192)
        self.assertEqual(profile['worker_options']['training_worker_effective_transition_edge_max_rows_per_entity'], 262144)
        self.assertLessEqual(2 * profile['effective_memory_limit_mb'] + 512, memory['available_mb'])
        combined = dict(DEFAULT_OPTIONS, **profile['worker_options'])
        self.assertEqual(training_options_fingerprint(combined), training_options_fingerprint(DEFAULT_OPTIONS))

    def test_custom_ceiling_and_transition_limit_are_preserved(self):
        custom = dict(DEFAULT_OPTIONS, training_parallel_worker_memory_limit_mb=640,
                      training_transition_edge_max_rows_per_entity=20000)
        memory = {'total_mb': 7806., 'available_mb': 5788.}
        profile = resolve_training_resource_profile(parallel_profile_options(custom, 2, memory), memory)
        self.assertEqual(profile['effective_memory_limit_mb'], 640)
        self.assertEqual(profile['worker_options']['training_worker_effective_transition_edge_max_rows_per_entity'], 20000)

    def test_parallel_share_reserves_parent_space_before_worker_start(self):
        custom = dict(DEFAULT_OPTIONS, training_worker_memory_limit_mb=2048,
                      training_parallel_worker_memory_limit_mb=1024)
        memory = {'total_mb': 3900., 'available_mb': 2300.}
        profile = resolve_training_resource_profile(parallel_profile_options(custom, 2, memory), memory)
        self.assertLessEqual(2 * profile['effective_memory_limit_mb'] + 512, memory['available_mb'])
        self.assertEqual(profile['worker_options']['training_worker_effective_transition_edge_max_rows_per_entity'], 262144)

    def test_low_memory_profile_keeps_old_small_cache_limits(self):
        profile = resolve_training_resource_profile(DEFAULT_OPTIONS, {'total_mb': 1024., 'available_mb': 600.})
        self.assertEqual(profile['effective_memory_limit_mb'], 256)
        self.assertEqual(profile['worker_options']['training_worker_effective_ram_replay_index_mb'], 24)
        self.assertEqual(profile['worker_options']['training_worker_effective_transition_edge_max_rows_per_entity'], 65536)

    def test_dense_source_admitted_to_ram_matches_legacy_transition_queries(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'dense.db')
            FeatureJournal(store)
            eid, start, count = 'binary_sensor.dense', 1791900000., 66000
            for lo in range(0, count, 2000):
                store.archive_batch([(eid, start + i / 100., 'on' if (i // 37) % 2 else 'off',
                                      {'device_class': 'occupancy'}, None, 'fixture', start + i / 100.)
                                     for i in range(lo, min(count, lo + 2000))])
            end = start + count / 100.
            old_index = ObservationTransitionEdgeIndex.build(store, [eid], start, end, max_rows_per_entity=65536)
            new_index = ObservationTransitionEdgeIndex.build(store, [eid], start, end, max_rows_per_entity=262144)
            self.assertEqual(old_index.status()['indexed_entities'], 0)
            self.assertEqual(new_index.status()['indexed_entities'], 1)
            context = SimpleNamespace(options={}, relevant_entities=lambda: [])
            old = ObservationSQLiteTemporalTracker(store, [eid], context, start, end)
            new = ObservationSQLiteTemporalTracker(store, [eid], context, start, end, transition_edge_index=new_index)
            try:
                for offset in (650., 655., 652., 659.):
                    at = start + offset
                    for positive in (True, False):
                        self.assertEqual(old.directional_transition_before(eid, at, positive, 2.),
                                         new.directional_transition_before(eid, at, positive, 2.))
                        self.assertEqual(old.first_directional_transition_after(eid, at - 2., at, positive),
                                         new.first_directional_transition_after(eid, at - 2., at, positive))
                self.assertGreater(new.stats()['transition_edge_index_hits'], 0)
            finally:
                old.close()
                new.close()


if __name__ == '__main__':
    unittest.main()
