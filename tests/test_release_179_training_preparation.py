"""Active-session UI and durable Recorder preparation progress regressions."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import ROOT
import history
from storage import Store
from runtime_debug_log import RuntimeDebugLogService
from tools.benchmark_history_import import payload, legacy_rows


class TrainingPreparation179Tests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="hm-preparation-")
        self.addCleanup(scratch.cleanup)
        self.store = Store(Path(scratch.name) / "archive.db")
        patcher = patch.object(history, "STORE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = SimpleNamespace(lock=threading.RLock(), state_map={}, entity_registry={})
        self.manager = self.session("a")

    def session(self, aid):
        session = history.HistoryManager(self.engine, session_mode=True)
        session.phase = "training"
        session.training_job_agent_id = aid
        session.training_job_name = aid.upper()
        session.training_job_started_at = history.now_ts() - 100
        session.cycle_started_at = session.training_job_started_at
        session.training_replay_progress_mapped = False
        session.job_cancel_event = threading.Event()
        return session

    def test_hot_status_replaces_stale_startup_fields_without_database_queries(self):
        parent = self.session("scheduler")
        parent.phase_detail = "Saved agents + realtime only"
        parent.stage_eta_seconds = 1
        parent.training_sessions = {"a": self.manager}
        self.manager.set_status(progress=.07, message="Importing fast context", work_done=500,
            work_total=1000, work_unit="Recorder response rows", stage_eta_seconds=23,
            phase_detail="Recorder import", eta_source="measured chunk throughput")
        with patch.object(self.store, "conn", side_effect=AssertionError("hot status must be RAM only")):
            result = parent.status()
        self.assertEqual(result["phase_detail"], "Recorder import")
        self.assertEqual(result["message"], "Importing fast context")
        self.assertEqual(result["training_overall_progress"], .07)
        self.assertEqual(result["training_stage_progress"], .5)
        self.assertEqual(result["training_stage_eta_seconds"], 23)
        self.assertGreaterEqual(result["training_elapsed_seconds"], 100)
        self.assertIsNone(result["training_overall_eta_seconds"])

    def test_parallel_sessions_keep_independent_stages_and_aggregate_progress(self):
        other = self.session("b")
        self.manager.set_status(progress=.06, message="Importing A", work_done=6, work_total=10)
        other.set_status(progress=.5, message="Replaying B", work_done=40, work_total=100)
        parent = self.session("scheduler")
        parent.training_sessions = {"a": self.manager, "b": other}
        result = parent.status()
        self.assertAlmostEqual(result["training_overall_progress"], .28)
        self.assertEqual(result["message"], "Importing A")
        rows = result["parallel_training"]["agents"]
        self.assertEqual([r["status"]["work_done"] for r in rows], [6, 40])
        self.assertEqual([r["message"] for r in rows], ["Importing A", "Replaying B"])

    def test_large_response_reports_committed_prefix_before_request_completes(self):
        snapshots = []
        original = self.manager.set_status
        def capture(*args, **kwargs):
            original(*args, **kwargs)
            snapshots.append((self.manager.status(), self.store.archive_count()))
        ticks = iter(range(10000))
        data = payload(100)
        with patch.object(self.manager, "set_status", side_effect=capture), \
                patch.object(history.HA, "history", return_value=data), \
                patch.object(history.time, "monotonic", side_effect=lambda: next(ticks)), \
                patch.object(history.TRAINING_BUDGET, "checkpoint", return_value=0):
            count = self.manager._import_section(["sensor.room_0"], 1700000000, 1700000100,
                batch_size=1, minimal=False, no_attributes=False, source="ha_history_fast_context",
                progress_lo=0, progress_hi=.1, label="Fast context", max_hours=6)
        self.assertEqual(count, 400)
        prefix = [(s, n) for s, n in snapshots if 0 < n < count]
        self.assertTrue(prefix)
        self.assertTrue(all(0 < s["progress"] < .1 for s, _ in prefix))
        self.assertEqual(prefix[0][0]["work_done"], 128)
        self.assertEqual(prefix[0][0]["work_total"], 400)
        self.assertIn("128 archive rows committed", prefix[0][0]["phase_detail"])
        self.assertAlmostEqual(snapshots[-1][0]["progress"], .1)
        self.assertIsNone(self.manager._recorder_chunk_report)
        actual = list(self.store.archive_iter())
        self.assertEqual(len(actual), len(legacy_rows(data, "ha_history_fast_context")))

    def test_cancel_keeps_committed_rows_and_removes_progress_callback(self):
        original = self.manager.set_status
        def cancel_on_commit(*args, **kwargs):
            original(*args, **kwargs)
            if "archive rows committed" in str(kwargs.get("phase_detail", "")):
                self.manager.job_cancel_event.set()
        ticks = iter(range(10000))
        with patch.object(self.manager, "set_status", side_effect=cancel_on_commit), \
                patch.object(history.HA, "history", return_value=payload(100)), \
                patch.object(history.time, "monotonic", side_effect=lambda: next(ticks)), \
                patch.object(history.TRAINING_BUDGET, "checkpoint", return_value=0):
            with self.assertRaises(InterruptedError):
                self.manager._import_section(["sensor.room_0"], 1700000000, 1700000100,
                    batch_size=1, minimal=False, no_attributes=False, source="ha_history_full",
                    progress_lo=0, progress_hi=.1, label="Target", max_hours=6)
        self.assertEqual(self.store.archive_count(), 128)
        self.assertIsNone(self.manager._recorder_chunk_report)

    def test_resume_stage_boundary_and_final_progress_preserve_replay_cursor_scale(self):
        self.manager.training_job_start_progress = .6
        self.manager.set_status(progress=.64)
        self.manager.training_replay_progress_mapped = True
        self.manager.set_status(progress=.6)
        self.assertAlmostEqual(self.manager.progress, .64)
        self.manager.set_status(progress=.8)
        self.assertAlmostEqual(self.manager.progress, .82)
        self.manager.set_status(progress=1)
        self.assertEqual(self.manager.progress, 1)

    def test_preparation_all_sections_progress_and_cached_repeat_make_no_ha_requests(self):
        self.manager._eligible_rebuild_context = lambda: ["sensor.fast", "sensor.slow"]
        self.manager.training_schema_seed = lambda *args: None
        data = payload(5)
        with patch.object(history.HA, "history", return_value=data) as ha, \
                patch.object(history, "entity_capability_tags", side_effect=lambda eid, st: {"activity"} if eid == "sensor.fast" else set()), \
                patch.object(history.TRAINING_BUDGET, "checkpoint", return_value=0):
            self.manager._refresh_agent_history({"id": "a", "target_entity": "switch.target"},
                1700000000, 1700000100, rebuild=True)
            self.assertEqual(ha.call_count, 3)
            self.assertAlmostEqual(self.manager.progress, .1)
            self.assertEqual(self.manager.work_done, self.manager.work_total)
            self.manager._refresh_agent_history({"id": "a", "target_entity": "switch.target"},
                1700000000, 1700000100, rebuild=True)
            self.assertEqual(ha.call_count, 3)
        self.assertEqual(len(self.manager.training_recorder_coverage), 3)

    def test_debug_export_contains_live_history_stage_when_tracing_is_disabled(self):
        parent = self.session("scheduler")
        parent.training_sessions = {"a": self.manager}
        self.manager.set_status(progress=.04, work_done=128, work_total=2000,
            phase_detail="Writing Recorder history")
        service = RuntimeDebugLogService(SimpleNamespace(HISTORY=parent), SimpleNamespace())
        result = service.export_payload()
        self.assertEqual(result["history"]["work_done"], 128)
        self.assertEqual(result["history"]["training_job_agent_id"], "a")
        self.assertEqual(result["history"]["training_overall_progress"], .04)


if __name__ == "__main__":
    unittest.main()
