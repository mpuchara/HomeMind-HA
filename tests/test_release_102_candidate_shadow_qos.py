import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import rpi_low_power_runtime as low_power


class FakeBudget:
    def configure(self, **kwargs):
        self.kwargs = dict(kwargs)

    def checkpoint(self, *args, **kwargs):
        return None

    def snapshot(self):
        return {}


class FakeStore:
    def archive_iter(self, *args, **kwargs):
        return iter(())

    def event(self, *args, **kwargs):
        return None


class CandidateShadowLowPowerQoSTests(unittest.TestCase):
    def test_deferred_shadow_drain_bypasses_periodic_maintenance_gate(self):
        counters = {"maintenance": 0, "drain": 0}
        store = FakeStore()
        engine = SimpleNamespace(last_event_monotonic=0.0)
        core = SimpleNamespace(OPTIONS={}, STORE=store, ENGINE=engine)
        manager = SimpleNamespace(poll_seconds=0.5)

        def base_maintenance():
            counters["maintenance"] += 1
            return "maintenance"

        def drain_deferred(*, max_roots=4):
            self.assertEqual(max_roots, 4)
            counters["drain"] += 1
            return 1

        manager._maintenance = base_maintenance
        manager.drain_deferred_candidate_shadow = drain_deferred

        with patch.object(low_power, "TRAINING_BUDGET", FakeBudget()):
            low_power.install(core, manager)

        # A fresh HA event intentionally suppresses periodic housekeeping, but the
        # deferred Candidate queue must still run immediately on the worker wake.
        engine.last_event_monotonic = time.monotonic()
        self.assertIsNone(manager._maintenance())
        self.assertEqual(counters, {"maintenance": 0, "drain": 1})

        # Once housekeeping is allowed, it runs normally.
        engine.last_event_monotonic = 0.0
        self.assertEqual(manager._maintenance(), "maintenance")
        self.assertEqual(counters, {"maintenance": 1, "drain": 2})

        # The 60 s maintenance interval still blocks housekeeping, not Candidate work.
        self.assertIsNone(manager._maintenance())
        self.assertEqual(counters, {"maintenance": 1, "drain": 3})
        self.assertEqual(manager.poll_seconds, low_power.DEFAULT_CANDIDATE_IDLE_POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
