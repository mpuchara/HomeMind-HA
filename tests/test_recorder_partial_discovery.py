"""Recorder discovery regression tests with injected REST responses and real local storage."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from support import agent, state
from test_release_235_recorder_discovery import manager
import history
from storage import Store

START = 1_793_836_800.0
END = START + 21600
BAD = "light.bad"
GOOD = "light.good"
SOURCE = "ha_history_full"


class PartialRecorderDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.obj = manager()
        del self.obj._fetch_history_resilient
        del self.obj.auto_discover_agents
        self.obj.engine = SimpleNamespace(
            registry_entry=lambda eid: {}, lock=threading.RLock(),
            state_map={}, entity_registry={},
        )
        self.current = {eid: state(eid, "off") for eid in (BAD, GOOD)}
        tmp = self.enterContext(TemporaryDirectory())
        self.store = Store(Path(tmp) / "archive.db")
        self.enterContext(patch.object(history, "STORE", self.store))
        self.enterContext(patch.object(history, "HEAVY_JOBS", SimpleNamespace(owner="discovery")))
        self.enterContext(patch.object(history, "now_ts", return_value=END))
        self.enterContext(patch.object(history, "rss_mb", return_value=0))
        self.enterContext(patch.object(history.AUTOMATION_KNOWLEDGE, "hints_for_target", return_value=([], [])))
        self.enterContext(patch.dict(history.OPTIONS, history_bootstrap_days=0.25, history_background_pause_ms=0))
        self.requests = []
        self.bad_ranges = [(START + 1350, START + 2700)]
        self.recordings = {BAD: [(START + 100, "off"), (START + 3000, "on")],
                           GOOD: [(START + 100, "off"), (START + 500, "on")]}
        self.all_fail = False
        self.enterContext(patch.object(history.HA, "history", side_effect=self.history_response))

    def history_response(self, ids, start, end, **kwargs):
        lo = datetime.fromisoformat(start).timestamp()
        hi = datetime.fromisoformat(end).timestamp()
        self.requests.append((tuple(ids), lo, hi))
        if self.all_fail or (BAD in ids and any(lo < rhi and hi > rlo and lo + 1e-5 < rhi
                                               for rlo, rhi in self.bad_ranges)):
            raise HTTPError("http://ha/secret", 500, "opaque", {}, None)
        return [[
            {"entity_id": eid, "last_changed": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
             "state": value}
            for ts, value in self.recordings[eid] if lo < ts < hi
        ] for eid in ids]

    def discover(self):
        self.obj._manual_lightweight_cycle(self.current, list(self.current), END, threshold_override=1)
        return self.obj.discovery_recorder_report

    def test_local_gap_leaves_partial_and_creates_agent_from_verified_transitions(self):
        report = self.discover()
        self.assertEqual(report["status"], "PARTIAL")
        self.assertTrue(self.obj.discovery_classified)
        self.assertEqual(report["coverage_pct"], {BAD: 93.75, GOOD: 100.0})
        self.assertEqual(report["skipped"], [{
            "source": SOURCE, "entity_id": BAD, "start": START + 1350,
            "end": START + 2700, "reason": "SKIPPED_HTTP500",
        }])
        self.assertEqual(self.obj._discovery_missing_intervals(SOURCE, BAD, START, END),
                         [(START + 1350, START + 2700)])
        self.assertFalse(self.store.meta_get("initial_discovery_complete"))
        self.assertFalse(self.store.meta_get("discovery_deep_history_complete"))
        self.assertEqual([a["target_entity"] for a in self.store.list_agent_configs()], [GOOD])
        self.assertEqual(self.obj.discovery_reason_counts["history_incomplete"], 1)
        self.assertEqual([(r["ts"], r["state"]) for r in self.store.archive_iter(entity_ids=[BAD])],
                         self.recordings[BAD])
        self.assertLess(len(self.requests), 20)

    def test_separated_gaps_survive_and_successful_rescan_resolves_them(self):
        self.bad_ranges.append((START + 5400, START + 6750))
        report = self.discover()
        self.assertEqual(len(report["skipped"]), 2)
        self.assertEqual(report["coverage_pct"][BAD], 87.5)
        self.assertEqual(self.obj._read_discovery_metadata(self.obj.DISCOVERY_GAPS_KEY, []), report["skipped"])
        self.bad_ranges.clear()
        self.requests.clear()
        self.assertEqual(self.discover()["status"], "COMPLETE")
        self.assertEqual(self.obj._read_discovery_metadata(self.obj.DISCOVERY_GAPS_KEY, []), [])
        self.assertTrue(self.store.meta_get("initial_discovery_complete"))
        self.assertEqual(len(self.requests), 2)

    def test_all_500_reports_unavailable_without_labeling_gaps(self):
        self.all_fail = True
        with self.assertRaisesRegex(history.DiscoveryRecorderUnavailable, "RECORDER_UNAVAILABLE"):
            self.discover()
        self.assertEqual(self.obj.discovery_recorder_report["status"], "BLOCKED")
        self.assertEqual(self.obj.discovery_recorder_report["skipped"], [])
        self.assertFalse(self.store.meta_get("initial_discovery_complete"))
        self.assertEqual(len(self.store.list_agent_configs()), 0)
        self.assertLessEqual(len(self.requests), 8)

    def test_entity_budget_preserves_healthy_sibling_and_distinguishes_unprobed(self):
        self.bad_ranges = [(START - 3600, END)]
        with patch.object(history, "DISCOVERY_HTTP_500_ENTITY_LIMIT", 7):
            report = self.discover()
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["coverage_pct"], {BAD: 0.0, GOOD: 100.0})
        self.assertIn("UNPROBED_HTTP500_BUDGET", {row["reason"] for row in report["skipped"]})
        self.assertEqual([a["target_entity"] for a in self.store.list_agent_configs()], [GOOD])
        self.assertLessEqual(len(self.requests), 16)

    def test_affected_agent_cannot_prepare_replay_across_known_gap(self):
        self.discover()
        self.obj._training_recorder_import = Mock()
        self.obj._eligible_rebuild_context = lambda: []
        self.obj.training_schema_seed = lambda *args: None
        with self.assertRaisesRegex(history.DiscoveryRecorderIncomplete, "Additional Recorder history"):
            self.obj._refresh_agent_history(
                {"id": "test", "target_entity": BAD}, START + 3600, END,
                rebuild=True, replay_start=START,
            )
        self.obj._training_recorder_import.assert_not_called()
        self.obj._require_agent_history({GOOD}, START, END)
        self.obj._require_agent_history({BAD}, START + 3600, END)

    def test_fatal_preparation_errors_stop_rebuild_before_learning_reset(self):
        config = self.store.create_agent(agent(target_entity=GOOD))
        agent_id = config["id"]
        self.store.save_model(agent_id, {"marker": "existing model"})
        self.obj._training_bounds = lambda: (START, END)
        self.obj.training_schema_seed = lambda *args: None
        self.obj._eligible_rebuild_context = lambda: []
        for error in (sqlite3.DatabaseError("database disk image is malformed"),
                      history.TrainingRecorderUnavailable("Recorder failure budget exhausted"),
                      history.TrainingImportWriteCorruption("SQLite corruption writing Recorder rows"),
                      InterruptedError("History import cancelled")):
            with self.subTest(error=error):
                self.obj._training_recorder_import = Mock(side_effect=error)
                with patch.object(self.store, "clear_learning", wraps=self.store.clear_learning) as clear:
                    with self.assertRaises(type(error)):
                        self.obj._run_agent_indexing(agent_id, rebuild=True)
                    clear.assert_not_called()
        self.assertEqual(self.store.get_model(agent_id)["marker"], "existing model")

    def test_corrupt_recorder_coverage_write_propagates(self):
        self.obj.training_recorder_coverage = {(SOURCE, GOOD): (START, END)}
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        with patch.object(self.store, "meta_set", side_effect=error):
            with self.assertRaisesRegex(history.TrainingImportWriteCorruption, "saving Recorder coverage") as raised:
                self.obj._persist_training_recorder_coverage()
        self.assertIs(raised.exception.__cause__, error)

    def test_import_write_corruption_continues_to_replay_from_committed_archive(self):
        config = self.store.create_agent(agent(target_entity=GOOD))
        self.obj.training_job_agent_id = config["id"]
        self.obj.session_mode = True
        self.obj.engine.wake_event = threading.Event()
        self.obj.temporal_replay_stats = {}
        self.obj._training_bounds = lambda: (START, END)
        self.obj.training_schema_seed = lambda *args: None
        self.obj._run_training_chunk = Mock()
        self.obj.training_recorder_coverage = {}
        self.obj.training_recorder_coverage_hits = self.obj.training_recorder_coverage_misses = 0
        history.HEAVY_JOBS.owner = "agent_training"
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT

        with patch.object(self.store, "archive_packed_batch", side_effect=error):
            with self.assertRaisesRegex(history.TrainingImportWriteCorruption, "insufficient readable"):
                self.obj._run_agent_indexing(config["id"])
        self.obj._run_training_chunk.assert_not_called()
        self.assertIsNone(self.store.get_agent_config(config["id"])["training_cursor_ts"])

        self.store.archive_batch([
            (GOOD, ts, value, {}, None, SOURCE) for ts, value in self.recordings[GOOD]
        ])
        with patch.object(self.store, "archive_packed_batch", side_effect=error) as failed_write:
            self.obj._run_agent_indexing(config["id"])
        failed_write.assert_called_once()
        self.obj._run_training_chunk.assert_called_once()
        self.assertEqual(self.obj._run_training_chunk.call_args.args, (START, END))
        self.assertEqual(self.obj.training_job_timings["recorder_import_fallback"], "local_archive")
        self.assertFalse(self.obj.training_recorder_coverage)
        self.assertEqual(self.store.get_agent_config(config["id"])["training_cursor_ts"], END)
