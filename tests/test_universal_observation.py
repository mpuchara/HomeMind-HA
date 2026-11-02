"""Contract tests for the opt-in universal variable-cardinality observation layer."""
import math
import unittest

from universal_observation import (
    CONTRACT_VERSION,
    UNASSIGNED_AREA,
    UniversalObservationIndex,
    encode_sensor,
)


def sensor(entity, value, *, area="kitchen", unit=None, device_class=None,
           at=10.0, event=None, received=None, ttl=900.0, quality=1.0,
           calibration=None):
    attrs = {}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    if device_class is not None:
        attrs["device_class"] = device_class
    return encode_sensor(
        entity, {"state": value, "attributes": attrs},
        area_id=area, at_ts=at,
        event_ts=at if event is None else event,
        received_ts=at if received is None else received,
        max_age_seconds=ttl, quality=quality, calibration=calibration,
    )


class TypedSensorTests(unittest.TestCase):
    def test_version_and_thermal_unit_parity(self):
        self.assertEqual(CONTRACT_VERSION, 1)
        one = sensor("sensor.temp", 20, unit="°C", device_class="temperature")
        two = sensor("sensor.temp", 68, unit="°F", device_class="temperature")
        three = sensor("sensor.temp", 293.15, unit="K", device_class="temperature")
        self.assertEqual({x.kind for x in (one, two, three)}, {"temperature"})
        for token in (one, two, three):
            self.assertTrue(token.available)
            self.assertAlmostEqual(token.value, 0, places=6)

    def test_different_physical_scales_are_bounded(self):
        observations = [
            sensor("sensor.h", 65, unit="%", device_class="humidity"),
            sensor("sensor.lux", 2500, unit="lx", device_class="illuminance"),
            sensor("sensor.power", 3.2, unit="kW", device_class="power"),
            sensor("sensor.co2", 1200, unit="ppm", device_class="carbon_dioxide"),
            sensor("binary_sensor.motion", "on"),
        ]
        self.assertEqual([x.kind for x in observations],
                         ["humidity", "illuminance", "power", "co2", "binary"])
        self.assertAlmostEqual(observations[0].value, 0.65)
        self.assertEqual(observations[-1].value, 1.0)
        self.assertTrue(all(x.available and math.isfinite(x.value) for x in observations))
        self.assertTrue(all(-6.0 <= x.value <= 6.0 for x in observations))

    def test_missing_is_not_numeric_zero_and_unknown_scale_requires_calibration(self):
        absent = sensor("sensor.h", "unavailable", unit="%", device_class="humidity")
        zero = sensor("sensor.h", 0, unit="%", device_class="humidity")
        self.assertEqual(absent.mask, 0.0)
        self.assertEqual(zero.mask, 1.0)
        self.assertEqual(absent.reason, "unavailable")
        self.assertFalse(sensor("sensor.radar_energy", 38).available)
        calibrated = sensor("sensor.radar_energy", 38, calibration={"center": 18, "scale": 10})
        self.assertTrue(calibrated.available)
        self.assertEqual(calibrated.kind, "generic")
        self.assertAlmostEqual(calibrated.value, 2.0)
        self.assertFalse(sensor("sensor.temp", 22, unit="banana",
                                device_class="temperature").available)

    def test_future_stale_and_invalid_readings_are_masked(self):
        future = sensor("sensor.lux", 1000, unit="lx", at=10, event=11, received=9)
        delayed = sensor("sensor.lux", 1000, unit="lx", at=10, event=8, received=11)
        stale = sensor("binary_sensor.motion", "off", at=20, event=5, received=5, ttl=10)
        invalid = sensor("sensor.temperature", "nan", unit="°C")
        for token in (future, delayed, stale, invalid):
            self.assertFalse(token.available)
            self.assertEqual(token.value, 0.0)
        self.assertEqual(future.reason, "future_observation")
        self.assertEqual(delayed.reason, "future_observation")
        self.assertEqual(stale.reason, "stale")

    def test_bad_quality_and_invalid_calibration_rejected_or_masked(self):
        with self.assertRaises(ValueError):
            sensor("sensor.temp", 20, unit="°C", quality=1.5)
        with self.assertRaises(ValueError):
            sensor("sensor.temp", 20, unit="°C", ttl=0)
        self.assertFalse(sensor("sensor.x", 3, calibration={"center": 0, "scale": 0}).available)


class DynamicIndexTests(unittest.TestCase):
    def test_zero_one_and_thousand_inputs_same_contract(self):
        idx = UniversalObservationIndex()
        self.assertEqual(idx.pool(), ())
        for i in range(1000):
            token = sensor(f"binary_sensor.motion_{i}", "on" if i % 2 else "off")
            self.assertTrue(idx.upsert(token, now=10))
        self.assertEqual(idx.sensor_count, 1000)
        pool = idx.pool("kitchen")
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["count"], 1000)
        self.assertEqual(pool[0]["available"], 1000)
        self.assertAlmostEqual(pool[0]["mean"], 0.5)
        self.assertEqual(len(idx.tokens("kitchen")), 1000)

    def test_permutation_invariant_pooling(self):
        observations = [
            sensor("sensor.h1", 10, unit="%", device_class="humidity", quality=0.5),
            sensor("sensor.h2", 90, unit="%", device_class="humidity", quality=1.0),
            sensor("sensor.h3", "unavailable", unit="%", device_class="humidity"),
            sensor("binary_sensor.motion", "off"),
        ]
        a, b = UniversalObservationIndex(), UniversalObservationIndex()
        for token in observations:
            a.upsert(token, now=10)
        for token in reversed(observations):
            b.upsert(token, now=10)
        self.assertEqual(a.pool(), b.pool())
        humidity = next(row for row in a.pool() if row["kind"] == "humidity")
        self.assertEqual(humidity["count"], 3)
        self.assertEqual(humidity["available"], 2)
        self.assertAlmostEqual(humidity["mean"], (0.1 * 0.5 + 0.9) / 1.5)
        self.assertAlmostEqual(humidity["coverage"], 2 / 3)

    def test_replacement_relocation_remove_and_late_event(self):
        idx = UniversalObservationIndex()
        idx.upsert(sensor("binary_sensor.presence", "on", area="kitchen", at=10), now=10)
        self.assertFalse(idx.upsert(
            sensor("binary_sensor.presence", "off", area="kitchen", at=9), now=10))
        idx.upsert(sensor("binary_sensor.presence", "off", area="bath", at=11), now=11)
        self.assertEqual(idx.pool("kitchen"), ())
        self.assertEqual(idx.pool("bath")[0]["mean"], 0.0)
        self.assertEqual(idx.sensor_count, 1)
        self.assertTrue(idx.remove("binary_sensor.presence"))
        self.assertEqual(idx.sensor_count, 0)
        self.assertEqual(idx.pool(), ())
        self.assertFalse(idx.remove("binary_sensor.presence"))

    def test_expiry_is_event_driven_without_full_sensor_scan(self):
        idx = UniversalObservationIndex()
        token = sensor("binary_sensor.motion", "on", at=5, event=5, received=5, ttl=15)
        idx.upsert(token, now=5)
        idx.advance(19)
        self.assertEqual(idx.pool()[0]["available"], 1)
        idx.advance(20)
        self.assertEqual(idx.pool()[0]["available"], 0)
        self.assertEqual(idx.tokens()[0].reason, "stale")
        idx.upsert(sensor("binary_sensor.motion", "off", at=22, ttl=15), now=22)
        self.assertEqual(idx.pool()[0]["available"], 1)
        self.assertEqual(idx.pool()[0]["mean"], 0)
        idx.advance(37)
        self.assertEqual(idx.pool()[0]["available"], 0)

    def test_monotonic_live_clock_and_no_future_data(self):
        idx = UniversalObservationIndex()
        with self.assertRaises(ValueError):
            idx.upsert(sensor("binary_sensor.motion", "on", at=12), now=11)
        idx.advance(12)
        with self.assertRaises(ValueError):
            idx.advance(11)
        with self.assertRaises(ValueError):
            idx.upsert(sensor("binary_sensor.motion", "on", at=13), now=12)
        self.assertEqual(idx.sensor_count, 0)

    def test_repeated_sensor_updates_keep_expiration_index_bounded(self):
        idx = UniversalObservationIndex()
        for stamp in range(1, 1001):
            idx.upsert(sensor("binary_sensor.motion", "on", at=stamp, ttl=900), now=stamp)
        self.assertEqual(idx.sensor_count, 1)
        self.assertLessEqual(idx.diagnostics()["pending_expiration_entries"], 128)
        idx.advance(1900)
        self.assertEqual(idx.pool()[0]["available"], 0)

    def test_unassigned_area_is_explicit(self):
        idx = UniversalObservationIndex()
        idx.upsert(sensor("binary_sensor.motion", "off", area=None), now=10)
        self.assertEqual(idx.pool()[0]["area_id"], UNASSIGNED_AREA)


if __name__ == "__main__":
    unittest.main()
