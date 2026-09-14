import threading
import unittest
from types import SimpleNamespace

from context_tournament_requalification import install_promotion_shadow_requalification


class FakeStore:
    def __init__(self):
        self.agents = {
            'agent-a': {'id': 'agent-a', 'mode': 'control', 'target_entity': 'light.a', 'enabled': True},
            'agent-b': {'id': 'agent-b', 'mode': 'control', 'target_entity': 'light.b', 'enabled': True},
        }
        self.events = []
        self.updated = []

    def get_agent_config(self, agent_id):
        value = self.agents.get(agent_id)
        return dict(value) if value else None

    def update_agent(self, agent_id, payload):
        self.updated.append((agent_id, dict(payload)))
        self.agents[agent_id].update(payload)
        return dict(self.agents[agent_id])

    def event(self, agent_id, level, kind, message, data):
        self.events.append((agent_id, level, kind, message, data))


class FakeExecutor:
    def __init__(self, fail=False):
        self.fail = fail
        self.released = []

    def release_control(self, agent, reason='mode_change'):
        self.released.append((agent['id'], reason))
        if self.fail:
            raise RuntimeError('restore failed')
        return ['automation.previous_controller']


class FakeTournamentService:
    def __init__(self, *, promote=True, initial_mode='control', release_fail=False,
                 reason='sensor_tournament_promotion'):
        self.store = FakeStore()
        self.store.agents['agent-a']['mode'] = initial_mode
        self.engine = SimpleNamespace(
            executor=FakeExecutor(release_fail), runtime={}, wake_event=threading.Event()
        )
        self._history = []
        self._promote = promote
        self._reason = reason

    def schema_history(self, agent_id, limit=50):
        rows = [x for x in self._history if x['agent_id'] == agent_id]
        return list(reversed(rows))[:limit]

    def observe_shadow(self, agent, state_map=None, changed_entities=None):
        if self._promote:
            self._history.append({
                'id': len(self._history) + 1,
                'agent_id': agent['id'],
                'old_schema': ['binary_sensor.old'],
                'new_schema': ['binary_sensor.new'],
                'reason': self._reason,
                'promoted_entity': 'binary_sensor.new',
                'removed_entity': 'binary_sensor.old',
                'status': 'promoted',
            })
        return {'ok': True}


class PromotionShadowRequalificationTests(unittest.TestCase):
    def test_only_promoted_control_agent_moves_to_shadow(self):
        service = FakeTournamentService()
        install_promotion_shadow_requalification(service)

        result = service.observe_shadow({'id': 'agent-a'}, {}, set())

        self.assertEqual(result, {'ok': True})
        self.assertEqual(service.store.agents['agent-a']['mode'], 'shadow')
        self.assertEqual(service.store.agents['agent-b']['mode'], 'control')
        self.assertEqual(service.store.updated, [('agent-a', {'mode': 'shadow'})])
        self.assertEqual(
            service.engine.executor.released,
            [('agent-a', 'schema_changed_shadow_requalification')],
        )
        self.assertTrue(service.engine.wake_event.is_set())
        rt = service.engine.runtime['agent-a']
        self.assertEqual(rt['decision_state'], 'shadow')
        self.assertTrue(rt['schema_requalification']['required'])
        self.assertEqual(rt['schema_requalification']['history_id'], 1)

    def test_agent_already_in_shadow_is_not_changed_or_released(self):
        service = FakeTournamentService(initial_mode='shadow')
        install_promotion_shadow_requalification(service)

        service.observe_shadow({'id': 'agent-a'}, {}, set())

        self.assertEqual(service.store.agents['agent-a']['mode'], 'shadow')
        self.assertEqual(service.store.updated, [])
        self.assertEqual(service.engine.executor.released, [])
        self.assertEqual(service.store.agents['agent-b']['mode'], 'control')

    def test_no_promotion_history_means_no_mode_change(self):
        service = FakeTournamentService(promote=False)
        install_promotion_shadow_requalification(service)

        service.observe_shadow({'id': 'agent-a'}, {}, set())

        self.assertEqual(service.store.agents['agent-a']['mode'], 'control')
        self.assertEqual(service.store.updated, [])
        self.assertEqual(service.engine.executor.released, [])

    def test_unrelated_schema_history_reason_does_not_demote_control(self):
        service = FakeTournamentService(reason='manual_schema_edit')
        install_promotion_shadow_requalification(service)

        service.observe_shadow({'id': 'agent-a'}, {}, set())

        self.assertEqual(service.store.agents['agent-a']['mode'], 'control')
        self.assertEqual(service.engine.executor.released, [])

    def test_release_failure_never_restores_control_mode(self):
        service = FakeTournamentService(release_fail=True)
        install_promotion_shadow_requalification(service)

        service.observe_shadow({'id': 'agent-a'}, {}, set())

        self.assertEqual(service.store.agents['agent-a']['mode'], 'shadow')
        warnings = [e for e in service.store.events if e[2] == 'context_schema_requalification_shadow']
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0][1], 'warning')
        self.assertIn('restore failed', warnings[0][4]['release_error'])

    def test_requires_schema_history_to_be_installed_first(self):
        service = SimpleNamespace(observe_shadow=lambda *args, **kwargs: None)
        with self.assertRaises(RuntimeError):
            install_promotion_shadow_requalification(service)


if __name__ == '__main__':
    unittest.main()
