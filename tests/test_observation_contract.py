import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from support import agent, state
from context import TemporalHistory
from observation_contract import (
    FeatureJournal,
    FeatureSchemaV12,
    HOME_FEATURE_NAMES,
    HOME_TAIL,
    ObservationSQLiteTemporalTracker,
    SCHEMA_VERSION,
    POLICY_VERSION,
    build_observation_features,
    observation_value,
    teaching_signature,
    policy_features,
    register_live_sample,
    _migrate_models,
)
from storage import Store


def stamp(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def sensor_state(eid, value, ts, *, unit=None, device_class=None, last_changed=None):
    attrs = {}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    if device_class is not None:
        attrs["device_class"] = device_class
    st = state(eid, value, **attrs)
    st["last_updated"] = stamp(ts)
    st["last_changed"] = stamp(ts if last_changed is None else last_changed)
    return st


def add_live(history, st, ts, received=None, source="ha_state_changed"):
    history.add(st["entity_id"], float(ts), dict(st))
    register_live_sample(history, st["entity_id"], st, float(ts),
                         float(received if received is not None else ts), source)


def label_index(labels, suffix):
    for idx, names in labels.items():
        if names and names[0].endswith(suffix):
            return idx
    raise AssertionError(f"missing label {suffix}")


class ObservationFeatureTests(unittest.TestCase):
    def setUp(self):
        self.a = agent()
        self.schema = FeatureSchemaV12(128, ["sensor.test"])

    def test_unknown_is_not_numeric_zero(self):
        h_unknown = TemporalHistory()
        unknown = sensor_state("sensor.test", "unknown", 100.0)
        add_live(h_unknown, unknown, 100.0, 100.01)
        vu, labels, _ = build_observation_features(
            self.schema, {"sensor.test": unknown}, h_unknown, 100.02, self.a)

        h_zero = TemporalHistory()
        zero = sensor_state("sensor.test", "0", 100.0, unit="%", last_changed=50.0)
        add_live(h_zero, zero, 100.0, 100.01)
        vz, _, _ = build_observation_features(
            self.schema, {"sensor.test": zero}, h_zero, 100.02, self.a)

        value_idx = label_index(labels, ":value")
        valid_idx = label_index(labels, ":valid")
        self.assertEqual(vu.get(value_idx, 0.0), 0.0)
        self.assertEqual(vz.get(value_idx, 0.0), 0.0)
        self.assertEqual(vu.get(valid_idx, 0.0), 0.0)
        self.assertEqual(vz.get(valid_idx, 0.0), 1.0)
        self.assertNotEqual(vu, vz)

    def test_celsius_and_fahrenheit_are_equivalent(self):
        hc, hf = TemporalHistory(), TemporalHistory()
        c = sensor_state("sensor.test", "20", 100.0, unit="°C",
                         device_class="temperature", last_changed=50.0)
        f = sensor_state("sensor.test", "68", 100.0, unit="°F",
                         device_class="temperature", last_changed=50.0)
        add_live(hc, c, 100.0, 100.01)
        add_live(hf, f, 100.0, 100.01)
        vc, labels, mc = build_observation_features(
            self.schema, {"sensor.test": c}, hc, 100.02, self.a)
        vf, _, mf = build_observation_features(
            self.schema, {"sensor.test": f}, hf, 100.02, self.a)

        for suffix in (":value", ":valid", ":trend_1", ":trend_2", ":trend_3"):
            idx = label_index(labels, suffix)
            self.assertAlmostEqual(vc.get(idx, 0.0), vf.get(idx, 0.0), places=12)
        self.assertEqual(mc["entity_observations"]["sensor.test"]["canonical_unit"], "°c")
        self.assertEqual(mf["entity_observations"]["sensor.test"]["canonical_unit"], "°c")

    def test_unavailable_gap_does_not_fake_value_edge(self):
        history = TemporalHistory()
        first = sensor_state("sensor.test", "0", 100.0, unit="%", last_changed=90.0)
        missing = sensor_state("sensor.test", "unavailable", 101.0, unit="%", last_changed=101.0)
        recovered = sensor_state("sensor.test", "0", 102.0, unit="%", last_changed=102.0)
        for st, ts in ((first, 100.0), (missing, 101.0), (recovered, 102.0)):
            add_live(history, st, ts, ts + .01)

        vector, labels, meta = build_observation_features(
            self.schema, {"sensor.test": recovered}, history, 103.0, self.a)
        for suffix in (":trend_1", ":trend_2", ":trend_3"):
            self.assertAlmostEqual(vector.get(label_index(labels, suffix), 0.0), 0.0, places=12)
        self.assertGreaterEqual(
            meta["entity_observations"]["sensor.test"]["time_since_edge_seconds"], 3.0)

    def test_arbitrary_categories_use_nonordinal_bits(self):
        a = observation_value(sensor_state("sensor.test", "eco", 1.0))
        b = observation_value(sensor_state("sensor.test", "boost", 1.0))
        self.assertEqual(a["value"], 0.0)
        self.assertEqual(b["value"], 0.0)
        self.assertEqual(a["kind"], "category")
        self.assertEqual(b["kind"], "category")
        self.assertNotEqual(a["category"], b["category"])

    def test_home_known_is_an_explicit_tail_feature(self):
        history = TemporalHistory()
        st = sensor_state("sensor.test", "0", 100.0, unit="%", last_changed=50.0)
        add_live(history, st, 100.0, 100.01)

        class Home:
            def __init__(self, known):
                self.known = known
            def forecast(self, target, ts):
                values = {name: 0.0 for name in HOME_FEATURE_NAMES}
                values["known"] = self.known
                values["area_id"] = "kitchen"
                return values

        fake = SimpleNamespace(
            context_engine=None, excluded_context_entities=set(), schema=self.schema,
            agent=self.a, dims=128,
        )
        history.home_context = Home(False)
        vf, labels, _ = policy_features(fake, {"sensor.test": st}, history, 100.02)
        known_idx = 128 - HOME_TAIL + HOME_FEATURE_NAMES.index("known")
        self.assertEqual(labels[known_idx], ["home:known"])
        self.assertEqual(vf.get(known_idx), 0.0)

        history.home_context = Home(True)
        vt, _, _ = policy_features(fake, {"sensor.test": st}, history, 100.02)
        self.assertEqual(vt.get(known_idx), 1.0)


class ObservationReplayParityTests(unittest.TestCase):
    class EmptyContext:
        options = {}
        def area_for(self, eid):
            return None
        def relevant_entities(self):
            return []
        def sensor_probability(self, eid, state):
            return None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        self.journal = FeatureJournal(self.store)
        self.schema = FeatureSchemaV12(128, ["sensor.fast"])
        self.a = agent()
        self.context = self.EmptyContext()

    def tearDown(self):
        self.temp.cleanup()

    def _record_both(self, history, value, event_time, received_time):
        st = sensor_state("sensor.fast", value, event_time, unit="%",
                          last_changed=event_time)
        add_live(history, st, event_time, received_time)
        self.journal.record(
            "sensor.fast", st, event_time=event_time, received_time=received_time,
            source="ha_state_changed",
        )
        return st

    def test_live_and_replay_match_at_millisecond_times_without_future_data(self):
        live = TemporalHistory(maxlen=96)
        current = None
        for value, event_time, received_time in (
            (0, 990.001, 990.011),
            (10, 999.251, 999.261),
            (20, 1000.751, 1000.761),
            (30, 1001.125, 1001.135),
        ):
            current = self._record_both(live, value, event_time, received_time)

        at = 1001.500
        live_vector, live_labels, live_meta = build_observation_features(
            self.schema, {"sensor.fast": current}, live, at, self.a)

        tracker = ObservationSQLiteTemporalTracker(
            self.store, {"sensor.fast"}, self.context, 980.0, at)
        try:
            tracker.advance(at)
            replay_vector, replay_labels, replay_meta = build_observation_features(
                self.schema, tracker.state_map, tracker.history, at, self.a)
        finally:
            tracker.close()

        self.assertEqual(live_labels, replay_labels)
        self.assertEqual(set(live_vector), set(replay_vector))
        for idx in live_vector:
            self.assertAlmostEqual(live_vector[idx], replay_vector[idx], places=12)
        self.assertTrue(live_meta["reconstruction_complete"])
        self.assertTrue(replay_meta["reconstruction_complete"])

        delayed = sensor_state("sensor.fast", "99", 1001.300, unit="%",
                               last_changed=1001.300)
        self.journal.record(
            "sensor.fast", delayed, event_time=1001.300, received_time=1002.500,
            source="ha_state_changed",
        )
        tracker = ObservationSQLiteTemporalTracker(
            self.store, {"sensor.fast"}, self.context, 980.0, at)
        try:
            tracker.advance(at)
            value = observation_value(tracker.state_map["sensor.fast"])
        finally:
            tracker.close()
        self.assertAlmostEqual(value["physical_value"], 30.0)

    def test_teach_signature_matches_live_and_replay_and_carries_versions(self):
        live = TemporalHistory(maxlen=96)
        current = None
        for value, event_time, received_time in (
            (0, 990.001, 990.011),
            (10, 998.251, 998.261),
            (20, 1000.251, 1000.261),
            (30, 1001.125, 1001.135),
        ):
            current = self._record_both(live, value, event_time, received_time)

        at = 1001.500

        class Policy:
            VERSION = POLICY_VERSION

            def __init__(self, schema, agent_config):
                self.schema = schema
                self.agent = agent_config
                self.dims = schema.dims

            def features(self, states, temporal, at_ts=None):
                vector, labels, meta = build_observation_features(
                    self.schema, states, temporal, at_ts, self.agent
                )
                meta = dict(meta)
                meta["home_known"] = False
                return vector, labels, meta

        policy = Policy(self.schema, self.a)
        live_signature = teaching_signature(
            policy, {"sensor.fast": current}, live, at
        )
        self.assertIsNotNone(live_signature)
        self.assertEqual(
            live_signature["meta:feature_schema_version"], float(SCHEMA_VERSION)
        )
        self.assertEqual(
            live_signature["meta:policy_version"], float(POLICY_VERSION)
        )
        self.assertEqual(live_signature["meta:signature_contract"], 3.0)
        self.assertEqual(live_signature["meta:home_known"], 0.0)

        tracker = ObservationSQLiteTemporalTracker(
            self.store, {"sensor.fast"}, self.context, 980.0, at
        )
        try:
            tracker.advance(at)
            replay_signature = teaching_signature(
                policy, tracker.state_map, tracker.history, at
            )
        finally:
            tracker.close()

        self.assertEqual(replay_signature, live_signature)
    def test_buffer_is_bounded_per_entity(self):
        tiny = FeatureJournal(
            self.store, clock=lambda: 1000.0, retention_hours=24,
            max_events_per_entity=3, global_event_limit=20)
        for i in range(8):
            st = sensor_state("sensor.fast", i, 900 + i, unit="%")
            tiny.record("sensor.fast", st, event_time=900 + i,
                        received_time=900 + i + .01, source="ha_state_changed",
                        event_key=f"e{i}")
        tiny.prune(entity_id="sensor.fast")
        with self.store.conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM feature_observation_events WHERE entity_id='sensor.fast'"
            ).fetchone()[0]
        self.assertLessEqual(count, 3)

    def test_window_protects_recent_context_but_retention_is_bounded(self):
        clock = {"now": 1000.0}
        tiny = FeatureJournal(
            self.store, clock=lambda: clock["now"], retention_hours=.001,
            max_events_per_entity=2, window_retention_days=.01,
            max_windows_per_agent=2, global_event_limit=20)
        for i in range(3):
            st = sensor_state("sensor.fast", i, 999.0 + i * .1, unit="%")
            tiny.record("sensor.fast", st, event_time=999.0 + i * .1,
                        received_time=999.0 + i * .1, source="ha_state_changed",
                        event_key=f"w{i}")
        tiny.open_window("w", "agent", ["sensor.fast"], 999.1, "decision",
                         before=.2, after=.2)
        clock["now"] = 1010.0
        tiny.prune(entity_id="sensor.fast")
        with self.store.conn() as c:
            protected = c.execute(
                "SELECT COUNT(*) FROM feature_observation_events WHERE protected_until>?",
                (clock["now"],)
            ).fetchone()[0]
        self.assertGreater(protected, 0)


class ObservationMigrationTests(unittest.TestCase):
    def test_schema_version_migration_preserves_old_model_bytes(self):
        temp = tempfile.TemporaryDirectory()
        try:
            store = Store(Path(temp.name) / "test.db")
            created = store.create_agent(agent(name="Old") | {"auto_created": True})
            old = {
                "version": 10,
                "schema": {"version": 11, "dims": 128, "entities": ["sensor.old"]},
                "sentinel": {"weights": [1, 2, 3]},
            }
            store.save_model(created["id"], old)
            before = json.dumps(store.get_model(created["id"]), sort_keys=True)
            core = SimpleNamespace(STORE=store, ENGINE=SimpleNamespace(models={"x": object()}))

            changed = _migrate_models(core)

            self.assertIn(created["id"], changed)
            self.assertEqual(json.dumps(store.get_model(created["id"]), sort_keys=True), before)
            fresh = store.get_agent_config(created["id"])
            self.assertEqual(fresh["training_state"], "needs_retrain")
            self.assertEqual(fresh["mode"], "paused")
            self.assertEqual(core.ENGINE.models, {})
            self.assertEqual(FeatureSchemaV12.VERSION, SCHEMA_VERSION)
            self.assertEqual(POLICY_VERSION, 11)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
