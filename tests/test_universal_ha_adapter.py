"""U02 real-HA-shaped data, semantic registry and causal replay contracts."""
import json
import sqlite3
import unittest

from universal_ha_adapter import (
    CausalObservationReplay,
    JournalObservation,
    RegistryEpoch,
    RegistryTimeline,
    SemanticRegistry,
    encode_ha_state_change,
    parse_ha_timestamp,
    journal_observation,
    read_feature_journal,
)
from universal_observation import UniversalObservationIndex


def registry(area="kitchen", revision="r1", *, calibration=None, entity_id="sensor.temp"):
    return SemanticRegistry.create(
        entities={entity_id: {
            "entity_id": entity_id, "device_id": "device-1",
            "original_device_class": "temperature" if entity_id == "sensor.temp" else None,
        }},
        devices=[{"id": "device-1", "area_id": area}],
        calibrations={entity_id: calibration} if calibration else {},
        revision=revision,
    )


def event(key, entity, event_time, received_time, state="20", attrs=None, quality=1.0):
    return JournalObservation(
        event_key=key, entity_id=entity, event_ts=float(event_time),
        received_ts=float(received_time), state=state,
        attributes=dict(attrs or {}), quality=quality,
    )


class SemanticRegistryTests(unittest.TestCase):
    def test_real_entity_and_device_registry_area_fallback(self):
        r = registry()
        token = r.encode(
            "sensor.temp", {"state": "68", "attributes": {"unit_of_measurement": "°F"}},
            at_ts=30, event_ts=20, received_ts=22,
        )
        self.assertEqual(token.area_id, "kitchen")
        self.assertEqual(token.kind, "temperature")
        self.assertAlmostEqual(token.value, 0.0)
        self.assertTrue(token.available)

    def test_entity_area_overrides_device_area(self):
        r = SemanticRegistry.create(
            entities=[{"entity_id": "binary_sensor.motion", "device_id": "d", "area_id": "bath"}],
            devices=[{"id": "d", "area_id": "hall"}],
            revision="override",
        )
        token = r.encode(
            "binary_sensor.motion", {"state": "on", "attributes": {}},
            at_ts=30, event_ts=30, received_ts=30,
        )
        self.assertEqual(token.area_id, "bath")
        self.assertEqual(token.value, 1.0)

    def test_unknown_or_ineligible_inputs_do_not_impersonate_sensor(self):
        r = registry()
        with self.assertRaises(ValueError):
            r.encode("switch.hvac", {"state": "on"}, at_ts=10, event_ts=10, received_ts=10)
        tok = r.encode("sensor.raw_radar", {"state": "38"}, at_ts=10, event_ts=10, received_ts=10)
        self.assertEqual(tok.reason, "unsupported_or_uncalibrated")
        self.assertFalse(tok.available)
        r2 = SemanticRegistry.create(
            calibrations={"sensor.raw_radar": {"center": 18, "scale": 10}},
            revision="calibration-v1",
        )
        calibrated = r2.encode("sensor.raw_radar", {"state": "38"},
                               at_ts=10, event_ts=10, received_ts=10)
        self.assertAlmostEqual(calibrated.value, 2.0)

    def test_raw_observation_metadata_not_overwritten_by_current_registry(self):
        r = SemanticRegistry.create(
            entities=[{"entity_id": "sensor.x", "original_device_class": "temperature"}]
        )
        token = r.encode(
            "sensor.x", {
                "state": "24",
                "attributes": {"device_class": "humidity", "unit_of_measurement": "%"},
            },
            at_ts=10, event_ts=10, received_ts=10,
        )
        self.assertEqual(token.kind, "humidity")
        self.assertAlmostEqual(token.value, 0.24)


class LiveHAEventAdapterTests(unittest.TestCase):
    def test_state_changed_iso8601_and_delete(self):
        at = parse_ha_timestamp("2026-11-01T10:00:01+01:00")
        changed = {
            "entity_id": "binary_sensor.motion",
            "new_state": {
                "entity_id": "binary_sensor.motion", "state": "on",
                "last_updated": "2026-11-01T10:00:00+01:00",
                "attributes": {"device_class": "motion"},
            },
        }
        eid, token = encode_ha_state_change(
            changed, SemanticRegistry.create(), at_ts=at, received_ts=at
        )
        self.assertEqual(eid, "binary_sensor.motion")
        self.assertTrue(token.available)
        self.assertEqual(token.event_ts, at - 1.0)
        self.assertEqual(encode_ha_state_change(
            {"entity_id": eid, "new_state": None},
            SemanticRegistry.create(), at_ts=at, received_ts=at,
        ), (eid, None))
        self.assertIsNone(encode_ha_state_change(
            {"entity_id": "light.kitchen", "new_state": {}},
            SemanticRegistry.create(), at_ts=at, received_ts=at,
        ))

    def test_event_refuses_missing_naive_or_mismatched_timestamps(self):
        for bad in ("2026-11-01T10:00:00", "", "broken"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_ha_timestamp(bad)
        at = parse_ha_timestamp("2026-11-01T10:00:01Z")
        with self.assertRaises(ValueError):
            encode_ha_state_change({
                "entity_id": "sensor.temp",
                "new_state": {"entity_id": "sensor.not_temp", "state": "20",
                              "last_updated": "2026-11-01T10:00:00Z"},
            }, registry(), at_ts=at, received_ts=at)
        with self.assertRaises(ValueError):
            encode_ha_state_change({
                "entity_id": "sensor.temp", "new_state": {"state": "20"},
            }, registry(), at_ts=at, received_ts=at)


class JournalContractTests(unittest.TestCase):
    def test_exact_feature_journal_row_and_tombstone(self):
        row = {
            "event_key": "sensor.temp:1", "contract_version": 1,
            "entity_id": "sensor.temp", "event_time": 10, "received_time": 12,
            "state": "20", "attributes_json": '{"unit_of_measurement":"°C"}',
            "source": "ha_ws", "quality": 0.75,
        }
        item = journal_observation(row)
        self.assertEqual(item.event_ts, 10)
        self.assertEqual(item.received_ts, 12)
        self.assertEqual(item.attributes["unit_of_measurement"], "°C")
        self.assertAlmostEqual(item.quality, .75)
        self.assertIsNone(journal_observation({**row, "state": None}).state)

    def test_invalid_malformed_or_unsupported_journal_is_explicit(self):
        base = {
            "event_key": "e1", "contract_version": 1,
            "entity_id": "sensor.temp", "event_time": 1, "received_time": 2,
            "state": "20", "attributes_json": "{}",
        }
        for bad in (
            {"contract_version": 2},
            {"entity_id": "light.kitchen"},
            {"attributes_json": "invalid"},
            {"attributes_json": "[]"},
            {"event_time": float("nan")},
            {"quality": -1},
            {"attributes_json": '{"x":"' + ("x" * 40000) + '"}'},
        ):
            with self.subTest(bad=list(bad)):
                with self.assertRaises(ValueError):
                    journal_observation({**base, **bad})

    def test_read_actual_sqlite_schema_without_mutating_data(self):
        db = sqlite3.connect(":memory:")
        db.execute("""
            CREATE TABLE feature_observation_events (
              event_key TEXT PRIMARY KEY, contract_version INTEGER NOT NULL,
              entity_id TEXT NOT NULL, event_time REAL NOT NULL, received_time REAL NOT NULL,
              state TEXT, attributes_json TEXT NOT NULL DEFAULT '{}',
              last_changed TEXT, last_updated TEXT,
              source TEXT NOT NULL, quality REAL NOT NULL,
              protected_until REAL NOT NULL DEFAULT 0
            )""")
        rows = [
            ("a", 1, "sensor.temp", 10, 10, "20", '{"unit_of_measurement":"°C"}', "test", 1.0),
            ("b", 1, "sensor.temp", 11, 15, "21", '{"unit_of_measurement":"°C"}', "test", 1.0),
            ("c", 1, "sensor.temp", 12, 12, "22", '{"unit_of_measurement":"°C"}', "test", 1.0),
            ("d", 1, "binary_sensor.motion", 11, 11, "on", "{}", "test", 1.0),
        ]
        db.executemany(
            "INSERT INTO feature_observation_events "
            "(event_key, contract_version, entity_id, event_time, received_time, "
            "state, attributes_json, source, quality) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        output = read_feature_journal(db, ["sensor.temp", "binary_sensor.motion"],
                                      at_ts=12)
        self.assertEqual([row.event_key for row in output], ["a", "d", "c"])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM feature_observation_events").fetchone()[0], 4)
        with self.assertRaises(ValueError):
            read_feature_journal(db, ["sensor.temp"], at_ts=20, max_records=2)
        with self.assertRaises(ValueError):
            read_feature_journal(db, ["light.kitchen"], at_ts=20)
        db.close()

    def test_bound_rows_does_not_discard_old_seed_silently(self):
        # No LIMIT 50 that would return only recent rows; fail if requested window
        # lacks a complete replay prefix under caller-supplied retention.
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE feature_observation_events("
                   "event_key TEXT,contract_version INT,entity_id TEXT,event_time REAL,"
                   "received_time REAL,state TEXT,attributes_json TEXT,source TEXT,quality REAL)")
        db.executemany("INSERT INTO feature_observation_events VALUES(?,?,?,?,?,?,?,?,?)", [
            (str(i), 1, "sensor.temp", i, i, str(i), '{"unit_of_measurement":"°C"}', "test", 1.0)
            for i in range(100)
        ])
        with self.assertRaisesRegex(ValueError, "cap exceeded"):
            read_feature_journal(db, ["sensor.temp"], at_ts=100, max_records=50)
        db.close()


class CausalReplayTests(unittest.TestCase):
    def setUp(self):
        self.r1 = registry("kitchen", "r1")
        self.r2 = registry("bath", "r2")
        self.timeline = RegistryTimeline([
            RegistryEpoch(0, 0, self.r1),
            RegistryEpoch(30, 40, self.r2),
        ])

    def test_late_older_packet_cannot_replace_newer_event(self):
        rows = [
            event("a", "sensor.temp", 10, 10, state="20", attrs={"unit_of_measurement": "°C"}),
            event("newer", "sensor.temp", 12, 12, state="22", attrs={"unit_of_measurement": "°C"}),
            event("late", "sensor.temp", 11, 20, state="90", attrs={"unit_of_measurement": "°C"}),
        ]
        replay = CausalObservationReplay(rows, registry_timeline=self.timeline)
        at12 = replay.snapshot(12)
        self.assertEqual(at12["tokens"][0].event_ts, 12)
        self.assertEqual(at12["sensor_count"], 1)
        at20 = replay.snapshot(20)
        self.assertEqual(at20["tokens"][0].event_ts, 12)
        self.assertAlmostEqual(at20["tokens"][0].value, 0.1)
        # Rewind and forward must reproduce the same token.
        replay.snapshot(10)
        self.assertEqual(replay.snapshot(20)["tokens"], at20["tokens"])

    def test_received_time_gate_and_registry_metadata_gate(self):
        rows = [
            event("a", "sensor.temp", 10, 10, state="20", attrs={"unit_of_measurement": "°C"}),
            event("late", "sensor.temp", 20, 50, state="24", attrs={"unit_of_measurement": "°C"}),
        ]
        replay = CausalObservationReplay(rows, registry_timeline=self.timeline)
        self.assertEqual(replay.snapshot(19)["tokens"][0].area_id, "kitchen")
        self.assertEqual(replay.snapshot(35)["tokens"][0].area_id, "kitchen")
        self.assertEqual(replay.snapshot(41)["tokens"][0].area_id, "bath")
        at49 = replay.snapshot(49)
        self.assertEqual(at49["tokens"][0].event_ts, 10)
        self.assertEqual(at49["registry_revision"], "r2")
        at50 = replay.snapshot(50)
        self.assertEqual(at50["tokens"][0].event_ts, 20)
        self.assertEqual(at50["tokens"][0].area_id, "bath")

    def test_registry_received_after_effective_time_does_not_leak(self):
        replay = CausalObservationReplay([
            event("a", "sensor.temp", 10, 10, attrs={"unit_of_measurement": "°C"}),
        ], registry_timeline=self.timeline)
        self.assertEqual(replay.snapshot(31)["registry_revision"], "r1")
        self.assertEqual(replay.snapshot(40)["registry_revision"], "r2")

    def test_no_historical_topology_snapshot_does_not_use_current_area(self):
        replay = CausalObservationReplay([
            event("a", "sensor.temp", 10, 10, state="20", attrs={"unit_of_measurement": "°C"}),
        ], registry_timeline=RegistryTimeline([RegistryEpoch(100, 100, self.r2)]))
        self.assertEqual(replay.snapshot(10)["tokens"][0].area_id, "__unassigned__")
        self.assertEqual(replay.snapshot(100)["tokens"][0].area_id, "bath")

    def test_tombstone_and_rewind(self):
        rows = [
            event("a", "sensor.temp", 10, 10, attrs={"unit_of_measurement": "°C"}),
            event("delete", "sensor.temp", 20, 20, state=None),
        ]
        replay = CausalObservationReplay(rows, registry_timeline=self.timeline)
        self.assertEqual(replay.snapshot(10)["sensor_count"], 1)
        self.assertEqual(replay.snapshot(20)["sensor_count"], 0)
        self.assertEqual(replay.snapshot(10)["sensor_count"], 1)
        self.assertEqual(replay.snapshot(25)["sensor_count"], 0)

    def test_later_events_of_same_timestamp_use_receipt_tie_break(self):
        rows = [
            event("a", "sensor.temp", 10, 10, state="20", attrs={"unit_of_measurement": "°C"}),
            event("b", "sensor.temp", 10, 13, state="21", attrs={"unit_of_measurement": "°C"}),
        ]
        replay = CausalObservationReplay(rows, registry_timeline=self.timeline)
        self.assertEqual(replay.snapshot(12)["tokens"][0].received_ts, 10)
        self.assertEqual(replay.snapshot(14)["tokens"][0].received_ts, 13)

    def test_stale_deadline_updates_without_new_event(self):
        rows = [event("a", "binary_sensor.motion", 10, 10, "on")]
        replay = CausalObservationReplay(rows, max_age_seconds=5)
        self.assertTrue(replay.snapshot(14)["tokens"][0].available)
        self.assertEqual(replay.snapshot(15)["tokens"][0].reason, "stale")
        self.assertEqual(replay.snapshot(15)["pools"][0]["available"], 0)

    def test_duplicate_rows_idempotent_and_conflicts_rejected(self):
        first = event("dup", "binary_sensor.motion", 10, 10, "on")
        replay = CausalObservationReplay([first, first])
        self.assertEqual(replay.snapshot(12)["indexed_events"], 1)
        with self.assertRaises(ValueError):
            CausalObservationReplay([first, event("dup", "binary_sensor.motion", 11, 11, "off")])

    def test_unordered_event_sequence_replays_equivalently(self):
        rows = [
            event(str(i), f"binary_sensor.motion_{i%20}", i, i + (i % 3), "on" if i % 2 else "off")
            for i in range(300)
        ]
        a = CausalObservationReplay(rows)
        b = CausalObservationReplay(reversed(rows))
        for at in (10, 100, 250, 500):
            self.assertEqual(a.snapshot(at)["tokens"], b.snapshot(at)["tokens"])
            self.assertEqual(a.snapshot(at)["pools"], b.snapshot(at)["pools"])


if __name__ == "__main__":
    unittest.main()
