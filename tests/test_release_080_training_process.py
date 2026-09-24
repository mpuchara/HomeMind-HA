"""0.14.80 contracts for process-isolated historical training."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from settings import DEFAULT_OPTIONS, TRAINING_REVISION
from storage import Store
from training_process import (
    JOB_FORMAT,
    JOB_VERSION,
    agent_config_fingerprint,
    descriptor_checksum,
    runtime_context_fingerprint,
)


class TrainingProcessContractTests(unittest.TestCase):
    def test_agent_fingerprint_ignores_lifecycle_but_detects_training_config(self):
        base = {
            "id": "a", "enabled": True, "input_entities": ["sensor.x"],
            "target_entity": "switch.x", "target_property": "power",
            "min_value": 0.0, "max_value": 1.0,
            "confidence_threshold": .78, "deadband": .5,
            "action_interval": 30.0, "exploration_step": 1.0,
            "exploration_interval": 21600.0, "micro_exploration": False,
            "mode": "paused", "training_state": "training",
            "training_progress": .2, "benchmark_score": .5,
        }
        changed_lifecycle = dict(base, mode="shadow", training_state="qualified",
                                 training_progress=1.0, benchmark_score=.9)
        self.assertEqual(
            agent_config_fingerprint(base),
            agent_config_fingerprint(changed_lifecycle),
        )
        changed_config = dict(base, input_entities=["sensor.y"])
        self.assertNotEqual(
            agent_config_fingerprint(base),
            agent_config_fingerprint(changed_config),
        )

    def test_runtime_fingerprint_ignores_state_values_but_detects_topology_and_options(self):
        states_a = {
            "binary_sensor.motion": {
                "state": "off",
                "attributes": {"device_class": "motion"},
            }
        }
        states_b = {
            "binary_sensor.motion": {
                "state": "on",
                "attributes": {"device_class": "motion"},
            }
        }
        registry = {"binary_sensor.motion": {"area_id": "kitchen"}}
        options = {"feature_dimensions": 128, "x": 1}
        self.assertEqual(
            runtime_context_fingerprint(states_a, registry, options),
            runtime_context_fingerprint(states_b, registry, options),
        )
        moved = {"binary_sensor.motion": {"area_id": "hall"}}
        self.assertNotEqual(
            runtime_context_fingerprint(states_a, registry, options),
            runtime_context_fingerprint(states_a, moved, options),
        )
        self.assertNotEqual(
            runtime_context_fingerprint(states_a, registry, options),
            runtime_context_fingerprint(states_a, registry, {**options, "x": 2}),
        )

    def test_descriptor_checksum_covers_versioned_job_payload(self):
        job = {
            "format": JOB_FORMAT, "version": JOB_VERSION,
            "job_id": "job", "agent_id": "a",
            "training_revision": TRAINING_REVISION,
            "start_ts": 1.0, "end_ts": 2.0,
        }
        job["checksum"] = descriptor_checksum(job)
        self.assertEqual(job["checksum"], descriptor_checksum(job))
        altered = dict(job, end_ts=3.0)
        self.assertNotEqual(job["checksum"], descriptor_checksum(altered))


class StoreIsolationContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hm-process-store-")
        self.store = Store(Path(self.temp.name) / "adaptive_ai.db")

    def tearDown(self):
        self.temp.cleanup()

    def _agent(self):
        return self.store.create_agent({
            "name": "Process test",
            "target_entity": "switch.target",
            "target_property": "power",
            "min_value": 0, "max_value": 1,
            "deadband": .5, "exploration_step": 1,
            "confidence_threshold": .78,
            "action_interval": 30,
            "input_entities": ["binary_sensor.motion"],
        })

    def test_checkpointed_archive_reader_has_exact_order_and_rows(self):
        rows = []
        for idx in range(240):
            rows.append((
                "sensor.a" if idx % 2 else "sensor.b",
                1000.0 + idx // 3,
                str(idx % 5),
                {"unit_of_measurement": "x"},
                None,
                "test",
            ))
        self.store.archive_batch(rows)
        baseline = list(self.store.archive_iter(1000, 1100, chunk_size=101))
        self.store.checkpointed_archive_reads = True
        paged = list(self.store.archive_iter(1000, 1100, chunk_size=101))
        self.assertEqual(paged, baseline)
        self.assertEqual(
            [(r["ts"], r["id"]) for r in paged],
            sorted((r["ts"], r["id"]) for r in paged),
        )

    def test_publish_guard_blocks_atomic_model_replace(self):
        agent = self._agent()
        self.store.save_model(agent["id"], {"version": 1, "value": "old"})
        old = self.store.get_model(agent["id"])

        def reject(_agent_id, _model):
            raise RuntimeError("stale job")

        self.store.training_publish_guard = reject
        with self.assertRaisesRegex(RuntimeError, "stale job"):
            self.store.save_model(agent["id"], {"version": 1, "value": "new"})
        self.assertEqual(self.store.get_model(agent["id"]), old)

    def test_restore_chunk_snapshot_preserves_old_watermark_and_discards_new_rows(self):
        agent = self._agent()
        self.store.add_historical_experience(
            agent["id"], 1, 0, 0.0, 1.0, 10.0, {0: 1.0}
        )
        self.store.save_model(agent["id"], {"version": 1, "value": "old"})
        before_model = self.store.get_model(agent["id"])
        before_agent = self.store.get_agent_config(agent["id"])

        self.store.add_historical_experience(
            agent["id"], 2, 1, 1.0, 1.0, 10.0, {0: 2.0}
        )
        self.store.save_model(agent["id"], {"version": 1, "value": "new"})
        self.assertEqual(len(self.store.list_historical_experiences(agent["id"])), 2)

        self.store.restore_training_chunk_snapshot(
            agent["id"], before_agent, before_model
        )
        self.store.discard_uncommitted_experiences(agent["id"])
        self.assertEqual(self.store.get_model(agent["id"])["value"], "old")
        rows = self.store.list_historical_experiences(agent["id"])
        self.assertEqual([row["target_history_id"] for row in rows], [1])


class SupervisorRollbackContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hm-supervisor-")
        self.store = Store(Path(self.temp.name) / "adaptive_ai.db")
        self.agent = self.store.create_agent({
            "name": "Supervisor test",
            "target_entity": "switch.target",
            "target_property": "power",
            "min_value": 0, "max_value": 1,
            "deadband": .5, "exploration_step": 1,
            "confidence_threshold": .78,
            "action_interval": 30,
            "input_entities": ["binary_sensor.motion"],
        })
        self.store.add_historical_experience(
            self.agent["id"], 1, 0, 0.0, 1.0, 10.0, {0: 1.0}
        )
        self.store.save_model(
            self.agent["id"], {"version": 1, "value": "pre-worker"}
        )
        self.before_model = self.store.get_model(self.agent["id"])
        self.before_agent = self.store.get_agent_config(self.agent["id"])

    def tearDown(self):
        self.temp.cleanup()

    def test_rejected_chunk_helper_rolls_back_model_lifecycle_and_experiences(self):
        import training_process

        self.store.add_historical_experience(
            self.agent["id"], 2, 1, 1.0, 1.0, 10.0, {0: 2.0}
        )
        self.store.save_model(
            self.agent["id"], {"version": 1, "value": "worker-result"}
        )
        self.store.set_training_state(
            self.agent["id"], "qualified", score=.99, samples=99,
            source="worker", detail={"worker": True},
            shadow_after_completion=True,
        )

        training_process._restore_rejected_chunk(
            self.store, self.agent["id"], self.before_agent, self.before_model
        )
        self.assertEqual(
            self.store.get_model(self.agent["id"])["value"], "pre-worker"
        )
        after = self.store.get_agent_config(self.agent["id"])
        self.assertEqual(after["training_state"], self.before_agent["training_state"])
        self.assertEqual(after["mode"], self.before_agent["mode"])
        self.assertEqual(after["benchmark_score"], self.before_agent["benchmark_score"])
        rows = self.store.list_historical_experiences(self.agent["id"])
        self.assertEqual([row["target_history_id"] for row in rows], [1])

    def _fake_history(self, *, stop=False):
        event = __import__("threading").Event()
        if stop:
            event.set()
        engine = SimpleNamespace(
            models={}, context_relevance={}, state_map={}, entity_registry={},
            lock=__import__("threading").RLock(),
            agent_index_at=1.0, agent_index_revision=1,
            wake_event=__import__("threading").Event(),
        )
        return SimpleNamespace(
            engine=engine,
            stop_event=event,
            job_cancel_event=None,
            active_training_process=None,
            training_process_status={},
            temporal_replay_stats={},
            training_replay_cache_status={},
            training_home_context_cache_status={},
            training_schema_cache={},
            set_status=lambda **kwargs: None,
        )

    def _supervisor_job(self, root):
        import training_process
        job_id = "supervisor-test"
        return {
            "format": training_process.JOB_FORMAT,
            "version": training_process.JOB_VERSION,
            "job_id": job_id,
            "agent_id": self.agent["id"],
            "agent_fingerprint": agent_config_fingerprint(self.before_agent),
            "context_fingerprint": runtime_context_fingerprint({}, {}, dict(DEFAULT_OPTIONS)),
            "job_path": str(Path(root) / f"{job_id}.job.json"),
            "status_path": str(Path(root) / f"{job_id}.status.json"),
            "result_path": str(Path(root) / f"{job_id}.result.json"),
            "log_path": str(Path(root) / f"{job_id}.log"),
        }

    def test_parent_cancellation_terminates_worker_and_rolls_back_dirty_chunk(self):
        import storage as storage_module
        import training_process

        history = self._fake_history(stop=True)
        job = self._supervisor_job(self.temp.name)
        store = self.store
        aid = self.agent["id"]

        class FakeProcess:
            pid = os.getpid()

            def __init__(self):
                self.code = None
                self.dirtied = False

            def poll(self):
                return self.code

            def terminate(self):
                if not self.dirtied:
                    store.add_historical_experience(
                        aid, 2, 1, 1.0, 1.0, 10.0, {0: 2.0}
                    )
                    store.save_model(aid, {"version": 1, "value": "dirty-child"})
                    self.dirtied = True
                self.code = -15

            def kill(self):
                self.code = -9

            def wait(self, timeout=None):
                return self.code if self.code is not None else 0

        fake = FakeProcess()
        with (
            patch.object(storage_module, "STORE", self.store),
            patch.object(training_process, "DATA_DIR", Path(self.temp.name)),
            patch.object(training_process, "_build_job", return_value=dict(job)),
            patch.object(training_process, "_prune_job_files", return_value=None),
            patch.object(training_process.subprocess, "Popen", return_value=fake),
            patch.object(training_process, "_proc_metrics", return_value={
                "rss_mb": 10.0, "cpu_seconds": .1,
                "read_bytes": 0, "write_bytes": 0,
            }),
        ):
            with self.assertRaisesRegex(InterruptedError, "cancelled"):
                training_process.run_isolated_training_chunk(
                    history, 1.0, 2.0, agent_ids={aid}
                )

        self.assertEqual(self.store.get_model(aid)["value"], "pre-worker")
        rows = self.store.list_historical_experiences(aid)
        self.assertEqual([row["target_history_id"] for row in rows], [1])
        self.assertEqual(history.training_process_status["state"], "cancelled")

    def test_successful_worker_result_is_rejected_after_agent_config_change(self):
        import storage as storage_module
        import training_process

        history = self._fake_history(stop=False)
        job = self._supervisor_job(self.temp.name)
        store = self.store
        aid = self.agent["id"]

        class FakeProcess:
            pid = os.getpid()

            def __init__(self):
                self.code = 0
                store.add_historical_experience(
                    aid, 2, 1, 1.0, 1.0, 10.0, {0: 2.0}
                )
                store.save_model(aid, {"version": 1, "value": "stale-child"})
                store.update_agent(aid, {"input_entities": ["sensor.changed"]})
                Path(job["result_path"]).write_text(json.dumps({
                    "ok": True,
                    "job_id": job["job_id"],
                    "return_value": 1,
                    "context_relevance": {},
                    "temporal_replay": {},
                    "training_replay_cache": {},
                    "training_home_context_cache": {},
                    "schema_cache_item": {},
                }), encoding="utf-8")

            def poll(self):
                return self.code

            def terminate(self):
                self.code = -15

            def kill(self):
                self.code = -9

            def wait(self, timeout=None):
                return self.code

        with (
            patch.object(storage_module, "STORE", self.store),
            patch.object(training_process, "DATA_DIR", Path(self.temp.name)),
            patch.object(training_process, "_build_job", return_value=dict(job)),
            patch.object(training_process, "_prune_job_files", return_value=None),
            patch.object(training_process.subprocess, "Popen", side_effect=lambda *a, **k: FakeProcess()),
            patch.object(training_process, "_proc_metrics", return_value={
                "rss_mb": 10.0, "cpu_seconds": .1,
                "read_bytes": 0, "write_bytes": 0,
            }),
        ):
            with self.assertRaises(training_process.StaleTrainingJob):
                training_process.run_isolated_training_chunk(
                    history, 1.0, 2.0, agent_ids={aid}
                )

        current = self.store.get_agent_config(aid)
        self.assertEqual(current["input_entities"], ["sensor.changed"])
        self.assertEqual(self.store.get_model(aid)["value"], "pre-worker")
        rows = self.store.list_historical_experiences(aid)
        self.assertEqual([row["target_history_id"] for row in rows], [1])
        self.assertEqual(history.training_process_status["state"], "failed")

    def test_worker_mode_history_init_does_not_pause_parent_training_lifecycle(self):
        import history as history_module

        self.store.set_training_state(
            self.agent["id"], "training", score=None, samples=0,
            source=None, detail={"worker": "active"},
        )
        fake_engine = SimpleNamespace()
        with patch.object(history_module, "STORE", self.store):
            history_module.HistoryManager(fake_engine, worker_mode=True)
        current = self.store.get_agent_config(self.agent["id"])
        self.assertEqual(current["training_state"], "training")

    def test_parent_mode_history_init_keeps_existing_restart_pause_contract(self):
        import history as history_module

        self.store.set_training_state(
            self.agent["id"], "training", score=None, samples=0,
            source=None, detail={"restart": True},
        )
        fake_engine = SimpleNamespace()
        with patch.object(history_module, "STORE", self.store):
            history_module.HistoryManager(fake_engine, worker_mode=False)
        current = self.store.get_agent_config(self.agent["id"])
        self.assertEqual(current["training_state"], "paused")


class ActualWorkerSmoke(unittest.TestCase):
    def test_clean_worker_process_trains_tiny_explicit_schema_job(self):
        with tempfile.TemporaryDirectory(prefix="hm-worker-smoke-") as root:
            db = Path(root) / "adaptive_ai.db"
            store = Store(db)
            agent = store.create_agent({
                "name": "Worker smoke",
                "target_entity": "switch.target",
                "target_property": "power",
                "min_value": 0, "max_value": 1,
                "deadband": .5, "exploration_step": 1,
                "confidence_threshold": .78,
                "action_interval": 30,
                "input_entities": ["binary_sensor.motion"],
            })
            base = 1_700_000_000.0
            rows = []
            for idx in range(24):
                ts = base + idx * 30
                rows.append((
                    "binary_sensor.motion", ts - 2,
                    "on" if idx % 2 else "off",
                    {"device_class": "motion"}, None, "test",
                ))
                rows.append((
                    "switch.target", ts,
                    "on" if idx % 2 else "off",
                    {}, None, "test",
                ))
            store.archive_batch(rows)

            state_map = {
                "binary_sensor.motion": {
                    "entity_id": "binary_sensor.motion", "state": "off",
                    "attributes": {"device_class": "motion"},
                },
                "switch.target": {
                    "entity_id": "switch.target", "state": "off",
                    "attributes": {},
                },
            }
            registry = {
                "binary_sensor.motion": {"area_id": "room"},
                "switch.target": {"area_id": "room"},
            }
            options = dict(DEFAULT_OPTIONS)
            options.update({
                "training_cpu_duty_cycle": .70,
                "training_max_continuous_work_ms": 35,
                "training_throttle_max_sleep_seconds": .05,
                "training_home_context_cache_entries": 4,
                "training_home_context_cache_units": 1024,
                "feature_dimensions": 128,
            })
            job_id = "worker-smoke"
            job = {
                "format": JOB_FORMAT,
                "version": JOB_VERSION,
                "job_id": job_id,
                "app_version": __import__("settings").APP_VERSION,
                "training_revision": TRAINING_REVISION,
                "created_at": base,
                "agent_id": agent["id"],
                "agent_fingerprint": agent_config_fingerprint(
                    store.get_agent_config(agent["id"])
                ),
                "context_fingerprint": runtime_context_fingerprint(
                    state_map, registry, options
                ),
                "state_map": state_map,
                "entity_registry": registry,
                "context_relevance": {},
                "automation_hints": [],
                "schema_cache_item": {},
                "options": options,
                "start_ts": base - 5,
                "end_ts": base + 24 * 30,
                "train_kwargs": {
                    "qualify": False,
                    "include_candidates": True,
                    "benchmark": False,
                    "accumulate_benchmark": False,
                    "progress_lo": 0.0,
                    "progress_hi": 1.0,
                    "progress_label": "Worker smoke",
                },
                "status_path": str(Path(root) / "status.json"),
                "result_path": str(Path(root) / "result.json"),
                "log_path": str(Path(root) / "worker.log"),
            }
            job["checksum"] = descriptor_checksum(job)
            job_path = Path(root) / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")

            import training_process
            env = dict(os.environ)
            env["ADAPTIVE_AI_DATA"] = root
            completed = subprocess.run(
                [sys.executable, training_process.__file__, "--worker", str(job_path)],
                env=env,
                cwd=str(Path(training_process.__file__).parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=45,
            )
            if completed.returncode:
                log = Path(job["log_path"])
                self.fail(
                    f"worker exited {completed.returncode}: {completed.stdout}\n"
                    + (log.read_text(errors="replace") if log.exists() else "")
                )
            result = json.loads(Path(job["result_path"]).read_text())
            self.assertTrue(result["ok"])
            self.assertNotEqual(os.getpid(), result["worker_pid"])
            model = store.get_model(agent["id"])
            self.assertIsNotNone(model)
            self.assertGreaterEqual(int(model.get("_history_watermark") or 0), 1)


if __name__ == "__main__":
    unittest.main()
