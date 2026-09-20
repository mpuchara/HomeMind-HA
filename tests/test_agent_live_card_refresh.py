import threading
import unittest
from types import SimpleNamespace

from support import agent, state
from agent_live_card_refresh import live_agent_payload


class _Store:
    def __init__(self, agents):
        self.agents = agents

    def list_agent_configs(self):
        return list(self.agents)


class AgentLiveCardRefreshTests(unittest.TestCase):
    def test_payload_contains_all_three_live_card_values(self):
        configured = agent(id='live-a', target_entity='light.kitchen', target_property='power')
        engine = SimpleNamespace(
            lock=threading.RLock(),
            state_map={'light.kitchen': state('light.kitchen', 'on')},
            all_agent_configs={'live-a': configured},
            _refresh_agent_index=lambda: None,
            runtime={
                'live-a': {
                    'last_prediction': 0.0,
                    'last_confidence': 0.83,
                    'last_inference_ts': 123.5,
                    'teaching_id': 7,
                }
            },
        )
        core = SimpleNamespace(STORE=_Store([configured]), ENGINE=engine)

        payload = live_agent_payload(core, include_configs=True)

        self.assertEqual(len(payload['agents']), 1)
        live = payload['agents'][0]
        self.assertEqual(live['id'], 'live-a')
        self.assertEqual(live['current_value'], 1.0)
        self.assertEqual(live['last_prediction'], 0.0)
        self.assertAlmostEqual(live['last_confidence'], 0.83)
        self.assertEqual(live['last_inference_ts'], 123.5)
        self.assertEqual(live['teaching_id'], 7)
        self.assertIsNotNone(live['live_snapshot_ts'])
        self.assertEqual(payload['configs'][0]['id'], 'live-a')

    def test_payload_preserves_option_label_for_desired_tile(self):
        configured = agent(
            id='select-a',
            target_entity='select.mode',
            target_property='option_index',
            min_value=0,
            max_value=2,
        )
        engine = SimpleNamespace(
            lock=threading.RLock(),
            state_map={
                'select.mode': state('select.mode', 'eco', options=['off', 'eco', 'boost'])
            },
            all_agent_configs={'select-a': configured},
            _refresh_agent_index=lambda: None,
            runtime={'select-a': {'last_prediction': 2.0, 'last_confidence': 0.91}},
        )
        core = SimpleNamespace(STORE=_Store([configured]), ENGINE=engine)

        live = live_agent_payload(core)['agents'][0]
        self.assertEqual(live['last_prediction_label'], 'boost')
        self.assertAlmostEqual(live['last_confidence'], 0.91)


if __name__ == '__main__':
    unittest.main()
