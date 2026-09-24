"""0.14.80 contracts for process-isolated historical training."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
