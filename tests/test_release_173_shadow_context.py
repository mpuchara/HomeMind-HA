"""Per-observation sharing preserves causal inputs, independent policies and full SHA."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import support
from observation_contract import _last_edge_time
from shared_inference_context import shared_inference_temporal
from context import TemporalHistory
from storage import Store
import context_tournament_policy_candidate as candidates
import context_tournament_promotion as promotion
from policy import MultiHorizonPolicy
with patch.dict(os.environ):
    from tools.benchmark_shadow_context import Fixture, STAMP, state, training_contract, run_mode


class ShadowContext173Tests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion))
        self.stack.enter_context(patch.object(promotion, '_migrate_schema', promotion._migrate_schema))
        self.stack.enter_context(training_contract())
        self.stack.enter_context(patch('time.time', return_value=STAMP))
        self.f = Fixture(Store(Path(directory)/'test.db'))
        self.f.observe()

    def test_production_observer_shares_one_forecast_and_checks_all_four_payloads(self):
        with patch.object(self.f.home, 'forecast', wraps=self.f.home.forecast) as home, \
             patch.object(candidates, '_verify_candidate_checksum', wraps=candidates._verify_candidate_checksum) as sha:
            result = self.f.observe()
        self.assertEqual(len(result['predictions']), 4)
        self.assertEqual(home.call_count, 1)
        self.assertEqual(sha.call_count, 4)

    def test_every_feature_label_and_metadata_equals_previous_observer(self):
        previous = run_mode('previous', passes=2)
        current = run_mode('current', passes=2)
        self.assertEqual(previous['every_features_labels_meta_sha256'], current['every_features_labels_meta_sha256'])

    def test_new_observation_at_same_timestamp_uses_fresh_forecast(self):
        vectors = []
        features = MultiHorizonPolicy.features
        def record(policy, *args, **kwargs):
            result = features(policy, *args, **kwargs)
            vectors.append(result[2]['home_forecast']['occupancy_now'])
            return result
        with patch.object(self.f.home, 'forecast', side_effect=[{'known': True, 'occupancy_now': .1},
                                                             {'known': True, 'occupancy_now': .9}]) as home, \
             patch.object(MultiHorizonPolicy, 'features', record):
            self.f.observe()
            self.f.observe()
        self.assertEqual(home.call_count, 2)
        self.assertEqual(vectors, [.1]*4 + [.9]*4)

    def test_observation_cleanup_after_failure_does_not_reuse_temporal_view(self):
        original = candidates.shared_inference_temporal
        views = []
        def capture(*args, **kwargs):
            view = original(*args, **kwargs)
            views.append(view)
            return view
        with patch.object(candidates, 'shared_inference_temporal', side_effect=capture), \
             patch.object(self.f.service, '_shadow_predict_index', side_effect=RuntimeError('probe')):
            with self.assertRaisesRegex(RuntimeError, 'probe'):
                self.f.observe()
        with patch.object(candidates, 'shared_inference_temporal', side_effect=capture):
            self.f.observe()
        self.assertEqual(len(views), 2)
        self.assertIsNot(views[0], views[1])

    def test_corrupted_warm_payload_still_requires_checksum_repair(self):
        key = (self.f.agent['id'], self.f.challengers[0])
        model = self.f.service._shadow_models[key]
        model['candidate_policy']['model_revision'] = 'tampered-without-new-checksum'
        with patch.object(candidates, '_verify_candidate_checksum', wraps=candidates._verify_candidate_checksum) as sha:
            self.f.observe()
        self.assertEqual(sha.call_count, 4)
        self.assertNotEqual(model['candidate_policy']['model_revision'], 'tampered-without-new-checksum')
        self.assertTrue(candidates.verify_model_checksum(model['candidate_policy']))

    def test_candidate_specific_sensor_features_remain_distinct(self):
        schemas = []
        features = MultiHorizonPolicy.features
        def record(policy, *args, **kwargs):
            schemas.append(tuple(policy.schema.entities))
            return features(policy, *args, **kwargs)
        with patch.object(MultiHorizonPolicy, 'features', record):
            self.f.observe()
        self.assertEqual(len(set(schemas)), 4)
        for schema, challenger in zip(schemas, self.f.challengers):
            self.assertIn(challenger, schema)

    def test_no_home_provider_does_not_add_empty_forecast_metadata(self):
        self.f.engine.context = None
        for model in self.f.service._shadow_models.values():
            model.pop('candidate_policy', None)
        captured = []
        features = MultiHorizonPolicy.features
        def record(policy, *args, **kwargs):
            result = features(policy, *args, **kwargs)
            captured.append(result[2])
            return result
        with patch.object(MultiHorizonPolicy, 'features', record):
            self.f.observe()
        self.assertEqual(len(captured), 4)
        self.assertTrue(all('home_forecast' not in meta for meta in captured))


class ObservationEdge173Tests(unittest.TestCase):
    def view(self, rows):
        temporal = TemporalHistory()
        for ts, value in rows:
            temporal.add('sensor.x', ts, state('sensor.x', value, ts))
        return temporal, shared_inference_temporal(temporal, STAMP)

    def test_edge_cache_preserves_missing_invalid_future_and_out_of_order_samples(self):
        cases = [[], [(STAMP-5,'unknown')], [(STAMP-4,'off'),(STAMP-3,'on'),(STAMP+1,'off')],
                 [(STAMP-3,1),(STAMP-2,1)], [(STAMP+1,9),(STAMP-5,1),(STAMP-2,2)],
                 [(STAMP-6,1),(STAMP-4,'unavailable'),(STAMP-2,2)]]
        current = state('sensor.x', 2, STAMP-7)
        for rows in cases:
            with self.subTest(rows=rows):
                temporal, shared = self.view(rows)
                expected = _last_edge_time(temporal, 'sensor.x', STAMP, current)
                self.assertEqual(_last_edge_time(shared,'sensor.x',STAMP,current), expected)
                self.assertEqual(_last_edge_time(shared,'sensor.x',STAMP,current), expected)
                self.assertEqual(shared.edge_misses, 1)
                self.assertEqual(shared.edge_hits, 1)

    def test_different_query_timestamp_bypasses_cache(self):
        temporal, shared = self.view([(STAMP-3,1),(STAMP-1,2)])
        for ts in (STAMP-2, STAMP-1, STAMP+1):
            self.assertEqual(_last_edge_time(shared,'sensor.x',ts,None),_last_edge_time(temporal,'sensor.x',ts,None))
        self.assertEqual(shared.edge_hits, 0)
        self.assertEqual(shared.edge_misses, 0)

    def test_fallback_change_timestamp_invalidates_within_observation(self):
        temporal, shared = self.view([])
        old = state('sensor.x',2,STAMP-5)
        new = state('sensor.x',2,STAMP-1)
        self.assertEqual(_last_edge_time(shared,'sensor.x',STAMP,old),STAMP-5)
        self.assertEqual(_last_edge_time(shared,'sensor.x',STAMP,new),STAMP-1)
        self.assertEqual(shared.edge_misses, 2)

    def test_new_observation_sees_new_history_edge_at_same_timestamp(self):
        temporal, shared = self.view([(STAMP-4,1),(STAMP-3,1)])
        self.assertEqual(_last_edge_time(shared,'sensor.x',STAMP,None),STAMP-4)
        temporal.add('sensor.x',STAMP-1,state('sensor.x',2,STAMP-1))
        later = shared_inference_temporal(temporal,STAMP)
        self.assertEqual(_last_edge_time(later,'sensor.x',STAMP,None),STAMP-1)

    def test_none_results_are_cached(self):
        _, shared = self.view([])
        compute = Mock(return_value=None)
        self.assertIsNone(shared.observation_edge('sensor.x',STAMP,None,compute))
        self.assertIsNone(shared.observation_edge('sensor.x',STAMP,None,compute))
        self.assertEqual(compute.call_count,1)

    def test_forecasts_of_different_targets_are_not_combined(self):
        _, shared = self.view([])
        home = Mock()
        home.forecast.side_effect = [{'occupancy_now': .2},{'occupancy_now': .8}]
        shared = shared_inference_temporal(shared._temporal,STAMP,home_provider=home)
        self.assertEqual(shared.home_context.forecast('light.a',STAMP)['occupancy_now'],.2)
        self.assertEqual(shared.home_context.forecast('light.b',STAMP)['occupancy_now'],.8)
        self.assertEqual(home.forecast.call_count,2)


if __name__=='__main__': unittest.main()
