import threading
import unittest
from pathlib import Path

from paused_shadow_inference import inference_eligible, install, paused_shadow_eligible


ROOT = Path(__file__).resolve().parents[1]


class DoneFuture:
    def done(self):
        return True

    def add_done_callback(self, callback):
        callback(self)


class InlinePool:
    def submit(self, fn, *args):
        fn(*args)
        return DoneFuture()


class FakeStop:
    def is_set(self):
        return False


class FakeWake:
    def __init__(self):
        self.count = 0

    def set(self):
        self.count += 1


class FakeExperiments:
    def __init__(self):
        self.cancelled = []

    def cancel(self, agent_id, reason):
        self.cancelled.append((agent_id, reason))

    def watches(self, agent_id):
        return set()


class FakeContext:
    admitted = set()


class FakeStore:
    def __init__(self, agents, models=None):
        self.agents = {str(a['id']): dict(a) for a in agents}
        self.models = dict(models or {})
        self.events = []

    def list_agent_configs(self):
        return [dict(v) for v in self.agents.values()]

    def get_agent_config(self, agent_id):
        row = self.agents.get(str(agent_id))
        return dict(row) if row else None

    def get_model(self, agent_id):
        value = self.models.get(str(agent_id))
        return dict(value) if isinstance(value, dict) else value

    def event(self, agent_id, level, kind, message, data=None):
        self.events.append((agent_id, level, kind, message, data))


class FakeEngine:
    def __init__(self):
        self.models = {}
        self.runtime = {}
        self.experiments = FakeExperiments()
        self.context = FakeContext()
        self.in_flight = {}
        self.resubmit_targets = set()
        self.control_workers = InlinePool()
        self.lock = threading.RLock()
        self.dirty_entities = set()
        self.wake_event = FakeWake()
        self.stop_event = FakeStop()
        self.state_revision = 1
        self.state_map = {'switch.demo': {'entity_id': 'switch.demo', 'state': 'off'}}
        self.processed = []

    def process_agent(self, agent, states, changed_entities=None):
        self.processed.append(str(agent['id']))
        self.runtime.setdefault(str(agent['id']), {})['last_prediction'] = 1.0

    def runtime_for(self, agent):
        return dict(self.runtime.get(str(agent['id'])) or {})


class FakeCore:
    def __init__(self, store, engine):
        self.STORE = store
        self.ENGINE = engine

    def runtime_available(self):
        return True


def agent(agent_id='a1', *, mode='shadow', training='paused', enabled=True):
    return {
        'id': agent_id,
        'enabled': enabled,
        'mode': mode,
        'training_state': training,
        'target_entity': f'switch.{agent_id}',
        'target_property': 'power',
    }


class PausedShadowInferenceTests(unittest.TestCase):
    def test_paused_shadow_requires_existing_model(self):
        a = agent()
        with_model = FakeStore([a], {'a1': {'version': 10, 'schema': {'entities': ['binary_sensor.motion']}}})
        no_model = FakeStore([a], {})
        self.assertTrue(paused_shadow_eligible(a, with_model))
        self.assertTrue(inference_eligible(a, with_model))
        self.assertFalse(paused_shadow_eligible(a, no_model))
        self.assertFalse(inference_eligible(a, no_model))

    def test_paused_never_gets_control_inference_permission(self):
        a = agent(mode='control')
        store = FakeStore([a], {'a1': {'version': 10}})
        self.assertFalse(paused_shadow_eligible(a, store))
        self.assertFalse(inference_eligible(a, store))

    def test_waiting_training_and_needs_retrain_remain_inactive(self):
        for state in ('waiting', 'training', 'needs_retrain'):
            with self.subTest(state=state):
                a = agent(training=state)
                store = FakeStore([a], {'a1': {'version': 10}})
                self.assertFalse(inference_eligible(a, store))

    def test_qualified_runtime_is_unchanged(self):
        a = agent(mode='shadow', training='qualified')
        self.assertTrue(inference_eligible(a, FakeStore([a], {})))
        a_control = agent(mode='control', training='qualified')
        self.assertTrue(inference_eligible(a_control, FakeStore([a_control], {})))

    def test_installed_extension_admits_paused_shadow_without_replacing_scheduler(self):
        a = agent()
        store = FakeStore([a], {'a1': {'version': 10, 'schema': {'entities': ['binary_sensor.motion']}}})
        engine = FakeEngine()
        core = FakeCore(store, engine)
        self.assertTrue(install(core))

        self.assertTrue(engine.inference_eligible(a))
        payload = engine.runtime_for(a)
        self.assertTrue(payload['paused_shadow_inference'])
        self.assertTrue(payload['inference_eligible'])
        source = (ROOT / 'adaptive_ai/src/paused_shadow_inference.py').read_text(encoding='utf-8')
        self.assertNotIn('engine.process =', source)
        self.assertNotIn('engine.process_target =', source)

    def test_installed_eligibility_keeps_paused_without_model_idle(self):
        a = agent()
        store = FakeStore([a], {})
        engine = FakeEngine()
        install(FakeCore(store, engine))

        self.assertFalse(engine.inference_eligible(a))
        self.assertFalse(engine.runtime_for(a)['paused_shadow_inference'])

    def test_ui_does_not_dim_paused_training_when_runtime_mode_is_shadow(self):
        source = (ROOT / 'adaptive_ai/src/static/index.html').read_text(encoding='utf-8')
        self.assertIn('.agent.paused-agent:has(.mode.shadow){opacity:1;filter:none}', source)

    def test_control_safety_modules_remain_independent(self):
        for rel in (
            'adaptive_ai/src/executor.py',
            'adaptive_ai/src/intent.py',
            'adaptive_ai/src/control_handoff.py',
            'adaptive_ai/src/qualification.py',
        ):
            text = (ROOT / rel).read_text(encoding='utf-8')
            self.assertNotIn('paused_shadow_inference', text)


if __name__ == '__main__':
    unittest.main()
