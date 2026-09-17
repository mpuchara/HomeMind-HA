import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from support import agent, state
from context import TemporalHistory
from observation_contract import (
    FeatureJournal,
    FeatureSchemaV12,
    build_observation_features,
    register_live_sample,
)
from storage import Store


def stamp(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def sensor_state(value, ts):
    st = state('sensor.fast', value, unit_of_measurement='%')
    st['last_updated'] = stamp(ts)
    st['last_changed'] = stamp(ts)
    return st


class ObservationEdgeContractTests(unittest.TestCase):
    def test_rejected_out_of_order_sample_is_not_eligible_for_high_res_journal(self):
        temp = tempfile.TemporaryDirectory()
        try:
            store = Store(Path(temp.name) / 'test.db')
            journal = FeatureJournal(store, clock=lambda: 20.0)
            temporal = TemporalHistory(maxlen=96)

            newest = sensor_state('20', 12.0)
            temporal.add('sensor.fast', 12.0, dict(newest))
            self.assertTrue(register_live_sample(
                temporal, 'sensor.fast', newest, 12.0, 12.01, 'ha_state_changed'))
            journal.record('sensor.fast', newest, event_time=12.0, received_time=12.01,
                           source='ha_state_changed', event_key='accepted')

            stale = sensor_state('99', 11.0)
            temporal.add('sensor.fast', 11.0, dict(stale))  # TemporalHistory rejects rewind.
            accepted = register_live_sample(
                temporal, 'sensor.fast', stale, 11.0, 13.0, 'ha_state_changed')
            self.assertFalse(accepted)
            if accepted:
                journal.record('sensor.fast', stale, event_time=11.0, received_time=13.0,
                               source='ha_state_changed', event_key='stale')

            with store.conn() as c:
                rows = c.execute(
                    "SELECT event_key,state FROM feature_observation_events ORDER BY received_time"
                ).fetchall()
            self.assertEqual([(r['event_key'], r['state']) for r in rows], [('accepted', '20')])
        finally:
            temp.cleanup()

    def test_sparse_fast_numeric_context_is_explicitly_not_reconstructable(self):
        temporal = TemporalHistory(maxlen=96)
        current = sensor_state('0', 100.0)
        temporal.add('sensor.fast', 100.0, dict(current))
        self.assertTrue(register_live_sample(
            temporal, 'sensor.fast', current, 100.0, 100.01, 'ha_state_changed'))

        schema = FeatureSchemaV12(128, ['sensor.fast'])
        _vector, _labels, meta = build_observation_features(
            schema, {'sensor.fast': current}, temporal, 100.02, agent())

        self.assertFalse(meta['reconstruction_complete'])
        self.assertIn(
            'sensor.fast:high_resolution_numeric_history_unavailable',
            meta['reconstruction_reasons'],
        )
        obs = meta['entity_observations']['sensor.fast']
        self.assertEqual(obs['lag_coverage'], [False, False, False])
        self.assertLessEqual(obs['quality'], 0.35)


if __name__ == '__main__':
    unittest.main()
