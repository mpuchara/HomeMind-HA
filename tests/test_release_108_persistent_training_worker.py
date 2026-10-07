"""0.14.108 persistent selected-agent training worker contracts."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import history as history_module
import training_process
import tiny_mlp_shadow
from context_engine import ContextEngine
from replay import ReplayQueryCache, SQLiteTemporalTracker
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


class PersistentReplayCacheTests(unittest.TestCase):
    def test_zero_copy_mode_reuses_cached_row_objects_but_not_result_list(self):
        cache = ReplayQueryCache(max_rows=16, max_entry_rows=8, copy_rows=False)
        source = [{"id": 1, "state": "on"}, {"id": 2, "state": "off"}]
        cache.put("SELECT ?", (1,), source)
        first = cache.get("SELECT ?", (1,))
        second = cache.get("SELECT ?", (1,))
        self.assertIsNot(first, second)
        self.assertIs(first[0], source[0])
        self.assertIs(second[1], source[1])
        self.assertFalse(cache.status()["copy_rows"])

    def test_default_cache_keeps_defensive_copy_contract(self):
        cache = ReplayQueryCache(max_rows=16, max_entry_rows=8)
        source = [{"id": 1, "state": "on"}]
        cache.put("SELECT ?", (1,), source)
        cached = cache.get("SELECT ?", (1,))
        self.assertIsNot(cached[0], source[0])
        self.assertTrue(cache.status()["copy_rows"])


class SequenceRollbackTests(unittest.TestCase):
    def test_worker_advances_rollback_snapshot_after_each_completed_chunk(self):
        with tempfile.TemporaryDirectory(prefix="hm-sequence-rollback-") as root:
            root = Path(root)
            agent = {
                "id": "rollback-agent",
                "enabled": True,
                "input_entities": ["binary_sensor.motion"],
                "target_entity": "switch.target",
                "target_property": "power",
                "min_value": 0.0,
                "max_value": 1.0,
                "confidence_threshold": .78,
                "deadband": .5,
                "action_interval": 30.0,
                "exploration_step": 1.0,
                "exploration_interval": 21600.0,
                "micro_exploration": False,
                "ack_timeout": 8.0,
                "settling_seconds": 2.0,
                "manual_hold_seconds": 30.0,
            }

            class FakeStore:
                def __init__(self):
                    self.model = {"version": 1, "marker": "initial"}
                    self.training_publish_guard = None
                    self.checkpointed_archive_reads = False

                def get_agent_config(self, agent_id):
                    return dict(agent)

                def get_model(self, agent_id):
                    return dict(self.model)

                def set_training_progress(self, *args, **kwargs):
                    return None

                def event(self, *args, **kwargs):
                    return None

            store = FakeStore()

            class FakeEngine:
                def __init__(self, job, worker_store):
                    self.context_relevance = {agent["id"]: {}}
                    self.history_manager = None

            class FakeHistory:
                calls = 0

                def __init__(self, engine, worker_mode=False):
                    self.engine = engine
                    self.worker_mode = worker_mode
                    self.stop_event = threading.Event()
                    self.agent_jobs = set()
                    self.training_schema_cache = {}
                    self.neural_training_artifacts = {}
                    self.temporal_replay_stats = {}
                    self.training_long_memory_status = {}
                    self.training_replay_cache_status = {}
                    self.training_home_context_cache_status = {}
                    self.progress = 0.0
                    self.phase = "training"
                    self.message = ""

                def set_status(self, *args, **kwargs):
                    if kwargs.get("progress") is not None:
                        self.progress = float(kwargs["progress"])
                    return None

                def status(self):
                    return {
                        "phase": self.phase,
                        "progress": self.progress,
                        "message": self.message,
                    }

                def train_from_archive(self, start_ts, end_ts, agent_ids=None, **kwargs):
                    type(self).calls += 1
                    if type(self).calls == 1:
                        store.model = {"version": 1, "marker": "chunk-1"}
                        return 1
                    raise RuntimeError("synthetic second chunk failure")

                def close_persistent_training_resources(self):
                    return None

            class FakeBudget:
                def begin(self, **kwargs):
                    return None

                def end(self):
                    return None

            status_path = root / "job.status.json"
            result_path = root / "job.result.json"
            rollback_path = root / "job.rollback.json"
            job = {
                "format": training_process.JOB_FORMAT,
                "version": training_process.JOB_VERSION,
                "job_id": "rollback-job",
                "app_version": training_process.APP_VERSION,
                "training_revision": training_process.TRAINING_REVISION,
                "parent_pid": 0,
                "agent_id": agent["id"],
                "agent_fingerprint": training_process.agent_config_fingerprint(agent),
                "state_map": {},
                "entity_registry": {},
                "context_relevance": {},
                "automation_hints": [],
                "schema_cache_item": {},
                "options": dict(DEFAULT_OPTIONS),
                "resource_profile": {"worker_options": {}},
                "status_path": str(status_path),
                "result_path": str(result_path),
                "rollback_path": str(rollback_path),
                "sequence_start_ts": 0.0,
                "sequence_target_end_ts": 12.0,
                "sequence_chunks": [
                    {
                        "index": 0,
                        "start_ts": 0.0,
                        "end_ts": 6.0,
                        "checkpoint_cursor_ts": 6.0,
                        "checkpoint_meta": {},
                        "train_kwargs": {"progress_lo": 0.0, "progress_hi": .5},
                    },
                    {
                        "index": 1,
                        "start_ts": 3.0,
                        "end_ts": 12.0,
                        "checkpoint_cursor_ts": 12.0,
                        "checkpoint_meta": {},
                        "train_kwargs": {
                            "progress_lo": .5,
                            "progress_hi": 1.0,
                            "continuation_from_ts": 6.0,
                        },
                    },
                ],
            }
            job["checksum"] = training_process.descriptor_checksum(job)
            job_path = root / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")

            FakeHistory.calls = 0
            with (
                patch.object(history_module, "HistoryManager", FakeHistory),
                patch.object(training_process, "TrainingWorkerEngine", FakeEngine),
                # This rollback fixture has no policy implementation; selector
                # parity is exercised by the actual isolated-worker integration.
                patch("fast_local_primary.install", return_value=False),
                patch.object(training_process, "_worker_configure_budget", return_value=FakeBudget()),
                patch("storage.STORE", store),
                patch.object(tiny_mlp_shadow, "load_training_record", return_value=None),
                patch.object(tiny_mlp_shadow, "publish_training_artifact", side_effect=lambda _store, artifact: artifact),
            ):
                code = training_process.worker_main(job_path)

            self.assertEqual(code, 2)
            rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
            self.assertEqual(rollback["chunk_index"], 1)
            self.assertEqual(rollback["model_before"]["marker"], "chunk-1")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertIn("synthetic second chunk failure", result["error"])


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
