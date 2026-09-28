import unittest

from hybrid_inference_benchmark import fixture
from observation_contract import policy_features
from observation_space import observation_as_of
from shared_inference_context import shared_inference_temporal


class SharedInferenceContextParityTests(unittest.TestCase):
    def test_shared_context_preserves_ridge_and_mlp_inputs_exactly(self):
        states, temporal, home, live, _candidate, mask, _live_mlp, _candidate_mlp, ts = fixture(8)

        baseline_features, baseline_labels, baseline_meta = policy_features(
            live, states, temporal, at_ts=ts
        )
        baseline_observation = observation_as_of(
            mask, states, temporal, ts, live.agent, home_provider=home
        )
        self.assertEqual(home.calls, 2)

        states2, temporal2, home2, live2, _candidate2, mask2, _lm2, _cm2, ts2 = fixture(8)
        shared = shared_inference_temporal(
            temporal2, ts2, home_provider=home2
        )
        shared_features, shared_labels, shared_meta = policy_features(
            live2, states2, shared, at_ts=ts2
        )
        shared_observation = observation_as_of(
            mask2, states2, shared, ts2, live2.agent,
            home_provider=shared.home_context,
        )

        self.assertEqual(shared_features, baseline_features)
        self.assertEqual(shared_labels, baseline_labels)
        self.assertEqual(shared_meta, baseline_meta)
        self.assertEqual(shared_observation, baseline_observation)
        self.assertEqual(home2.calls, 1)

        diag = shared.diagnostics()
        self.assertEqual(diag["forecast_misses"], 1)
        self.assertEqual(diag["forecast_hits"], 1)
        self.assertGreater(diag["sample_misses"], 0)
        self.assertGreater(diag["sample_hits"], 0)

    def test_shared_context_is_scoped_to_exact_timestamp(self):
        _states, temporal, home, _live, _candidate, _mask, _lm, _cm, ts = fixture(4)
        first = shared_inference_temporal(temporal, ts, home_provider=home)
        same = shared_inference_temporal(first, ts, home_provider=home)
        later = shared_inference_temporal(first, ts + 0.001, home_provider=home)

        self.assertIs(same, first)
        self.assertIsNot(later, first)
        self.assertAlmostEqual(later.timestamp, ts + 0.001)

    def test_forecast_cache_returns_isolated_dict_copies(self):
        _states, temporal, home, _live, _candidate, _mask, _lm, _cm, ts = fixture(4)
        shared = shared_inference_temporal(temporal, ts, home_provider=home)
        one = shared.home_context.forecast("light.target", ts)
        one["occupancy_now"] = 999.0
        two = shared.home_context.forecast("light.target", ts)

        self.assertNotEqual(two["occupancy_now"], 999.0)
        self.assertEqual(home.calls, 1)


if __name__ == "__main__":
    unittest.main()
