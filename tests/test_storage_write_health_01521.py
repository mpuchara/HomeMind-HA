"""Storage write telemetry must not change checkpoint or persistence behavior."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import support
from storage import Store
from storage_write_health import StorageWriteHealth, read_proc_io
from wal_checkpoint import WALCheckpointWorker


class StorageWriteHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.reader = Mock(return_value=(1000, 10))
        self.health = StorageWriteHealth(clock=lambda: self.now, reader=self.reader)

    def sample(self, now, written, cancelled=10):
        self.now = float(now)
        self.reader.return_value = (written, cancelled)
        return self.health.sample()

    def test_rate_uses_counter_delta_and_elapsed_time_not_wal_size(self):
        self.assertTrue(self.sample(0, 1000))
        self.assertFalse(self.sample(10, 1200))
        self.assertTrue(self.sample(30, 1300))
        self.assertIsNone(self.health.snapshot()["recent_write_bytes_per_hour"])
        self.assertTrue(self.sample(90, 1900))
        result = self.health.snapshot()
        self.assertEqual(result["recent_write_bytes_per_hour"], 36000.)
        self.assertEqual(result["hour_write_bytes_per_hour"], 36000.)
        self.assertEqual(result["observed_write_bytes"], 900)
        self.assertEqual(result["process_cancelled_write_bytes"], 10)
        self.assertEqual(self.reader.call_count, 3)

    def test_idle_is_zero_not_unavailable_and_ring_is_bounded(self):
        for i in range(160):
            self.sample(i*30, 1000)
        result = self.health.snapshot()
        self.assertEqual(result["sample_count"], 121)
        self.assertEqual(result["recent_window_seconds"], 300.)
        self.assertEqual(result["hour_window_seconds"], 3600.)
        self.assertEqual(result["recent_write_bytes_per_hour"], 0.)
        self.assertEqual(result["hour_write_bytes_per_hour"], 0.)

    def test_missing_stale_and_counter_reset_never_invent_rates(self):
        self.sample(0, 1000)
        self.sample(60, 1600)
        self.now = 151.
        self.assertTrue(self.health.snapshot()["stale"])
        self.assertIsNone(self.health.snapshot()["recent_write_bytes_per_hour"])
        self.sample(151, 5)
        self.assertEqual(self.health.snapshot()["counter_resets"], 1)
        self.assertEqual(self.health.snapshot()["observed_write_bytes"], 0)
        self.reader.side_effect = OSError("proc unavailable")
        self.now = 181.
        self.assertFalse(self.health.sample())
        self.assertFalse(self.health.snapshot()["available"])
        self.assertIsNone(self.health.snapshot()["observed_write_bytes"])
        self.reader.side_effect = None
        self.sample(211, 800)
        self.assertEqual(self.health.snapshot()["observed_write_bytes"], 0)
        self.assertIsNone(self.health.snapshot()["hour_write_bytes_per_hour"])

    def test_proc_parser_requires_write_counter(self):
        with patch("storage_write_health.Path.read_text",
                   return_value="rchar: 7\nwrite_bytes: 4096\ncancelled_write_bytes: 32\n"):
            self.assertEqual(read_proc_io(), (4096, 32))
        with patch("storage_write_health.Path.read_text", return_value="rchar: 7\n"):
            with self.assertRaises(ValueError):
                read_proc_io()

    def test_worker_failure_is_nonblocking_for_storage_sampling_and_snapshot_is_ram_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/"test.db")
            worker = WALCheckpointWorker(store)
            worker.write_health = self.health
            self.reader.side_effect = PermissionError("no proc")
            self.assertIsNotNone(worker.sample_storage())
            self.assertEqual(self.reader.call_count, 1)
            store._wal_checkpoint_worker = worker
            with patch.object(store, "conn", side_effect=AssertionError("SQL")), \
                 patch("pathlib.Path.read_text", side_effect=AssertionError("file read")):
                result = store.sqlite_snapshot()["wal_checkpoint"]["write_health"]
            self.assertFalse(result["available"])
            self.assertEqual(result["error"], "PermissionError")
            self.assertEqual(self.reader.call_count, 1)
