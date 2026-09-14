import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from support import *
from storage import Store
from teaching_rl import RLTeaching, _feature_evidence_factor, fingerprint


class FakeEngine:
    def __init__(self, store, states, registry):
        self.store = store
        self.state_map = states
        self.entity_registry = registry
        self.context_relevance = {}
        self.lock = threading.RLock()


def make_agent(store):
    agent = store.create_agent({
        'name': 'Kitchen',
        'target_entity': 'light.kitchen',
        'target_property': 'power',
        'min_value': 0,
        'max_value': 1,
        'deadband': .5,
        'confidence_threshold': .78,
        'action_interval': 1,
        'exploration_step': 1,
        'input_entities': ['*'],
    })
    return store.get_agent_config(agent['id'])


class TeachRLEvidenceShrinkageTests(unittest.TestCase):
    def test_evidence_factor_matches_expected_table(self):
        with patch.dict('settings.OPTIONS', {'teach_rl_feature_evidence_samples': 24}):
            expected = {
                4: math.sqrt(4/24),
                8: math.sqrt(8/24),
                12: math.sqrt(12/24),
                24: 1.0,
            }
            for n, factor in expected.items():
                with self.subTest(n=n):
                    self.assertAlmostEqual(_feature_evidence_factor(n), factor, places=7)
                    self.assertAlmostEqual(0.9 * _feature_evidence_factor(n), 0.9 * factor, places=7)
            self.assertAlmostEqual(_feature_evidence_factor(48), 1.0, places=7)

    def test_supervised_score_applies_evidence_in_addition_to_coverage_and_recency(self):
        temp = tempfile.TemporaryDirectory(prefix='teach-rl-evidence-')
        try:
            store = Store(Path(temp.name) / 'test.db')
            now = time.time()
            sensor = 'binary_sensor.kitchen_presence'
            states = {
                'light.kitchen': state('light.kitchen', 'off'),
                sensor: state(sensor, 'off', device_class='occupancy'),
            }
            registry = {
                'light.kitchen': {'area_id': 'kitchen'},
                sensor: {'area_id': 'kitchen'},
            }
            agent = make_agent(store)
            service = RLTeaching(store, FakeEngine(store, states, registry))
            fp = fingerprint(agent)

            samples = []
            for i in range(12):
                ts = now - (11-i) * 6 * 3600
                desired = float(i % 2)
                samples.append((ts, desired))

            with store.lock, store.conn() as c:
                c.executemany(
                    'INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint) '
                    'VALUES(?,?,?,?,?,?)',
                    [(agent['id'], now, ts, desired, 0, fp) for ts, desired in samples],
                )
            store.archive_batch([
                (sensor, ts, 'on' if desired else 'off', {'device_class': 'occupancy'}, None, 'test')
                for ts, desired in samples
            ])

            with patch.dict('settings.OPTIONS', {
                'teach_rl_feature_min_labels': 12,
                'teach_rl_feature_min_per_binary_class': 5,
                'teach_rl_feature_min_observation_days': 2,
                'teach_rl_feature_evidence_samples': 24,
            }):
                scores, stats = service.supervised_scores(agent)

            self.assertTrue(stats['feature_selection_eligible'])
            self.assertIn(sensor, scores)
            # Perfect correlation, full coverage and zero sample age make coverage=1 and
            # recency multiplier=1. With n=12 and N=24 the only remaining penalty is
            # sqrt(12/24) = 0.707106...
            self.assertAlmostEqual(scores[sensor], math.sqrt(12/24), places=5)
        finally:
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
