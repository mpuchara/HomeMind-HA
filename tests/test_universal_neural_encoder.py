"""Deterministic U03 Deep Sets, incremental invalidation and checkpoint tests."""
import copy
import json
import unittest

import numpy as np

from universal_neural_encoder import (
    INPUT_WIDTH, KINDS, IncrementalNeuralHome, SharedSetEncoder,
)
from universal_observation import encode_sensor


def observation(number, *, area="kitchen", at=100.0, valid=True, kind="temperature",
                ttl=1000.0, quality=1.0):
    unit, device_class = {
        "temperature": ("°C", "temperature"),
        "humidity": ("%", "humidity"),
        "illuminance": ("lx", "illuminance"),
        "power": ("W", "power"),
        "co2": ("ppm", "carbon_dioxide"),
        "binary": (None, "motion"),
        "generic": ("raw", None),
    }[kind]
    attrs = {"device_class": device_class}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    entity = (
        f"binary_sensor.motion_{number}" if kind == "binary"
        else f"sensor.sensor_{number}"
    )
    raw = {
        "temperature": 22 + number % 4,
        "humidity": 30 + number % 20,
        "illuminance": 100 + number % 1000,
        "power": 200 + number % 500,
        "co2": 600 + number % 1000,
        "binary": "on" if number % 2 else "off",
        "generic": 100 + number % 50,
    }[kind]
    if not valid:
        raw = "unavailable"
    return encode_sensor(
        entity, {"state": raw, "attributes": attrs},
        area_id=area, at_ts=at, event_ts=at, received_ts=at,
        quality=quality, max_age_seconds=ttl,
        calibration={"center": 100, "scale": 25} if kind == "generic" else None,
    )


class SharedSetEncoderTests(unittest.TestCase):
    def test_checkpoint_roundtrip_is_json_safe_and_identical(self):
        model = SharedSetEncoder(token_width=8, room_width=12, hidden_width=16, seed=44)
        token = observation(2, kind="illuminance")
        data = json.loads(json.dumps(model.checkpoint()))
        rebuilt = SharedSetEncoder(token_width=8, room_width=12, hidden_width=16,
                                   checkpoint=data, seed=123)
        self.assertFalse(data["trained"])
        self.assertEqual(tuple(data["kinds"]), KINDS)
        self.assertGreater(model.parameter_count, 0)
        self.assertEqual(model.token_features(token).shape, (INPUT_WIDTH,))
        np.testing.assert_array_equal(model.token_embedding(token),
                                      rebuilt.token_embedding(token))
        a = IncrementalNeuralHome(model)
        b = IncrementalNeuralHome(rebuilt)
        for i in range(25):
            t = observation(i, kind=KINDS[i % len(KINDS)])
            a.upsert(t, now=100)
            b.upsert(t, now=100)
        np.testing.assert_array_equal(a.home(), b.home())

    def test_rejects_corrupt_or_incompatible_checkpoints(self):
        model = SharedSetEncoder(token_width=8, room_width=12, hidden_width=16)
        checkpoint = model.checkpoint()
        for name, value in [
            ("contract_version", 777),
            ("kinds", ["changed-kind"]),
            ("token_width", 2),
        ]:
            bad = copy.deepcopy(checkpoint)
            bad[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                SharedSetEncoder(token_width=8, room_width=12, hidden_width=16,
                                 checkpoint=bad)
        bad = copy.deepcopy(checkpoint)
        bad["layers"]["token1"]["weights"][0][0] = float("nan")
        with self.assertRaises(ValueError):
            SharedSetEncoder(token_width=8, room_width=12, hidden_width=16,
                             checkpoint=bad)
        bad = copy.deepcopy(checkpoint)
        bad["layers"]["room1"]["bias"] = [0]
        with self.assertRaises(ValueError):
            SharedSetEncoder(token_width=8, room_width=12, hidden_width=16,
                             checkpoint=bad)

    def test_available_zero_and_missing_are_distinct(self):
        model = SharedSetEncoder()
        zero = observation(0, kind="binary", valid=True)
        missing = observation(0, kind="binary", valid=False)
        self.assertEqual(zero.value, missing.value)
        self.assertNotEqual(zero.mask, missing.mask)
        self.assertNotEqual(
            model.token_features(zero).tolist(),
            model.token_features(missing).tolist(),
        )


class IncrementalNeuralHomeTests(unittest.TestCase):
    def test_zero_one_and_thousand_sensors_have_same_output_width(self):
        model = SharedSetEncoder(token_width=8, room_width=12, hidden_width=16)
        for size in (0, 1, 5, 100, 1000):
            with self.subTest(size=size):
                cache = IncrementalNeuralHome(model)
                for i in range(size):
                    cache.upsert(
                        observation(i, area=f"room_{i % 13}",
                                    kind=KINDS[i % len(KINDS)]),
                        now=100,
                    )
                vector = cache.home()
                self.assertEqual(vector.shape, (12,))
                self.assertTrue(np.isfinite(vector).all())
                self.assertEqual(cache.diagnostics()["sensor_count"], size)
                self.assertEqual(cache.diagnostics()["token_encoder_calls"], size)
                if not size:
                    np.testing.assert_array_equal(vector, np.zeros(12, dtype=np.float32))
                else:
                    self.assertLessEqual(cache.diagnostics()["room_count"], 13)

    def test_permutation_invariance_with_all_supported_types_and_missing(self):
        model = SharedSetEncoder(seed=123)
        observations = [
            observation(i, area=f"room_{i % 3}", kind=KINDS[i % len(KINDS)],
                        valid=i % 8 != 0, quality=.5 if i % 3 == 0 else 1.0)
            for i in range(80)
        ]
        first, second = IncrementalNeuralHome(model), IncrementalNeuralHome(model)
        for item in observations:
            first.upsert(item, now=100)
        for item in reversed(observations):
            second.upsert(item, now=100)
        np.testing.assert_allclose(first.home(), second.home(), atol=1e-6, rtol=1e-6)
        for area in ("room_0", "room_1", "room_2"):
            np.testing.assert_allclose(first.room(area), second.room(area),
                                       atol=1e-6, rtol=1e-6)
        self.assertEqual(first.diagnostics()["room_count"], 3)

    def test_only_changed_room_and_token_are_reencoded(self):
        cache = IncrementalNeuralHome()
        cache.upsert(observation(1, area="kitchen"), now=100)
        cache.upsert(observation(2, area="bath"), now=100)
        cache.upsert(observation(3, area="hall"), now=100)
        initial = cache.home()
        bath_before = cache.room("bath")
        hall_before = cache.room("hall")
        diag = cache.diagnostics()
        self.assertEqual(diag["token_encoder_calls"], 3)
        self.assertEqual(diag["room_encoder_calls"], 3)
        self.assertEqual(diag["home_encoder_calls"], 1)
        cache.home()
        self.assertEqual(cache.diagnostics()["home_encoder_calls"], 1)
        cache.upsert(observation(1, area="kitchen", at=101), now=101)
        cache.home()
        updated = cache.diagnostics()
        self.assertEqual(updated["token_encoder_calls"], 4)
        self.assertEqual(updated["room_encoder_calls"], 4)
        self.assertEqual(updated["home_encoder_calls"], 2)
        # Same numerical reading on a later event needn't alter context:
        # just confirm the other two rooms did not require a new pass.
        np.testing.assert_array_equal(cache.room("bath"), bath_before)
        np.testing.assert_array_equal(cache.room("hall"), hall_before)

    def test_room_reassignment_and_remove_match_fresh_rebuild(self):
        cache = IncrementalNeuralHome()
        cache.upsert(observation(1, area="kitchen", at=10), now=10)
        cache.upsert(observation(2, area="hall", at=10), now=10)
        moved = observation(1, area="bath", at=11)
        cache.upsert(moved, now=11)
        expected = IncrementalNeuralHome(cache.encoder)
        expected.upsert(moved, now=11)
        expected.upsert(observation(2, area="hall", at=10), now=11)
        self.assertEqual(cache.diagnostics()["room_count"], 2)
        self.assertEqual(cache.index.pool("kitchen"), ())
        np.testing.assert_allclose(cache.home(), expected.home(), atol=1e-6, rtol=1e-6)
        cache.remove(moved.entity_id)
        expected.remove(moved.entity_id)
        self.assertEqual(cache.diagnostics()["room_count"], 1)
        np.testing.assert_allclose(cache.home(), expected.home(), atol=1e-6, rtol=1e-6)

    def test_incremental_result_matches_full_recomputation_after_many_updates(self):
        model = SharedSetEncoder(seed=91)
        cache = IncrementalNeuralHome(model)
        from random import Random
        random = Random(91)
        for stamp in range(1, 251):
            i = random.randrange(28)
            if stamp % 17 == 0:
                cache.remove(f"sensor.sensor_{i}")
            else:
                cache.upsert(observation(
                    i, area=f"room_{(i + stamp // 7) % 5}", at=stamp,
                    kind=KINDS[(i + stamp) % (len(KINDS) - 1) + 1],
                    valid=stamp % 11 != 0,
                ), now=stamp)
            if stamp % 25 == 0:
                full = IncrementalNeuralHome(model)
                for token in cache.index.tokens():
                    full.upsert(token, now=stamp)
                np.testing.assert_allclose(cache.home(), full.home(), atol=1e-6, rtol=1e-6)
                for room in {t.area_id for t in cache.index.tokens()}:
                    np.testing.assert_allclose(cache.room(room), full.room(room),
                                               atol=1e-6, rtol=1e-6)

    def test_expiry_invalidates_only_due_sensor_and_room(self):
        cache = IncrementalNeuralHome()
        a = observation(1, area="kitchen", at=10, ttl=5)
        b = observation(2, area="hall", at=10, ttl=100)
        cache.upsert(a, now=10)
        cache.upsert(b, now=10)
        before = cache.room("hall")
        cache.advance(14)
        self.assertEqual(cache.diagnostics()["token_encoder_calls"], 2)
        cache.advance(15)
        d = cache.diagnostics()
        self.assertEqual(d["available"], 1)
        self.assertEqual(d["token_encoder_calls"], 3)
        self.assertEqual(d["room_encoder_calls"], 3)
        np.testing.assert_array_equal(cache.room("hall"), before)
        self.assertEqual(cache.index.get(a.entity_id).reason, "stale")

    def test_late_older_event_does_not_change_neural_embedding(self):
        cache = IncrementalNeuralHome()
        cache.upsert(observation(1, at=10), now=10)
        cache.upsert(observation(1, at=15), now=15)
        before = cache.home()
        count = cache.diagnostics()["token_encoder_calls"]
        late = encode_sensor(
            "sensor.sensor_1",
            {"state": "99", "attributes": {
                "device_class": "temperature", "unit_of_measurement": "°C"
            }},
            at_ts=20, event_ts=12, received_ts=20,
        )
        self.assertFalse(cache.upsert(late, now=20))
        self.assertEqual(cache.diagnostics()["token_encoder_calls"], count)
        np.testing.assert_array_equal(cache.home(), before)

    def test_rejected_future_update_still_advances_expiry(self):
        cache = IncrementalNeuralHome()
        cache.upsert(observation(1, at=10, ttl=5), now=10)
        future = observation(1, at=22, ttl=5)
        with self.assertRaises(ValueError):
            cache.upsert(future, now=15)
        self.assertEqual(cache.diagnostics()["available"], 0)
        self.assertEqual(cache.index.get(future.entity_id).reason, "stale")

    def test_no_shared_state_between_cache_instances(self):
        model = SharedSetEncoder()
        one = IncrementalNeuralHome(model)
        two = IncrementalNeuralHome(model)
        one.upsert(observation(1), now=100)
        self.assertEqual(one.diagnostics()["sensor_count"], 1)
        self.assertEqual(two.diagnostics()["sensor_count"], 0)
        np.testing.assert_array_equal(two.home(), np.zeros(model.room_width))


if __name__ == "__main__":
    unittest.main()
