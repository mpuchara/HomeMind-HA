"""Exact causal feature reuse across candidate schemas, with owned outputs."""
import copy
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import support
import observation_contract as observation
import shared_observation_features as shared
from context import TemporalHistory
from settings import OPTIONS
from test_observation_contract import sensor_state, add_live


class SharedEntityFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.agent = support.agent()
        self.temporal = TemporalHistory()
        self.states = {}
        fixtures = [('light.kitchen', 'off', None, None),
                    ('sensor.temp', '20', '°C', 'temperature'),
                    ('sensor.lux', '12', 'lx', 'illuminance'),
                    ('sensor.radar', '125', 'cm', 'distance'),
                    ('binary_sensor.motion', 'on', None, 'occupancy'),
                    ('sensor.mode', 'eco', None, None),
                    ('sensor.missing', 'unavailable', None, None)]
        for eid, value, unit, device_class in fixtures:
            for ts in (1_700_000_080., 1_700_000_095., 1_700_000_099.):
                st = sensor_state(eid, value, ts, unit=unit, device_class=device_class)
                add_live(self.temporal, st, ts, ts + .01)
                self.states[eid] = st
        self.entities = [row[0] for row in fixtures[1:]]

    def schema(self, entities=None, contract=4, dims=128):
        schema = observation.FeatureSchemaV12(dims, self.entities if entities is None else entities)
        schema.feature_contract_version = contract
        return schema

    def build(self, schema=None, **kwargs):
        params = dict(schema=schema or self.schema(), state_map=self.states,
                      temporal=self.temporal, at_ts=1_700_000_100., agent=self.agent)
        params.update(kwargs)
        return observation.build_observation_features(**params)

    def frame(self):
        return shared.shared_entity_observations(self.agent, self.states, self.temporal, 1_700_000_100.)

    def test_all_contracts_order_dimensions_and_missing_values_match_uncached(self):
        schemas = [self.schema(entities, contract, dims)
                   for contract in (1, 2, 3, 4)
                   for entities in (self.entities, list(reversed(self.entities)))
                   for dims in (32, 128)]
        expected = [self.build(schema) for schema in schemas]
        with self.frame():
            actual = [self.build(schema) for schema in schemas]
        self.assertEqual(actual, expected)

    def test_overlapping_schemas_compute_each_entity_once(self):
        schemas = [self.schema(['sensor.temp', 'sensor.lux']),
                   self.schema(['sensor.lux', 'sensor.radar'])]
        with patch.object(observation, '_entity_observation', wraps=observation._entity_observation) as compute:
            with self.frame():
                for schema in schemas:
                    self.build(schema)
                self.assertEqual(compute.call_count, 3)
            self.build(schemas[0])
            self.assertEqual(compute.call_count, 5)

    def test_vectors_labels_and_nested_metadata_are_owned(self):
        expected = self.build()
        with self.frame():
            first = self.build()
            first[0].clear()
            first[1].clear()
            for meta in first[2]['entity_observations'].values():
                meta['lag_seconds'].append(999)
                meta['lag_coverage'].clear()
                if meta['photometric'] is not None:
                    meta['photometric']['source'] = 'corrupted'
            self.assertEqual(self.build(), expected)

    def test_excluded_entity_and_semantic_contract_do_not_collide(self):
        calls = [dict(schema=self.schema(contract=1)), dict(schema=self.schema(contract=4)),
                 dict(excluded_entities={'sensor.temp'}), dict(excluded_entities={'sensor.lux'})]
        expected = [self.build(**kw) for kw in calls]
        with self.frame():
            self.assertEqual([self.build(**kw) for kw in calls], expected)

    def test_lag_option_changes_are_fresh(self):
        with patch.dict(OPTIONS, {'fast_series_lags_seconds': '1,3,10'}):
            first = self.build()
            with self.frame():
                self.assertEqual(self.build(), first)
                # Slow agents use the regular temporal options.
                slow = self.agent | {'target_entity': 'climate.room', 'target_property': 'temperature'}
                with shared.shared_entity_observations(slow, self.states, self.temporal, 1_700_000_100.):
                    with patch.dict(OPTIONS, {'temporal_short_seconds': 2, 'temporal_long_seconds': 4}):
                        a = self.build(agent=slow)
                    with patch.dict(OPTIONS, {'temporal_short_seconds': 6, 'temporal_long_seconds': 8}):
                        b = self.build(agent=slow)
                        self.assertNotEqual(a[2]['entity_observations'], b[2]['entity_observations'])

    def test_agent_states_temporal_or_timestamp_identity_mismatch_bypasses(self):
        with self.frame(), patch.object(observation, '_entity_observation', wraps=observation._entity_observation) as compute:
            self.build()
            for kwargs in ({'agent': self.agent | {'target_property': 'temperature'}}, {'state_map': dict(self.states)},
                           {'temporal': copy.deepcopy(self.temporal)}, {'at_ts': 1_700_000_101.}):
                before = compute.call_count
                self.build(**kwargs)
                self.assertEqual(compute.call_count - before, len(self.entities))

    def test_future_received_state_is_not_used(self):
        prior_value = self.build()[0].get(5, 0.)
        future = sensor_state('sensor.temp', '90', 1_700_000_099.5, unit='°C', device_class='temperature')
        add_live(self.temporal, future, 1_700_000_099.5, 1_700_000_101.)
        expected = self.build()
        self.assertEqual(expected[0].get(5, 0.), prior_value)
        with self.frame():
            self.assertEqual(self.build(), expected)
            self.assertEqual(self.build(), expected)

    def test_new_observation_refreshes_after_state_and_history_change(self):
        with self.frame():
            before = self.build()
        newer = sensor_state('sensor.temp', '30', 1_700_000_099.8, unit='°C', device_class='temperature')
        add_live(self.temporal, newer, 1_700_000_099.8, 1_700_000_099.9)
        self.states['sensor.temp'] = newer
        expected = self.build()
        with self.frame():
            self.assertEqual(self.build(), expected)
        self.assertNotEqual(before, expected)

    def test_nested_scope_exception_restores_outer_and_releases_after_exit(self):
        with self.frame():
            outer = shared.entity_observation_cache(self.agent, self.states, self.temporal, 1_700_000_100.)
            self.build()
            with self.assertRaises(ValueError):
                with self.frame():
                    self.assertIsNot(shared.entity_observation_cache(self.agent, self.states, self.temporal, 1_700_000_100.), outer)
                    raise ValueError('nested')
            self.assertIs(shared.entity_observation_cache(self.agent, self.states, self.temporal, 1_700_000_100.), outer)
        self.assertIsNone(shared.entity_observation_cache(self.agent, self.states, self.temporal, 1_700_000_100.))

    def test_cache_is_thread_local(self):
        results = []
        with self.frame():
            self.build()
            worker = threading.Thread(target=lambda: results.append(shared.entity_observation_cache(
                self.agent, self.states, self.temporal, 1_700_000_100.)))
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [None])

    def test_capacity_falls_back_without_losing_any_entity(self):
        expected = self.build()
        with patch.object(observation, 'MAX_ENTRIES', 2), self.frame():
            self.assertEqual(self.build(), expected)
            self.assertEqual(self.build(), expected)
            cache = shared.entity_observation_cache(self.agent, self.states, self.temporal, 1_700_000_100.)
            self.assertEqual(len(cache), 2)

    def test_light_on_photometric_baseline_matches_each_contract(self):
        target = sensor_state('light.kitchen', 'on', 1_700_000_099.5)
        add_live(self.temporal, target, 1_700_000_099.5, 1_700_000_099.6)
        self.states['light.kitchen'] = target
        schemas = [self.schema(['sensor.lux'], contract) for contract in (1, 2, 3, 4)]
        expected = [self.build(schema) for schema in schemas]
        with self.frame():
            for _ in range(2):
                self.assertEqual([self.build(schema) for schema in schemas], expected)

    def test_failed_descriptor_is_not_published_to_cache(self):
        expected = self.build()
        with self.frame():
            with patch.object(observation, '_entity_observation', side_effect=ValueError('read failed')):
                with self.assertRaises(ValueError):
                    self.build()
            self.assertEqual(shared.entity_observation_cache(self.agent, self.states, self.temporal, 1_700_000_100.), {})
            self.assertEqual(self.build(), expected)

    def test_in_place_target_change_invalidates_photometric_descriptor(self):
        target = sensor_state('light.other', 'on', 1_700_000_099.5)
        add_live(self.temporal, target, 1_700_000_099.5, 1_700_000_099.6)
        self.states['light.other'] = target
        with self.frame():
            self.build()
            self.agent['target_entity'] = 'light.other'
            cached = self.build()
        self.assertEqual(cached, self.build())

    def test_actual_candidates_keep_full_checksum_features_predictions_and_learning(self):
        from tools.benchmark_observation_census import worker
        # The production wrapper exercises four candidates, an independent target
        # label, post-label learning, durable/RAM pool counts and fresh full SHA.
        row = worker(Path(__file__).resolve().parents[1], 2)
        self.assertEqual(set(row['census_sql_per_frame']), {1})
        self.assertEqual(list(row['labels'].values()), [1] * 4)
        self.assertGreaterEqual(row['checksum_checks'], 4 * 6)


if __name__ == '__main__':
    unittest.main()
