"""0.14.58 regressions for periodic/background runtime QoS."""
import threading
import time
import unittest
from types import SimpleNamespace

import support

from engine import Engine
from settings import OPTIONS
from telemetry import HEAVY_JOBS


class FakeContext:
    def __init__(self):
        self.calls = []
        self.registry = {}

    def configure(self, state_map, **kwargs):
        self.calls.append((dict(state_map), dict(kwargs)))
        if "entities" in kwargs:
            self.registry = dict(kwargs["entities"])

    def resolved_registry(self):
        return dict(self.registry)


class FakePollWorker:
    def __init__(self):
        self.calls = []

    def submit(self, fn):
        self.calls.append(fn)
        return SimpleNamespace(done=lambda: False)


class Release058PeriodicQosTests(unittest.TestCase):
    def bare_engine(self):
        engine = Engine.__new__(Engine)
        engine.lock = threading.RLock()
        engine.state_map = {}
        engine.entity_registry = {}
        engine._entity_registry_raw = None
        engine._device_registry_raw = None
        engine._area_registry_raw = None
        engine.registry_refresh_stats = {
            "requests": 0, "coalesced": 0, "entity_updates": 0,
            "device_updates": 0, "area_updates": 0, "duplicates": 0,
            "last_duration_ms": 0.0, "max_duration_ms": 0.0,
        }
        engine.context = FakeContext()
        engine.models = {"agent-1": object()}
        engine.agent_index_at = 123.0
        engine.agent_index_revision = 99
        return engine

    def test_registry_refresh_keeps_warm_policy_models_and_dedupes_identical_payload(self):
        engine = self.bare_engine()
        payload = [{"entity_id": "binary_sensor.motion", "device_id": "dev-1"}]

        changed = Engine._registry_update(engine, "entity", payload)
        self.assertTrue(changed)
        self.assertIn("agent-1", engine.models)
        self.assertEqual(len(engine.context.calls), 1)
        self.assertEqual(engine.agent_index_at, 0.0)
        self.assertEqual(engine.agent_index_revision, -1)

        duplicate = Engine._registry_update(engine, "entity", payload)
        self.assertFalse(duplicate)
        self.assertEqual(len(engine.context.calls), 1)
        self.assertEqual(engine.registry_refresh_stats["duplicates"], 1)
        self.assertIn("agent-1", engine.models)

    def test_healthy_full_state_resync_waits_for_realtime_quiet_window(self):
        engine = Engine.__new__(Engine)
        engine.lock = threading.RLock()
        engine.ws_connected = True
        engine.last_full_poll = 0.0
        engine.poll_future = None
        engine.next_resync_retry_monotonic = 0.0
        engine.last_event_monotonic = time.monotonic()
        engine.poll_worker = FakePollWorker()
        engine.state_resync_stats = {
            "runs": 0, "failures": 0, "last_changed_entities": 0,
            "last_duration_ms": 0.0, "max_duration_ms": 0.0,
            "deferred_for_realtime": 0, "deferred_for_heavy_job": 0,
            "scheduled": 0,
        }
        with HEAVY_JOBS.lock:
            old_owner = HEAVY_JOBS.owner
            HEAVY_JOBS.owner = None
        try:
            self.assertFalse(Engine._maybe_schedule_state_resync(engine))
            self.assertEqual(engine.state_resync_stats["deferred_for_realtime"], 1)
            self.assertEqual(engine.poll_worker.calls, [])

            engine.last_event_monotonic = time.monotonic() - 3.0
            engine.next_resync_retry_monotonic = 0.0
            self.assertTrue(Engine._maybe_schedule_state_resync(engine))
            self.assertEqual(engine.state_resync_stats["scheduled"], 1)
            self.assertEqual(len(engine.poll_worker.calls), 1)
        finally:
            with HEAVY_JOBS.lock:
                HEAVY_JOBS.owner = old_owner

    def test_shipped_healthy_resync_default_is_fifteen_minutes(self):
        self.assertEqual(int(OPTIONS["realtime_resync_seconds"]), 900)
        config = (support.ROOT / "adaptive_ai/config.yaml").read_text(encoding="utf-8")
        self.assertIn("realtime_resync_seconds: 900", config)

    def test_engine_event_path_schedules_persistence_only_after_inference(self):
        source = (support.ROOT / "adaptive_ai/src/engine.py").read_text(encoding="utf-8")
        engine_source = source.split("class Engine(threading.Thread):", 1)[1]
        run = engine_source.split("    def run(self):", 1)[1].split("    def archive_live_states", 1)[0]
        self.assertNotIn("self.flush_archive(force=False)", run)
        self.assertNotIn("self.teaching.flush(force=False)", run)
        self.assertLess(run.index("self.process(state_map, changed_entities)"), run.index("self._schedule_housekeeping()"))
        self.assertLess(run.index("self._schedule_housekeeping()"), run.index("self._maybe_schedule_state_resync()"))

    def test_registry_event_stream_coalesces_duplicate_full_list_requests(self):
        source = (support.ROOT / "adaptive_ai/src/engine.py").read_text(encoding="utf-8")
        stream = source.split("class HAEventStream", 1)[1].split("class Engine", 1)[0]
        self.assertIn("registry_inflight", stream)
        self.assertIn("registry_dirty", stream)
        self.assertIn('registry_min_interval = 2.0', stream)
        self.assertIn("self.engine.registry_worker.submit(callback, payload)", stream)

    def test_shadow_maintenance_defers_behind_recent_realtime_event(self):
        candidate = (support.ROOT / "adaptive_ai/src/agent_candidate_shadow_runtime.py").read_text(encoding="utf-8")
        tournament = (support.ROOT / "adaptive_ai/src/context_tournament.py").read_text(encoding="utf-8")
        self.assertIn('getattr(manager.engine, "last_event_monotonic"', candidate)
        self.assertIn("now - last_event < 0.50", candidate)
        self.assertIn('getattr(self.engine, "last_event_monotonic"', tournament)
        self.assertIn("quiet_for < 0.75", tournament)


if __name__ == "__main__":
    unittest.main()
