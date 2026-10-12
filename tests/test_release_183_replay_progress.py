import copy
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import support
import history
from causal_home_statistics import CausalHomeStatistics
from training_process import _apply_status, _worker_status_writer
from tools.benchmark_causal_statistics import fixture, legacy_statistics_at


class CausalPrefixTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.context, self.base, self.end = fixture(24)
        self.addCleanup(self.conn.close)
        self.index = CausalHomeStatistics(self.conn, self.context, self.base, self.end)

    def compare(self, ts, cursor='default'):
        reference = legacy_statistics_at(self.index, ts, {'rows': 0})
        actual = self.index.statistics_at(ts, cursor=cursor)
        self.assertEqual(actual, reference)
        return actual

    def test_interleaved_cursors_checkpoint_crossings_and_rewinds_match_legacy(self):
        for offset in [1, 11, 12, 14, 39, 40, 41, 49, 95, 250, 290, 301, 303, 370, 399,
                       11, 400, 405, 700, 710, 701, 960, 970, 1000, 1060]:
            self.compare(self.base + offset, 'onset')
            self.compare(self.base + max(0, offset - 1), 'persistence')
        self.assertGreater(self.index.cursor_hits, 0)
        self.assertLessEqual(len(self.index._cursors), 2)

    def test_query_expiry_never_teaches_the_next_prefix_and_outputs_are_private(self):
        for offset in (11, 41, 42, 49, 50, 51, 52, 54, 80):
            actual = self.compare(self.base + offset)
            actual[0].clear()
            actual[1].clear()
            actual[2]['count'] = 999999
            self.compare(self.base + offset)
        self.assertEqual(self.compare(self.base - 1)[0], {})
        with self.assertRaises(ValueError):
            self.index.statistics_at(self.end + 1)

    def test_insert_update_delete_of_visible_history_invalidates_prefix(self):
        self.compare(self.base + 100)
        self.conn.execute("INSERT INTO entity_history(entity_id,ts,received_ts,state,attributes_json) "
                          "VALUES('binary_sensor.room',?,?, 'on','{}')", (self.base + 55, self.base + 55))
        self.compare(self.base + 105)
        self.conn.execute("UPDATE entity_history SET state='off' WHERE ts=?", (self.base + 55,))
        self.compare(self.base + 108)
        self.conn.execute('DELETE FROM entity_history WHERE ts=?', (self.base + 55,))
        self.compare(self.base + 110)

    def test_context_revision_and_half_life_change_invalidate_prefix(self):
        self.compare(self.base + 100)
        restores = self.index.cursor_restores
        self.context.registry_revision += 1
        self.compare(self.base + 110)
        self.context.options['home_model_half_life_days'] = 10
        self.compare(self.base + 115)
        self.assertEqual(self.index.cursor_restores, restores + 2)

    def test_other_connection_commit_invalidates_visible_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.db'
            reader = sqlite3.connect(path)
            reader.row_factory = sqlite3.Row
            self.conn.backup(reader)
            writer = sqlite3.connect(path)
            try:
                index = CausalHomeStatistics(reader, self.context, self.base, self.end)
                index.statistics_at(self.base + 100)
                writer.execute("INSERT INTO entity_history(entity_id,ts,received_ts,state,attributes_json) "
                               "VALUES('binary_sensor.room',?,?, 'on','{}')", (self.base + 55, self.base + 55))
                writer.commit()
                self.assertEqual(index.statistics_at(self.base + 105),
                                 legacy_statistics_at(index, self.base + 105, {'rows': 0}))
                self.assertEqual(index.cursor_restores, 2)
            finally:
                writer.close()
                reader.close()

    def test_additional_cursor_evicts_oldest_without_unbounded_models(self):
        for cursor in ('one', 'two', 'three'):
            self.compare(self.base + 100, cursor)
        self.assertEqual(list(self.index._cursors), ['two', 'three'])
        self.compare(self.base + 105, 'one')
        self.assertEqual(list(self.index._cursors), ['three', 'one'])

    def test_existing_memory_budget_admits_no_cursor_when_snapshots_fill_it(self):
        self.index.max_bytes = self.index.bytes
        for offset in (10, 11, 20, 21, 200):
            self.compare(self.base + offset)
        self.assertEqual(self.index.status()['cursor_bytes'], 0)
        self.assertEqual(self.index.cursor_hits, 0)

    def test_dense_queries_reduce_replayed_work_and_keep_bounded_memory(self):
        counts = {'rows': 0}
        for offset in range(1, 270):
            for cursor in ('onset', 'persistence'):
                expected = legacy_statistics_at(self.index, self.base + offset, counts)
                self.assertEqual(self.index.statistics_at(self.base + offset, cursor=cursor), expected)
        status = self.index.status()
        self.assertLess(status['query_rows'], counts['rows'] / 10)
        self.assertLessEqual(status['compressed_bytes'] + status['cursor_bytes'], status['max_bytes'])


class WorkerProgressTests(unittest.TestCase):
    def manager(self, aid='a'):
        engine = SimpleNamespace(lock=threading.RLock(), state_map={}, entity_registry={})
        manager = history.HistoryManager(engine, session_mode=True)
        manager.training_job_agent_id = aid
        manager.training_job_name = aid
        manager.training_job_started_at = history.now_ts() - 200
        manager.cycle_started_at = manager.training_job_started_at
        manager.training_replay_progress_mapped = True
        manager.training_job_timings = {'recorder_refresh_seconds': 42}
        return manager

    def test_worker_round_trip_keeps_fractional_rate_timings_and_one_progress_mapping(self):
        worker = self.manager()
        worker.training_replay_progress_mapped = False
        worker.training_job_agent_id = None
        worker.training_rows_per_second = .145
        worker.training_phase_timings = {'causal_home_statistics': {'query_rows': 23}}
        worker.temporal_replay_stats = {'totals': {'sql_queries': 8}}
        worker.training_ram_replay_index_status = {'rows': 300}
        _worker_status_writer(worker, 'unused')
        with patch('training_process._atomic_json') as write:
            worker.set_status(phase='training', progress=.0125, work_done=37, work_total=87,
                              stage_eta_seconds=149)
        payload = write.call_args.args[1]
        parent = self.manager()
        started = parent.training_job_started_at
        _apply_status(parent, payload)
        scheduler = self.manager('scheduler')
        scheduler.training_sessions = {'a': parent}
        result = scheduler.status()
        self.assertEqual(result['phase'], 'training')
        self.assertAlmostEqual(result['progress'], .11125)
        self.assertEqual(result['training_rows_per_second'], .145)
        self.assertEqual(result['training_phase_timings'], worker.training_phase_timings)
        self.assertEqual(result['temporal_replay'], worker.temporal_replay_stats)
        self.assertEqual(result['training_ram_replay_index'], {'rows': 300})
        self.assertEqual(result['training_job_timings'], {'recorder_refresh_seconds': 42})
        self.assertEqual(result['eta_seconds'], result['training_overall_eta_seconds'])
        self.assertEqual(result['stage_eta_seconds'], 149)
        self.assertEqual(parent.training_job_started_at, started)
        self.assertEqual(parent.training_process_status['status_updated_at'], payload['updated_at'])

    def test_minimal_bootstrap_does_not_erase_existing_diagnostics_or_parent_identity(self):
        parent = self.manager()
        parent.training_phase_timings = {'screening_seconds': 3}
        _apply_status(parent, {'phase': 'worker_bootstrap', 'progress': 0,
                              'training_job_agent_id': 'other', 'training_replay_progress_mapped': False})
        self.assertEqual(parent.training_phase_timings, {'screening_seconds': 3})
        self.assertEqual(parent.training_job_agent_id, 'a')
        self.assertTrue(parent.training_replay_progress_mapped)

    def test_eta_excludes_recorder_ui_allocation_from_measured_replay_work(self):
        parent = self.manager()
        parent.training_replay_started_at = 1000
        parent.training_job_started_at = 800
        with patch.object(history, 'now_ts', return_value=1100):
            parent.set_status(progress=.01)
        # 1% replay completed in 100 s; preparation's 10% is not extrapolated.
        self.assertAlmostEqual(parent.training_overall_eta_seconds, 9900)

    def test_resume_eta_measures_only_remaining_replay_after_saved_cursor(self):
        parent = self.manager()
        parent.training_job_start_progress = .6
        parent.training_replay_started_at = 1000
        with patch.object(history, 'now_ts', return_value=1100):
            parent.set_status(progress=.64)
        # Of the remaining 40%, 4% was processed, so 900 s remain at this rate.
        self.assertAlmostEqual(parent.training_overall_eta_seconds, 900)


if __name__ == '__main__':
    unittest.main()
