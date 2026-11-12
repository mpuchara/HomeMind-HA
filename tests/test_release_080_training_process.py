"""0.14.80 contracts for process-isolated historical training."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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
    _compact_training_state_map,
    _compact_training_registry,
    _process_start_token,
    _same_process_alive,
    _worker_boot_status,
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

    def test_worker_context_snapshot_drops_large_irrelevant_ha_attributes(self):
        blob = "x" * 250000
        raw = {
            "sensor.rich": {
                "entity_id": "sensor.rich",
                "state": "23.4",
                "attributes": {
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                    "friendly_name": "Rich temperature",
                    "temperature": 23.4,
                    "huge_vendor_blob": blob,
                    "forecast": [blob, blob],
                },
                "last_changed": "2026-09-25T10:00:00+00:00",
                "last_updated": "2026-09-25T10:00:01+00:00",
            }
        }
        compact = _compact_training_state_map(raw)
        row = compact["sensor.rich"]
        self.assertEqual(row["state"], "23.4")
        self.assertEqual(row["attributes"]["device_class"], "temperature")
        self.assertEqual(row["attributes"]["unit_of_measurement"], "°C")
        self.assertEqual(row["attributes"]["friendly_name"], "Rich temperature")
        self.assertEqual(row["attributes"]["temperature"], 23.4)
        self.assertNotIn("huge_vendor_blob", row["attributes"])
        self.assertNotIn("forecast", row["attributes"])
        self.assertLess(len(json.dumps(compact)), 2000)

    def test_worker_registry_snapshot_keeps_only_training_topology(self):
        raw = {
            "binary_sensor.motion": {
                "device_id": "dev-1",
                "area_id": "kitchen",
                "platform": "esphome",
                "integration": "esphome",
                "capabilities": {"blob": "x" * 100000},
                "original_name": "Vendor detail",
            }
        }
        compact = _compact_training_registry(raw)
        self.assertEqual(compact["binary_sensor.motion"], {
            "device_id": "dev-1",
            "area_id": "kitchen",
            "platform": "esphome",
            "integration": "esphome",
        })
        self.assertLess(len(json.dumps(compact)), 300)

    def test_runtime_fingerprint_ignores_irrelevant_large_payload_but_keeps_semantic_metadata(self):
        base = {
            "sensor.temp": {
                "state": "20",
                "attributes": {
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                    "friendly_name": "Room temperature",
                    "temperature": 20.0,
                    "huge_blob": "a" * 100000,
                },
            }
        }
        changed_blob = {
            "sensor.temp": {
                "state": "99",
                "attributes": {
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                    "friendly_name": "Room temperature",
                    "temperature": 99.0,
                    "huge_blob": "b" * 200000,
                },
            }
        }
        registry = {"sensor.temp": {"device_id": "d", "area_id": "room", "platform": "esphome"}}
        options = {"feature_dimensions": 128}
        self.assertEqual(
            runtime_context_fingerprint(base, registry, options),
            runtime_context_fingerprint(changed_blob, registry, options),
        )
        changed_name = json.loads(json.dumps(base))
        changed_name["sensor.temp"]["attributes"]["friendly_name"] = "Outdoor temperature"
        self.assertNotEqual(
            runtime_context_fingerprint(base, registry, options),
            runtime_context_fingerprint(changed_name, registry, options),
        )

    def test_worker_boot_status_exists_before_history_manager_runtime(self):
        with tempfile.TemporaryDirectory(prefix="hm-worker-status-") as root:
            path = Path(root) / "status.json"
            job = {
                "status_path": str(path),
                "train_kwargs": {"progress_lo": 0.125},
            }
            _worker_boot_status(
                job,
                "descriptor ready",
                stage="descriptor_validated",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "worker_bootstrap")
            self.assertEqual(payload["bootstrap_stage"], "descriptor_validated")
            self.assertAlmostEqual(payload["progress"], 0.125)
            self.assertEqual(payload["message"], "descriptor ready")
            self.assertEqual(payload["worker_pid"], os.getpid())

    def test_parent_process_identity_uses_pid_plus_start_token(self):
        token = _process_start_token(os.getpid())
        self.assertTrue(_same_process_alive(os.getpid(), token))
        if token is not None:
            self.assertFalse(_same_process_alive(os.getpid(), str(int(token) + 1)))
        self.assertFalse(_same_process_alive(99999999, None))

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


class PiProfilerContracts(unittest.TestCase):
    def test_baseline_comparison_reports_total_cpu_without_hiding_worker(self):
        from pi_training_profile import comparison

        baseline = {
            "scenario": "training",
            "duration_seconds": 60.0,
            "runtime": {
                "cpu_one_core_percent": 80.0,
                "rss_mb_p95": 150.0,
            },
            "worker": None,
            "combined": {
                "cpu_one_core_percent": 80.0,
                "rss_p95_mb_sum": 150.0,
            },
            "http_status_latency_ms": {"p95": 120.0, "p99": 180.0},
            "correct_http_latency_ms": {"p95": None},
            "training_progress": {"delta": .20},
            "runtime_latency_metrics": {
                "telemetry.event_to_intent_p95_ms": {"p95": 250.0}
            },
        }
        candidate = {
            "scenario": "training",
            "duration_seconds": 60.0,
            "runtime": {
                "cpu_one_core_percent": 25.0,
                "rss_mb_p95": 110.0,
            },
            "worker": {
                "cpu_one_core_percent": 40.0,
                "rss_mb_p95": 80.0,
            },
            "combined": {
                "cpu_one_core_percent": 65.0,
                "rss_p95_mb_sum": 190.0,
            },
            "http_status_latency_ms": {"p95": 40.0, "p99": 60.0},
            "correct_http_latency_ms": {"p95": None},
            "training_progress": {"delta": .22},
            "runtime_latency_metrics": {
                "telemetry.event_to_intent_p95_ms": {"p95": 90.0}
            },
        }
        result = comparison(candidate, baseline)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(
            result["fields"]["runtime_cpu_one_core_percent"][
                "candidate_minus_baseline"
            ],
            -55.0,
        )
        self.assertEqual(
            result["fields"]["combined_cpu_one_core_percent"][
                "candidate_minus_baseline"
            ],
            -15.0,
        )
        self.assertEqual(
            result["shared_runtime_latency_p95"][
                "telemetry.event_to_intent_p95_ms"
            ]["candidate_minus_baseline"],
            -160.0,
        )

    def test_baseline_comparison_warns_on_non_equivalent_scenario(self):
        from pi_training_profile import comparison

        result = comparison(
            {"scenario": "burst", "duration_seconds": 30.0},
            {"scenario": "idle", "duration_seconds": 60.0},
        )
        self.assertIn("scenario mismatch", result["warnings"])
        self.assertIn("duration differs by more than 5%", result["warnings"])


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

    def test_worker_store_bootstrap_does_not_run_migrations_while_parent_writer_is_active(self):
        self.store.meta_set("worker_bootstrap_probe", "ready")
        holder = sqlite3.connect(str(Path(self.temp.name) / "adaptive_ai.db"), timeout=1)
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(
                "UPDATE app_meta SET value=value WHERE key='worker_bootstrap_probe'"
            )
            env = dict(os.environ)
            env["ADAPTIVE_AI_DATA"] = self.temp.name
            env["ADAPTIVE_AI_TRAINING_WORKER"] = "1"
            env["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "adaptive_ai" / "src"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import storage; "
                        "assert storage.STORE.training_worker_process is True; "
                        "assert storage.STORE.meta_get('worker_bootstrap_probe') == 'ready'; "
                        "print('worker-bootstrap-ok')"
                    ),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("worker-bootstrap-ok", result.stdout)
        finally:
            holder.rollback()
            holder.close()

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
        self.assertEqual(current["training_state"], "needs_retrain")
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
        with (
            patch.object(history_module, "STORE", self.store),
            patch.object(
                self.store,
                "archive_stats",
                side_effect=AssertionError(
                    "isolated worker must not scan global archive stats"
                ),
            ),
        ):
            history = history_module.HistoryManager(fake_engine, worker_mode=True)
        current = self.store.get_agent_config(self.agent["id"])
        self.assertEqual(current["training_state"], "training")
        self.assertTrue(
            history.archive_cache["worker_bootstrap_skipped_global_scan"]
        )

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
        self.run_worker_fixture()

    def test_generation_child_completes_with_production_inherited_budget(self):
        result=self.run_worker_fixture(samples=480,corruption_recovery=True,
                                       qualify=True,inherit_storage_budget=True)
        self.assertTrue(result['inherited_storage_budget'])
        self.assertEqual(result['agent_mode'],'shadow')
        self.assertEqual(result['agent_progress'],1.)

    def test_worker_with_corrupt_archive_indexes_keeps_the_same_learned_model(self):
        reference = self.run_worker_fixture(samples=120)
        recovered = self.run_worker_fixture(samples=120, corrupt_archive_indexes=True)
        self.assertEqual(reference["learning_hash"], recovered["learning_hash"])
        self.assertEqual(reference["frozen_holdout_counts"], recovered["frozen_holdout_counts"])
        self.assertEqual(
            reference["worker_phases"]["replay_driver_rows_processed"],
            recovered["worker_phases"]["replay_driver_rows_processed"],
        )

    def test_corrupt_indexes_and_concurrent_writes_complete_training_in_shadow(self):
        recovered = self.run_worker_fixture(
            samples=120, corrupt_archive_indexes=True, qualify=True,
            concurrent_writes=True,
        )
        self.assertEqual(recovered["agent_mode"], "shadow")
        self.assertEqual(recovered["agent_progress"], 1.0)
        self.assertGreater(recovered["concurrent_writes"], 0)

    def run_worker_fixture(self, samples=24, corrupt_archive_indexes=False, *,
                           qualify=False, concurrent_writes=False,
                           corruption_recovery=False, corrupt_table_page=False,
                           two_agents=False, http_gap=False, kill_and_restart=False,
                             expect_unavailable=False, large_noise_rows=0, inherit_storage_budget=False, rich_checkpoints=False):
        with tempfile.TemporaryDirectory(prefix="hm-worker-smoke-") as root:
            db = Path(root) / "adaptive_ai.db"
            store = Store(db)
            # Production creates the Observation Contract journal schema in the realtime
            # parent before an isolated training worker can be admitted. Mirror only the
            # fast-journal read surface here without importing observation_contract into
            # this parent unittest process; the child is the contract under test.
            with store.lock, store.conn() as c:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS feature_observation_events (
                        event_key TEXT PRIMARY KEY, contract_version INTEGER NOT NULL,
                        entity_id TEXT NOT NULL, event_time REAL NOT NULL,
                        received_time REAL NOT NULL, state TEXT,
                        attributes_json TEXT NOT NULL DEFAULT '{}',
                        last_changed TEXT, last_updated TEXT, source TEXT NOT NULL,
                        quality REAL NOT NULL, protected_until REAL NOT NULL DEFAULT 0);
                    CREATE INDEX IF NOT EXISTS idx_feature_obs_entity_time
                        ON feature_observation_events(entity_id,event_time,received_time);
                    CREATE INDEX IF NOT EXISTS idx_feature_obs_entity_received_time
                        ON feature_observation_events(entity_id,received_time,event_time);
                """)
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
            for idx in range(samples):
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
            if rich_checkpoints:
                for n in range(24):
                    store.create_agent(dict(name='Runtime '+str(n), target_entity='switch.runtime_'+str(n),
                        target_property='power', min_value=0,max_value=1,deadband=.5,
                        exploration_step=1,confidence_threshold=.78,action_interval=30,
                        input_entities=['binary_sensor.motion']))
                store.archive_batch([(f'sensor.context_{n}',base+i*30,str(i%7),{},None,'test')
                    for n in range(243) for i in range(samples)])
                from context_engine import ContextEngine
                from home_state import RoomBeliefModel
                context=ContextEngine({},store=store)
                with store.conn() as c:
                    c.execute('CREATE TABLE IF NOT EXISTS home_checkpoints(ts REAL PRIMARY KEY,model TEXT NOT NULL)')
                    for i in range(5):
                        stamp=base+i*30
                        c.execute('INSERT INTO home_checkpoints VALUES(?,?)',(stamp,json.dumps(RoomBeliefModel().export())))
                        c.execute('INSERT INTO adaptive_presence_checkpoints VALUES(?,?)',(stamp,json.dumps(context.adaptive_presence.export())))
            source_table_rows = {}
            other_model_before = None
            if large_noise_rows:
                other = store.create_agent(dict(name='Unrelated history owner',target_entity='switch.unrelated',
                    target_property='power',min_value=0,max_value=1,deadband=.5,exploration_step=1,
                    confidence_threshold=.78,action_interval=30,input_entities=['sensor.noise_0']))
                store.save_model(other['id'],dict(marker='untouched-other-agent'))
                other_model_before = store.get_model(other['id'])
                # Streaming fixture construction is outside measured worker time.
                with store.conn() as conn:
                    conn.executemany('INSERT INTO entity_history(entity_id,ts,state,attributes_json,source) VALUES(?,?,?,?,?)',
                        ((f'sensor.noise_{i%98}',base-10_000_000+i,'1','{}','synthetic') for i in range(int(large_noise_rows))))
                    conn.executemany('INSERT INTO historical_experiences(agent_id,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json) VALUES(?,?,?,?,?,?,?,?)',
                        ((other['id'],10_000_000+i,'fixture',0,0.,1.,30.,'{}') for i in range(100000)))
                    conn.executemany('INSERT INTO feature_observation_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        ((f'noise:{i}',12,f'sensor.noise_{i%98}',base-1_000_000+i,base-1_000_000+i,'1','{}',None,None,'synthetic',1.,0.) for i in range(100000)))
                    conn.executemany('INSERT INTO events(agent_id,created_at,level,kind,message,data_json) VALUES(?,?,?,?,?,?)',
                        ((other['id'],'fixture','info','unrelated','noise','{}') for _ in range(50000)))
                    conn.executemany('INSERT INTO rl_feedback(agent_id,created_at,action_index,action_value,reward,reason,features_json,user_id,source) VALUES(?,?,?,?,?,?,?,?,?)',
                        ((other['id'],'fixture',0,0.,-1.,'Correct','{}','resident','manual_feedback') for _ in range(1000)))
                    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    source_table_rows = {name:conn.execute('SELECT COUNT(*) FROM "'+name+'"').fetchone()[0] for name in names}
                store.add_feedback(agent['id'],0,0.,-1.,'Correct',{0:1.},user_id='resident',source='manual_feedback')
                source_table_rows['rl_feedback'] += 1

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
                "end_ts": base + samples * 30,
                "train_kwargs": {
                    "qualify": qualify,
                    "include_candidates": True,
                    "benchmark": samples > 24,
                    "accumulate_benchmark": False,
                    "progress_lo": 0.0,
                    "progress_hi": 1.0,
                    "progress_label": "Worker smoke",
                },
                "status_path": str((Path(root)/'jobs' if large_noise_rows else Path(root)) / "status.json"),
                "result_path": str((Path(root)/'jobs' if large_noise_rows else Path(root)) / "result.json"),
                "log_path": str((Path(root)/'jobs' if large_noise_rows else Path(root)) / "worker.log"),
            }
            job["checksum"] = descriptor_checksum(job)
            job_path = Path(root) / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")

            if corruption_recovery:
                job['training_generation'] = True
                job['generation_rebuild'] = True
                # A new generation accumulates benchmark evidence only from its
                # own trustworthy segments, retaining the established scorer.
                job['train_kwargs']['accumulate_benchmark'] = True
                if http_gap:
                    store.meta_set('training_history_gaps_v1', json.dumps([dict(
                        entity_id='binary_sensor.motion', start=base+2000, end=base+2100,
                        source='ha_history_full')]))
                if two_agents:
                    healthy = store.create_agent(dict(name='First healthy agent', target_entity='switch.healthy',
                        target_property='power', min_value=0,max_value=1,deadband=.5,exploration_step=1,
                        confidence_threshold=.78,action_interval=30,input_entities=['binary_sensor.motion']))
                    store.archive_batch([('switch.healthy',r[1],r[2],r[3],r[4],r[5]) for r in rows if r[0]=='switch.target'])
                    first_job = dict(job, agent_id=healthy['id'], generation_rebuild=True,
                        agent_fingerprint=agent_config_fingerprint(store.get_agent_config(healthy['id'])),
                        result_path=str(Path(root)/'first.result.json'))
                    first_job['state_map'] = dict(state_map, **{'switch.healthy': dict(state_map['switch.target'],entity_id='switch.healthy')})
                    first_job['checksum'] = descriptor_checksum(first_job)
                    first_path = Path(root)/'first.job.json'
                    first_path.write_text(json.dumps(first_job),encoding='utf-8')
                    env = dict(os.environ, ADAPTIVE_AI_DATA=root, ADAPTIVE_AI_TRAINING_WORKER='1')
                    if inherit_storage_budget:
                        env.update(store.storage_budget.worker_environment())
                    proc = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'adaptive_ai/src/training_process.py'), '--worker',str(first_path)],
                        env=env, capture_output=True,text=True,timeout=180)
                    self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                    first_result = json.loads(Path(first_job['result_path']).read_text())
                    self.assertTrue(first_result['ok'],first_result)
                    from history_recovery import publish_generation
                    publish_generation(store, first_result['generation_path'], healthy['id'], first_job['agent_fingerprint'], agent_config_fingerprint)
                    healthy_model = store.get_model(healthy['id'])
                    self.assertEqual(store.get_agent_config(healthy['id'])['mode'],'shadow')
                job['checksum'] = descriptor_checksum(job)
                job_path.write_text(json.dumps(job), encoding='utf-8')

            if rich_checkpoints:
                middle=base+samples*15
                job['sequence_start_ts']=job['start_ts']
                job['sequence_target_end_ts']=job['end_ts']
                job['sequence_chunks']=[dict(index=i,start_ts=lo,end_ts=hi,
                    checkpoint_cursor_ts=hi,checkpoint_meta={},train_kwargs=dict(job['train_kwargs'],
                    qualify=i==1,finalize_neural=i==1,continuation_from_ts=None if i==0 else middle,
                    progress_lo=i/2,progress_hi=(i+1)/2))
                    for i,(lo,hi) in enumerate(((job['start_ts'],middle),(middle,job['end_ts'])))]
                job['checksum']=descriptor_checksum(job)
                job_path.write_text(json.dumps(job),encoding='utf-8')

            if expect_unavailable:
                store.save_model(agent['id'],dict(version=11, marker='previous-approved'))
                previous_model = store.get_model(agent['id'])
                previous_agent = store.get_agent_config(agent['id'])

            if corrupt_table_page:
                with sqlite3.connect(db) as conn:
                    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    size = conn.execute('PRAGMA page_size').fetchone()[0]
                    page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='entity_history'").fetchone()[0]
                conn.close()
                raw = db.read_bytes()
                # Walk the real table b-tree to a leaf, keeping its interior root
                # readable so healthy physical ranges remain independently seekable.
                while raw[(page-1)*size] == 5:
                    offset = (page-1)*size
                    count = int.from_bytes(raw[offset+3:offset+5], 'big')
                    cell = int.from_bytes(raw[offset+12+2*(count//2):offset+14+2*(count//2)],'big')
                    page = int.from_bytes(raw[offset+cell:offset+cell+4],'big')
                with db.open('r+b') as stream:
                    stream.seek((page-1)*size)
                    stream.write(b'\xff')

            if corrupt_archive_indexes:
                # Modify only disposable index pages after closing SQLite readers.
                with sqlite3.connect(db) as connection:
                    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                    pages = [row[0] for row in connection.execute(
                        "SELECT rootpage FROM sqlite_master "
                        "WHERE type='index' AND tbl_name='entity_history' AND rootpage>0"
                    )]
                connection.close()
                with db.open("r+b") as stream:
                    for page in pages:
                        stream.seek((page - 1) * page_size)
                        stream.write(b"\xff")

            import training_process
            env = dict(os.environ)
            env["ADAPTIVE_AI_DATA"] = root
            env["ADAPTIVE_AI_TRAINING_WORKER"] = "1"
            if inherit_storage_budget:
                env.update(store.storage_budget.worker_environment())
            worker_args = [training_process.__file__, "--worker", str(job_path)]
            command = [sys.executable, *worker_args]
            if samples > 24:
                # Policy decay uses the wall clock even for replayed samples.
                # Hold that clock constant across benchmark runs.
                command = [
                    sys.executable, "-c",
                    "import policy, runpy, sys; stamp = float(sys.argv.pop(1)); "
                    "policy.now_ts = lambda: stamp; "
                    "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')",
                    str(job["end_ts"]), *worker_args,
                ]
                env["PYTHONHASHSEED"] = "0"
            started = time.perf_counter()
            try:
                import resource
                child_usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
            except ImportError:
                child_usage_before = None
            if kill_and_restart:
                # Keep an actual usable previous model, not just a UI status.
                store.save_model(agent['id'],healthy_model)
                previous_model = store.get_model(agent['id'])
                previous_agent = store.get_agent_config(agent['id'])
                probe_log = Path(root)/'kill-probe.log'
                probe_stream = probe_log.open('w',encoding='utf-8')
                checkpoint_ready=Path(root)/'committed-history-checkpoint'
                probe_code='''
import sys,runpy,time
from pathlib import Path
import policy,training_generation
stamp=float(sys.argv[1]); ready=Path(sys.argv[2]); args=sys.argv[3:]
policy.now_ts=lambda:stamp
original=training_generation.Preparation.save
def save(preparation, *args, **kwargs):
    original(preparation)
    if any(key.startswith('history:') and value.get('last') and not value.get('done')
           for key,value in preparation.state['queries'].items()):
        ready.write_text('committed real history batch')
        time.sleep(30)
training_generation.Preparation.save=save
sys.argv=args
runpy.run_path(args[0],run_name='__main__')
'''
                probe_command=[sys.executable,'-c',probe_code,str(job['end_ts']),str(checkpoint_ready),*worker_args]
                killed = subprocess.Popen(probe_command,env=env,cwd=str(Path(training_process.__file__).parent),
                    stdout=probe_stream,stderr=subprocess.STDOUT)
                deadline = time.monotonic()+30
                observed = False
                while killed.poll() is None and time.monotonic()<deadline:
                    try:
                        if checkpoint_ready.exists():
                            observed = True
                            break
                    except (OSError,ValueError):
                        pass
                    time.sleep(.005)
                killed.kill()
                killed.wait(timeout=15)
                probe_stream.close()
                self.assertTrue(observed,'committed recovery checkpoint was not observed: '+probe_log.read_text(errors='replace'))
                self.assertEqual(store.get_model(agent['id']),previous_model)
                self.assertEqual(store.get_agent_config(agent['id'])['training_cursor_ts'],previous_agent['training_cursor_ts'])
                # A new job generation is the restart contract; incomplete files
                # from the killed job cannot be mistaken for a completed result.
                job['result_path'] = str(Path(root)/'restart.result.json')
                job['checksum'] = descriptor_checksum(job)
                job_path.write_text(json.dumps(job),encoding='utf-8')
            writer_stop = threading.Event()
            writes = []
            writer_errors = []
            realtime_rows, ui_latency, writer_latency = [], [], []

            def write_metadata():
                try:
                    with sqlite3.connect(db, timeout=10) as connection, sqlite3.connect(db,timeout=1) as ui:
                        while not writer_stop.wait(.01):
                            began=time.perf_counter()
                            connection.execute(
                                "INSERT OR REPLACE INTO app_meta(key,value) VALUES('concurrent_probe',?)",
                                (str(len(writes)),),
                            )
                            if corruption_recovery:
                                stamp=job['end_ts']+1+len(realtime_rows)
                                connection.execute("INSERT INTO entity_history(entity_id,ts,received_ts,state,attributes_json,source) VALUES('sensor.realtime_probe',?,?,?,'{}','live')",
                                                   (stamp,stamp,str(len(realtime_rows))))
                            connection.commit()
                            writer_latency.append(time.perf_counter()-began)
                            if corruption_recovery:
                                realtime_rows.append(stamp)
                                began=time.perf_counter()
                                ui.execute('SELECT training_cursor_ts,mode FROM agents WHERE id=?',(agent['id'],)).fetchone()
                                ui_latency.append(time.perf_counter()-began)
                            writes.append(1)
                    connection.close()
                except Exception as exc:
                    writer_errors.append(exc)

            writer = threading.Thread(target=write_metadata) if concurrent_writes else None
            if writer:
                writer.start()
            try:
                completed = subprocess.run(
                    command,
                    env=env,
                    cwd=str(Path(training_process.__file__).parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=45 if samples == 24 else 600,
                )
            finally:
                writer_stop.set()
                if writer:
                    writer.join(15)
                    self.assertFalse(writer.is_alive())
            self.assertEqual(writer_errors, [])
            if realtime_rows:
                with store.conn() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM entity_history WHERE entity_id='sensor.realtime_probe'").fetchone()[0],len(realtime_rows))
            elapsed = time.perf_counter() - started
            if expect_unavailable:
                self.assertNotEqual(completed.returncode,0,completed.stdout)
                failed = json.loads(Path(job['result_path']).read_text())
                self.assertFalse(failed['ok'])
                self.assertEqual(store.get_model(agent['id']),previous_model)
                self.assertEqual(store.get_agent_config(agent['id'])['training_cursor_ts'],previous_agent['training_cursor_ts'])
                return dict(status='INSUFFICIENT',previous_model_retained=True,error=failed.get('error'))
            if completed.returncode:
                log = Path(job["log_path"])
                self.fail(
                    f"worker exited {completed.returncode}: {completed.stdout}\n"
                    + (log.read_text(errors="replace") if log.exists() else "")
                )
            result = json.loads(Path(job["result_path"]).read_text())
            self.assertTrue(result["ok"])
            if kill_and_restart:
                self.assertTrue((result.get('history_recovery') or {}).get('resumed'),result)
            if corruption_recovery:
                from history_recovery import publish_generation
                publish_generation(store,result['generation_path'],agent['id'],job['agent_fingerprint'],agent_config_fingerprint)
                if two_agents:
                    self.assertEqual(store.get_model(healthy['id']),healthy_model)
            self.assertNotEqual(os.getpid(), result["worker_pid"])
            model = store.get_model(agent["id"])
            self.assertIsNotNone(model)
            self.assertGreaterEqual(int(model.get("_history_watermark") or 0), 1)
            # A clean isolated process must publish the exact contract installed by
            # the shipped realtime runtime: Policy v11 / Observation Schema v12.
            # tests/test_model_contract_restart.py independently proves that this
            # contract survives a fresh Store/startup migration in Shadow.
            self.assertEqual(int(model.get("version") or 0), 11)
            self.assertEqual(
                int((model.get("schema") or {}).get("version") or 0),
                12,
            )
            learned = {
                key: model[key]
                for key in ("version", "actions", "horizons", "schema", "heads")
            }
            trained_agent = store.get_agent_config(agent["id"])
            child_metrics = dict(cpu_seconds=None,peak_rss_mb=None)
            if child_usage_before is not None:
                usage = resource.getrusage(resource.RUSAGE_CHILDREN)
                child_metrics = dict(cpu_seconds=usage.ru_utime+usage.ru_stime-child_usage_before.ru_utime-child_usage_before.ru_stime,
                                     peak_rss_mb=usage.ru_maxrss/1024.)
            if other_model_before is not None:
                self.assertEqual(store.get_model(other['id']),other_model_before)
                self.assertEqual(len(store.list_feedback(agent['id'])),1)
            return {
                "agent_mode": trained_agent["mode"],
                "agent_progress": trained_agent["training_progress"],
                "concurrent_writes": len(writes),
                "version": job["app_version"],
                "inherited_storage_budget": bool(inherit_storage_budget),
                "input_history_rows": len(rows),
                "worker_wall_seconds": elapsed,
                "worker_phases": result["training_phase_timings"],
                "worker_budget": result["training_budget"],
                "learning_hash": hashlib.sha256(json.dumps(
                    learned, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest(),
                "benchmark_counts": model.get("_benchmark_counts"),
                "frozen_holdout_counts": model.get("_frozen_holdout_counts"),
                "history_recovery": result.get('history_recovery'),
                "worker_resource_metrics": child_metrics,
                "source_table_rows": source_table_rows,
                "realtime_simulation": dict(ha_rows_written=len(realtime_rows),ui_reads=len(ui_latency),
                    ui_read_max_seconds=max(ui_latency,default=0.),writer_max_seconds=max(writer_latency,default=0.),
                    loss=0 if not writer_errors else None,environment='host SQLite ingress/read simulation; not physical Pi'),
            }


if __name__ == "__main__":
    unittest.main()
