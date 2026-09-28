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


class CandidateHeartbeatQoSTests(unittest.TestCase):
    def test_passive_candidate_refresh_bypasses_periodic_housekeeping_gate(self):
        counters = {"maintenance": 0, "deferred": 0, "passive": 0}
        store = FakeStore()
        engine = SimpleNamespace(last_event_monotonic=0.0)
        core = SimpleNamespace(OPTIONS={}, STORE=store, ENGINE=engine)
        manager = SimpleNamespace(poll_seconds=0.5)

        def base_maintenance():
            counters["maintenance"] += 1
            return "maintenance"

        def drain_deferred(*, max_roots=4):
            self.assertEqual(max_roots, 4)
            counters["deferred"] += 1
            return 0

        def drain_passive(*, max_roots=2):
            self.assertEqual(max_roots, 2)
            counters["passive"] += 1
            return 0

        manager._maintenance = base_maintenance
        manager.drain_deferred_candidate_shadow = drain_deferred
        manager.drain_candidate_shadow_events = drain_passive

        with patch.object(low_power, "TRAINING_BUDGET", FakeBudget()):
            low_power.install(core, manager)

        engine.last_event_monotonic = time.monotonic()
        self.assertIsNone(manager._maintenance())
        self.assertEqual(
            counters,
            {"maintenance": 0, "deferred": 1, "passive": 1},
        )

        # Housekeeping remains gated, while both Candidate runtime drains keep running
        # on subsequent worker turns.
        self.assertIsNone(manager._maintenance())
        self.assertEqual(
            counters,
            {"maintenance": 0, "deferred": 2, "passive": 2},
        )


if __name__ == "__main__":
    unittest.main()
