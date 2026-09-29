"""0.14.108 persistent selected-agent training worker contracts."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import history as history_module
import training_process
from context_engine import ContextEngine
from replay import SQLiteTemporalTracker
from settings import DEFAULT_OPTIONS
from storage import Store
from support import state


class PersistentSequenceDescriptorTests(unittest.TestCase):
    def test_sequence_descriptor_preserves_logical_chunk_boundaries(self):
        chunks = [
            {
                "start_ts": 0.0,
                "end_ts": 6 * 3600.0,
                "sequence_start_ts": 0.0,
                "sequence_target_end_ts": 18 * 3600.0,
                "checkpoint_cursor_ts": 6 * 3600.0,
                "train_kwargs": {
                    "agent_ids": {"agent-a"},
                    "qualify": False,
                    "continuation_from_ts": None,
                    "include_long_memory": True,
                },
            },
            {
                "start_ts": 3 * 3600.0,
                "end_ts": 12 * 3600.0,
                "sequence_start_ts": 0.0,
                "sequence_target_end_ts": 18 * 3600.0,
                "checkpoint_cursor_ts": 12 * 3600.0,
                "train_kwargs": {
                    "agent_ids": {"agent-a"},
                    "qualify": False,
                    "continuation_from_ts": 6 * 3600.0,
                    "include_long_memory": False,
                },
            },
            {
                "start_ts": 9 * 3600.0,
                "end_ts": 18 * 3600.0,
                "sequence_start_ts": 0.0,
                "sequence_target_end_ts": 18 * 3600.0,
                "checkpoint_cursor_ts": 18 * 3600.0,
                "train_kwargs": {
                    "agent_ids": {"agent-a"},
                    "qualify": True,
                    "continuation_from_ts": 12 * 3600.0,
                    "include_long_memory": False,
                },
            },
        ]
        base = {
            "format": training_process.JOB_FORMAT,
            "version": training_process.JOB_VERSION,
            "job_id": "sequence-test",
            "agent_id": "agent-a",
            "job_path": "/tmp/sequence-test.job.json",
            "status_path": "/tmp/sequence-test.status.json",
            "result_path": "/tmp/sequence-test.result.json",
            "log_path": "/tmp/sequence-test.log",
            "checksum": "placeholder",
        }
        with patch.object(training_process, "_build_job", return_value=dict(base)):
            job = training_process._build_sequence_job(SimpleNamespace(), chunks)

        self.assertEqual(job["sequence_contract"], "persistent_agent_training_worker_v1")
        self.assertEqual(len(job["sequence_chunks"]), 3)
        self.assertEqual(
            [
                (row["start_ts"], row["end_ts"], row["train_kwargs"]["continuation_from_ts"])
                for row in job["sequence_chunks"]
            ],
            [
                (0.0, 6 * 3600.0, None),
                (3 * 3600.0, 12 * 3600.0, 6 * 3600.0),
                (9 * 3600.0, 18 * 3600.0, 12 * 3600.0),
            ],
        )
        self.assertTrue(job["sequence_chunks"][-1]["train_kwargs"]["qualify"])
        self.assertTrue(job["sequence_chunks"][0]["train_kwargs"]["include_long_memory"])
        self.assertFalse(job["sequence_chunks"][1]["train_kwargs"]["include_long_memory"])
        self.assertNotIn("agent_ids", job["sequence_chunks"][0]["train_kwargs"])
        self.assertTrue(job["rollback_path"].endswith(".rollback.json"))

    def test_persistent_transport_knobs_are_nonsemantic(self):
        base = dict(DEFAULT_OPTIONS)
        changed = dict(base)
        changed["training_persistent_worker_enabled"] = not bool(
            base.get("training_persistent_worker_enabled", True)
        )
        changed["training_persistent_worker_cache_enabled"] = not bool(
            base.get("training_persistent_worker_cache_enabled", True)
        )
        self.assertEqual(
            training_process.training_options_fingerprint(base),
            training_process.training_options_fingerprint(changed),
        )


class PersistentSchedulingTests(unittest.TestCase):
    def test_fully_initialized_manager_submits_one_sequence_for_three_chunks(self):
        manager = history_module.HistoryManager.__new__(history_module.HistoryManager)
        manager.worker_mode = False
        manager._persistent_worker_capable = True
        manager.stop_event = threading.Event()
        manager.temporal_replay_stats = {}
        manager.training_stateful_replay_status = {}
        manager.training_process_status = {}
        manager.engine = SimpleNamespace(wake_event=threading.Event())
        manager._training_bounds = lambda: (0.0, 18 * 3600.0)
        manager._refresh_agent_history = lambda *args, **kwargs: None

        agent = {
            "id": "persistent-agent",
            "name": "Persistent agent",
            "training_window_start_ts": None,
            "training_cursor_ts": None,
            "training_window_end_ts": None,
            "training_state": "waiting",
            "benchmark_score": None,
            "benchmark_samples": 0,
            "benchmark_source": None,
            "benchmark_detail": {},
        }

        class FakeStore:
            def get_agent_config(self, agent_id):
                return dict(agent)

            def set_training_progress(self, *args, **kwargs):
                return None

            def event(self, *args, **kwargs):
                return None

            def set_training_state(self, *args, **kwargs):
                return None

        captured = {}

        def run_sequence(history, chunks):
            captured["history"] = history
            captured["chunks"] = list(chunks)
            history.training_process_status = {
                "sequence_chunk_reports": [
                    {"temporal_replay": {"continuation": {
                        "seed_target_rows_scanned": 0, "seed_agents": 0
                    }}},
                    {"temporal_replay": {"continuation": {
                        "seed_target_rows_scanned": 2, "seed_agents": 1
                    }}},
                    {"temporal_replay": {"continuation": {
                        "seed_target_rows_scanned": 2, "seed_agents": 1
                    }}},
                ]
            }
            return 0

        options = {
            **history_module.OPTIONS,
            "training_process_isolation": True,
            "training_persistent_worker_enabled": True,
            "agent_training_chunk_hours": 6,
            "agent_training_overlap_hours": 6,
            "agent_training_stateful_continuation": True,
            "agent_training_pause_ms": 0,
        }
        with (
            patch.object(history_module, "STORE", FakeStore()),
            patch.object(history_module, "OPTIONS", options),
            patch.object(history_module, "now_ts", return_value=18 * 3600.0),
            patch.object(training_process, "run_isolated_training_sequence", side_effect=run_sequence),
        ):
            manager._run_agent_indexing(
                agent["id"],
                rebuild=True,
                rebuild_reason="persistent-test",
            )

        self.assertIs(captured["history"], manager)
        chunks = captured["chunks"]
        self.assertEqual(len(chunks), 3)
        self.assertEqual(
            [
                (
                    row["start_ts"],
                    row["end_ts"],
                    row["train_kwargs"]["continuation_from_ts"],
                )
                for row in chunks
            ],
            [
                (0.0, 6 * 3600.0, None),
                (3 * 3600.0, 12 * 3600.0, 6 * 3600.0),
                (9 * 3600.0, 18 * 3600.0, 12 * 3600.0),
            ],
        )
        self.assertEqual(manager.training_stateful_replay_status["worker_processes"], 1)
        self.assertTrue(manager.training_stateful_replay_status["persistent_worker"])
        self.assertEqual(
            manager.training_stateful_replay_status["continuation_seed_target_rows"], 4
        )
        self.assertEqual(
            manager.training_stateful_replay_status["continuation_seed_agents"], 2
        )


class SharedReplayConnectionTests(unittest.TestCase):
    def test_tracker_close_does_not_close_shared_sqlite_connection(self):
        with tempfile.TemporaryDirectory(prefix="hm-persistent-replay-") as root:
            store = Store(Path(root) / "replay.db")
            entity = "binary_sensor.motion"
            store.archive_batch([
                (
                    entity,
                    1000.0,
                    "off",
                    {"device_class": "motion"},
                    None,
                    "test",
                ),
                (
                    entity,
                    1010.0,
                    "on",
                    {"device_class": "motion"},
                    None,
                    "test",
                ),
            ])
            states = {entity: state(entity, "on", device_class="motion")}
            registry = {entity: {"area_id": "room"}}
            context = ContextEngine(DEFAULT_OPTIONS)
            context.configure(states, entities=registry)

            connection = sqlite3.connect(store.path, timeout=30)
            connection.row_factory = sqlite3.Row
            first = SQLiteTemporalTracker(
                store, [entity], context, 900.0, 1100.0,
                connection=connection,
            )
            second = SQLiteTemporalTracker(
                store, [entity], context, 900.0, 1100.0,
                connection=connection,
            )
            try:
                first.advance(1010.0)
                first.close()
                # Shared connection must remain usable by the second tracker.
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM entity_history").fetchone()[0],
                    2,
                )
                second.advance(1010.0)
                self.assertTrue(second.stats()["shared_sqlite_connection"])
            finally:
                second.close()
                connection.close()


if __name__ == "__main__":
    unittest.main()
