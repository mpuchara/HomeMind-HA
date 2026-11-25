"""Status transport regressions; durable recovery files remain authoritative."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import training_status_transport as transport
from training_process import preparation_progress_token, _worker_boot_status, _worker_status_writer
from training_status_transport import StatusWriter, latest_status


class TrainingStatusTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.disk = Path(self.temp.name)/"durable.json"
        self.ram = Path(self.temp.name)/"volatile.json"
        self.now = 0.
        self.persist = Mock(side_effect=lambda path, data: Path(path).write_text(json.dumps(data)))
        self.writer = StatusWriter(self.disk, self.ram, self.persist, json.dumps,
                                   clock=lambda: self.now)

    def publish(self, stamp, progress, stage="replay", **details):
        self.now = stamp
        value = dict(progress=progress, updated_at=stamp, **details)
        self.writer.publish(value, stage)
        return value

    def test_full_live_progress_and_bounded_durable_writes(self):
        for stamp in range(61):
            latest = self.publish(stamp, stamp/100)
            self.assertEqual(json.loads(self.ram.read_text()), latest)
        self.assertEqual(self.persist.call_count, 3)
        self.assertEqual(json.loads(self.disk.read_text()), latest)
        self.publish(61, .61)
        self.assertEqual(self.persist.call_count, 3)
        self.writer.flush()
        self.assertEqual(self.persist.call_count, 4)
        self.assertEqual(json.loads(self.disk.read_text())["progress"], .61)

    def test_stage_transition_and_watchdog_cursor_preserved(self):
        self.publish(0, .01, "prepare")
        for stamp in range(1, 10):
            preparation = dict(query_key="sensor.a", current_cursor=stamp, copy_strategy="copy")
            value = self.publish(stamp, .01, "prepare", preparation=preparation)
            _, live = latest_status(self.disk, self.ram)
            self.assertEqual(live, value)
            self.assertEqual(preparation_progress_token(live["preparation"]),
                             preparation_progress_token(preparation))
        self.assertEqual(self.persist.call_count, 1)
        final = self.publish(10, .1, "replay")
        self.assertEqual(self.persist.call_count, 2)
        self.assertEqual(json.loads(self.disk.read_text()), final)

    def test_ram_failure_permanently_falls_back_to_guarded_disk_writer(self):
        self.publish(0, 0.)
        with patch.object(self.writer, "_write_ram", side_effect=OSError("full")) as fail:
            latest = self.publish(1, .1)
            self.assertIsNone(self.writer.volatile)
            self.assertFalse(self.ram.exists())
            self.assertEqual(latest_status(self.disk, self.ram)[1], latest)
            self.publish(2, .2)
            self.assertEqual(fail.call_count, 1)
        self.assertEqual(self.persist.call_count, 3)

    def test_durable_failure_is_not_hidden_by_ram(self):
        self.publish(0, 0.)
        self.persist.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            self.publish(1, .1, "new-stage")
        self.assertEqual(json.loads(self.ram.read_text())["progress"], 0.)

    def test_unsupported_platform_and_direct_worker_keep_disk_contract(self):
        with patch.object(transport, "_verified_tmpfs", return_value=False):
            with transport.volatile_status_path() as path:
                self.assertIsNone(path)
            self.assertIsNone(transport.worker_status_path())
        writer = StatusWriter(self.disk, None, self.persist, json.dumps)
        writer.publish({"progress": 0.}, "stage")
        writer.publish({"progress": .1}, "stage")
        self.assertEqual(self.persist.call_count, 2)
        self.assertEqual(json.loads(self.disk.read_text())["progress"], .1)

    def test_pending_snapshot_is_detached_and_oversized_ram_payload_falls_back(self):
        self.publish(0, 0.)
        nested = {"rows": [1]}
        self.publish(1, .1, nested=nested)
        nested["rows"].append(2)
        self.writer.flush()
        self.assertEqual(json.loads(self.disk.read_text())["nested"], {"rows": [1]})
        self.publish(2, .2, data="x" * transport.MAX_RAM_PAYLOAD)
        self.assertIsNone(self.writer.volatile)
        self.assertEqual(json.loads(self.disk.read_text())["progress"], .2)

    def test_worker_status_wrappers_keep_progress_and_eta_in_ram(self):
        job = {"status_path": str(self.disk), "train_kwargs": {"progress_lo": .01},
               "training_generation": True}
        with patch("training_process.time.monotonic", side_effect=lambda: self.now):
            _worker_boot_status(job, "prepare", stage="history_recovery", transport=self.writer,
                                preparation={"query_key": "sensor.a", "current_cursor": 1})
            self.now = 2.
            _worker_boot_status(job, "prepare", stage="history_recovery", transport=self.writer,
                                preparation={"query_key": "sensor.a", "current_cursor": 2})
            self.assertEqual(latest_status(self.disk, self.ram)[1]["preparation"]["current_cursor"], 2)
            self.assertEqual(self.persist.call_count, 1)
            h = SimpleNamespace(phase="training", progress=.2, message="replay",
                                phase_detail="replay", set_status=lambda **kw: None)
            h.status = lambda: {"phase": h.phase, "progress": h.progress, "eta_seconds": 120.}
            _worker_status_writer(h, self.disk, preparation_fraction=.1, transport=self.writer)
            self.now = 4.
            h.set_status()
            self.now = 6.
            h.progress = .3
            h.set_status()
            _, status = latest_status(self.disk, self.ram)
            self.assertAlmostEqual(status["progress"], .37)
            self.assertEqual(status["eta_seconds"], 120.)
            self.assertEqual(self.persist.call_count, 2)

    def test_mailbox_is_private_per_job_and_removed_after_scope(self):
        if not transport._verified_tmpfs():
            self.skipTest("verified tmpfs not available")
        with transport.volatile_status_path() as first:
            with transport.volatile_status_path() as second:
                if first is None or second is None:
                    self.skipTest("tmpfs not writable")
                self.assertNotEqual(first, second)
                with patch.dict(os.environ, {transport.STATUS_ENV: str(first)}):
                    self.assertEqual(transport.worker_status_path(), first)
                # Verify a fresh child can resolve and publish to the exact mailbox.
                env = dict(os.environ)
                env[transport.STATUS_ENV] = str(first)
                env["PYTHONPATH"] = str(Path(transport.__file__).parent)
                subprocess.run([sys.executable, "-c",
                    "from training_status_transport import worker_status_path; "
                    "worker_status_path().write_text('{\"progress\":0.5}')"],
                    env=env, check=True, timeout=10)
                self.assertEqual(json.loads(first.read_text()), {"progress": .5})
            self.assertFalse(second.parent.exists())
        self.assertFalse(first.parent.exists())
