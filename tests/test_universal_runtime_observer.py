"""U04b observer is read-only, opt-in and bounded under real HA event shapes."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock
import threading
import time
import unittest

import settings
from universal_runtime_observer import UniversalRuntimeObserver, install


def stamp(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def state(eid, value, ts, *, cls="temperature", unit="°C"):
    return {
        "entity_id": eid,
        "state": str(value),
        "last_updated": stamp(ts),
        "last_changed": stamp(ts),
        "attributes": {"device_class": cls, "unit_of_measurement": unit},
    }


def eventually(test, *, timeout=4.0, reason="observer has not converged"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if test():
            return
        time.sleep(0.015)
    raise AssertionError(reason)


class OptionalInstallerTests(unittest.TestCase):
    def test_disabled_installer_creates_no_worker_or_cache(self):
        engine = SimpleNamespace()
        with mock.patch.dict(settings.OPTIONS, {"universal_observer_enabled": False}):
            self.assertIsNone(install(engine))
        self.assertFalse(hasattr(engine, "universal_observer"))

    def test_enabled_installer_is_idempotent_and_worker_stops(self):
        engine = SimpleNamespace()
        with mock.patch.dict(settings.OPTIONS, {"universal_observer_enabled": True}):
            first = install(engine)
            try:
                self.assertIs(first, install(engine))
                eventually(lambda: first.diagnostics()["worker_alive"])
                self.assertEqual(first.diagnostics()["mode"], "observer_only")
            finally:
                first.stop()
        self.assertFalse(first.is_alive())


class RealHAObserverTests(unittest.TestCase):
    def setUp(self):
        self.observer = UniversalRuntimeObserver(
            max_pending=12, batch_size=3, pause_seconds=0.0, max_snapshot=100
        )
        self.observer.start()

    def tearDown(self):
        self.observer.stop()
        self.assertFalse(self.observer.is_alive())

    def test_rest_bootstrap_then_ws_change_without_action_dispatch(self):
        now = time.time()
        snapshot = {
            "sensor.kitchen": state("sensor.kitchen", 21, now - 10),
            "sensor.bath": state("sensor.bath", 20, now - 10),
            "light.lamp": {"entity_id": "light.lamp", "state": "on"},
        }
        self.assertTrue(self.observer.submit_snapshot(snapshot, received_ts=now))
        eventually(lambda: self.observer.diagnostics()["healthy"]
                   and self.observer.diagnostics()["neural"].get("sensor_count") == 2)
        before = self.observer.diagnostics()
        self.assertEqual(before["physical_actions"], 0)
        self.assertFalse(before["trained"])
        self.assertEqual(before["neural"]["token_encoder_calls"], 2)
        ts2 = time.time() - 0.01
        self.assertTrue(self.observer.submit_event(
            "sensor.kitchen", state("sensor.kitchen", 22, ts2),
            event_ts=ts2, received_ts=time.time()
        ))
        eventually(lambda: self.observer.diagnostics()["neural"]
                   .get("token_encoder_calls", 0) >= 3)
        after = self.observer.diagnostics()
        self.assertEqual(after["neural"]["sensor_count"], 2)
        self.assertEqual(after["neural"]["room_count"], 1)  # no registry -> unassigned
        self.assertEqual(after["neural"]["physical_actions"], 0)
        self.assertEqual(after["stats"]["snapshots"], 1)

    def test_registry_area_moves_refresh_only_in_worker(self):
        now = time.time()
        self.observer.submit_snapshot(
            {"sensor.kitchen": state("sensor.kitchen", 21, now - 2)}, received_ts=now
        )
        eventually(lambda: self.observer.diagnostics()["healthy"]
                   and self.observer.diagnostics()["neural"].get("sensor_count") == 1)
        self.assertTrue(self.observer.submit_registry(
            "entity",
            [{"entity_id": "sensor.kitchen", "device_id": "device-1", "area_id": "kitchen"}]
        ))
        eventually(lambda: self.observer.diagnostics()["stats"]["registry_rebuilds"] >= 1
                   and self.observer.diagnostics()["healthy"])
        self.assertEqual(self.observer.diagnostics()["neural"]["room_count"], 1)
        self.observer.submit_registry(
            "entity",
            [{"entity_id": "sensor.kitchen", "device_id": "device-1", "area_id": "bath"}]
        )
        eventually(lambda: self.observer.diagnostics()["stats"]["registry_rebuilds"] >= 2
                   and self.observer.diagnostics()["healthy"])
        self.assertEqual(self.observer.diagnostics()["neural"]["sensor_count"], 1)
        self.assertEqual(self.observer.diagnostics()["physical_actions"], 0)

    def test_accepted_ws_tombstone_removes_sensor(self):
        now = time.time()
        self.observer.submit_snapshot(
            {"sensor.kitchen": state("sensor.kitchen", 20, now - 1)}, received_ts=now
        )
        eventually(lambda: self.observer.diagnostics()["healthy"]
                   and self.observer.diagnostics()["neural"].get("sensor_count") == 1)
        self.observer.submit_event("sensor.kitchen", None,
                                   event_ts=time.time(), received_ts=time.time())
        eventually(lambda: self.observer.diagnostics()["neural"].get("sensor_count") == 0)
        self.assertEqual(self.observer.diagnostics()["neural"]["room_count"], 0)

    def test_bounded_mailbox_overflow_marks_unhealthy_until_full_resync(self):
        other = UniversalRuntimeObserver(max_pending=1, batch_size=1, pause_seconds=0)
        now = time.time()
        try:
            # Fill before worker starts: test the exact queue capacity boundary.
            self.assertTrue(other.submit_event(
                "sensor.a", state("sensor.a", 20, now), event_ts=now, received_ts=now
            ))
            self.assertFalse(other.submit_event(
                "sensor.b", state("sensor.b", 21, now), event_ts=now, received_ts=now
            ))
            other.start()
            eventually(lambda: other.diagnostics()["reconcile_required"])
            self.assertFalse(other.diagnostics()["healthy"])
            self.assertFalse(other.submit_event(
                "sensor.c", state("sensor.c", 22, now), event_ts=now, received_ts=now
            ))
            self.assertTrue(other.submit_snapshot(
                {"sensor.c": state("sensor.c", 22, now)}, received_ts=time.time()
            ))
            eventually(lambda: other.diagnostics()["healthy"]
                       and other.diagnostics()["neural"].get("sensor_count") == 1)
            self.assertFalse(other.diagnostics()["reconcile_required"])
            self.assertEqual(other.diagnostics()["stats"]["overflow"], 1)
        finally:
            other.stop()

    def test_later_packet_cannot_override_newer_event(self):
        now = time.time()
        first = now - 8
        self.observer.submit_snapshot(
            {"sensor.kitchen": state("sensor.kitchen", 20, first)}, received_ts=now
        )
        eventually(lambda: self.observer.diagnostics()["healthy"])
        n1 = self.observer.diagnostics()["neural"].get("token_encoder_calls")
        newer = now - 2
        self.observer.submit_event(
            "sensor.kitchen", state("sensor.kitchen", 22, newer),
            event_ts=newer, received_ts=time.time(),
        )
        eventually(lambda: self.observer.diagnostics()["neural"]
                   .get("token_encoder_calls", 0) > n1)
        n2 = self.observer.diagnostics()["neural"]["token_encoder_calls"]
        older = now - 4
        self.observer.submit_event(
            "sensor.kitchen", state("sensor.kitchen", 99, older),
            event_ts=older, received_ts=time.time(),
        )
        eventually(lambda: self.observer.diagnostics()["stats"]["processed"] >= 3)
        self.assertEqual(self.observer.diagnostics()["neural"]["token_encoder_calls"], n2)

    def test_snapshot_max_is_explicit_not_silent_partial_state(self):
        other = UniversalRuntimeObserver(max_snapshot=1)
        now = time.time()
        try:
            other.start()
            oversized = {
                "sensor.a": state("sensor.a", 20, now),
                "sensor.b": state("sensor.b", 20, now),
            }
            self.assertFalse(other.submit_snapshot(oversized, received_ts=now))
            self.assertTrue(other.diagnostics()["reconcile_required"])
            self.assertFalse(other.diagnostics()["healthy"])
        finally:
            other.stop()


if __name__ == "__main__":
    unittest.main()
