import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from qualification import assess_control_qualification
from storage import Store
from teach_rl_rebenchmark import (
    BENCHMARK_SOURCE,
    install_teach_rl_rebenchmark,
)


STRONG_BINARY_DETAIL = {
    'balanced': True,
    'counts': {
        'samples': 80,
        'correct': 80,
        'per_action': {
            '0': {'samples': 40, 'correct': 40},
            '1': {'samples': 40, 'correct': 40},
        },
        'origin_counts': {'recorded': 80},
    },
}


def target_state(value, user_id=None):
    return {
        'entity_id': 'light.test',
        'state': 'on' if float(value) >= .5 else 'off',
        'attributes': {},
        'context': {'user_id': user_id, 'parent_id': None},
    }


class FakePolicy:
    VERSION = 10

    def __init__(self, agent, revision='fine-1'):
        self.agent = dict(agent)
        self.model_revision = revision
        self.schema = SimpleNamespace(entities=['binary_sensor.presence'])
        self.actions = [0.0, 1.0]
        self.horizons = [1]
        self.heads = {1: object()}

    def serialize(self):
        return {
            'version': 10,
            'model_revision': self.model_revision,
            'schema': {'version': 11, 'dims': 128, 'entities': list(self.schema.entities)},
            'selection_meta': {},
            'dims': 128,
            'actions': list(self.actions),
            'horizons': list(self.horizons),
            'heads': {},
        }


class FakeTeachService:
    def __init__(self, store, engine, agent_id):
        self.store = store
        self.engine = engine
        self.agent_id = agent_id
        self.report = {}
        self._rebenchmark_finalize_wrapped = False

    def finalize_retrain(self, agent_id):
        # Simulate the state immediately after the normal rebuild + Teach fine tuning:
        # the rebuild has a strong benchmark, but the final policy has now changed.
        self.store.set_training_state(
            agent_id, 'qualified', score=1.0, samples=80,
            source='recorded-behaviour', detail=STRONG_BINARY_DETAIL,
        )
        agent = self.store.get_agent_config(agent_id)
        policy = FakePolicy(agent, 'fine-1')
        raw = policy.serialize()
        raw['_benchmark_counts'] = dict(STRONG_BINARY_DETAIL['counts'])
        self.store.save_model(agent_id, raw)
        self.engine.models[agent_id] = policy
        self.report = {'stage': 'done', 'benchmark_score': 1.0, 'labels_applied': 2}
        return dict(self.report)

    def _set_job_stage(self, agent_id, state=None, **updates):
        self.report.update(updates)
        if state is not None:
            self.report['state'] = state
        return dict(self.report)


class FakeEngine:
    def __init__(self, store):
        self.store = store
        self.models = {}
        self.runtime = {}
        self.wake_event = threading.Event()
        self.own_echo = False
        self.samples_seen_inside_process = []
        self.executor = SimpleNamespace(release_control=lambda *args, **kwargs: [])

    def policy(self, agent):
        policy = self.models.get(agent['id'])
        if policy is None:
            policy = FakePolicy(agent)
            self.models[agent['id']] = policy
        return policy

    def own_command_echo(self, agent, state, current):
        return self.own_echo

    def process_agent(self, agent, state_map, changed_entities=None):
        fresh = self.store.get_agent_config(agent['id'])
        self.samples_seen_inside_process.append(int(fresh.get('benchmark_samples') or 0))
        current = 1.0 if state_map[agent['target_entity']]['state'] == 'on' else 0.0
        # Predict the next alternating transition perfectly.
        self.runtime.setdefault(agent['id'], {})['last_prediction'] = 1.0 - current
        return {'ok': True}


class TeachRLRebenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='teach-rebenchmark-')
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.agent = self.store.create_agent({
            'name': 'Test', 'target_entity': 'light.test', 'target_property': 'power',
            'min_value': 0, 'max_value': 1, 'deadband': .5,
            'confidence_threshold': .78, 'action_interval': 1,
            'exploration_step': 1, 'input_entities': ['*'],
        })
        self.store.set_training_state(
            self.agent['id'], 'qualified', score=1.0, samples=80,
            source='recorded-behaviour', detail=STRONG_BINARY_DETAIL,
        )
        self.engine = FakeEngine(self.store)
        self.teach = FakeTeachService(self.store, self.engine, self.agent['id'])
        self.service = install_teach_rl_rebenchmark(self.store, self.engine, self.teach)

    def tearDown(self):
        self.temp.cleanup()

    def test_final_teach_policy_invalidates_old_control_proof_but_keeps_shadow(self):
        before = self.store.get_agent_config(self.agent['id'])
        self.assertTrue(assess_control_qualification(before)['passed'])

        report = self.teach.finalize_retrain(self.agent['id'])
        after = self.store.get_agent_config(self.agent['id'])

        self.assertEqual(report['control_qualification'], 'stale')
        self.assertEqual(after['training_state'], 'qualified')
        self.assertEqual(after['mode'], 'shadow')
        self.assertIsNone(after['benchmark_score'])
        self.assertEqual(after['benchmark_samples'], 0)
        self.assertEqual(after['benchmark_source'], BENCHMARK_SOURCE)
        self.assertTrue(after['benchmark_detail']['qualification_stale'])
        self.assertEqual(after['benchmark_detail']['control_qualification'], 'stale')
        qualification = assess_control_qualification(after)
        self.assertFalse(qualification['passed'])
        self.assertEqual(qualification['state'], 'stale')

        # Store.save_model normally inherits historical benchmark counts. Teach must
        # explicitly erase that proof because it describes the pre-fine-tuned model.
        raw = self.store.get_model(self.agent['id'])
        self.assertEqual(raw.get('_benchmark_counts'), {})
        stale_events = [x for x in self.store.list_events(20)
                        if x['kind'] == 'teach_rl_control_qualification_stale']
        self.assertEqual(len(stale_events), 1)

    def test_shadow_rebenchmark_is_prequential_and_can_requalify_without_enabling_control(self):
        self.teach.finalize_retrain(self.agent['id'])
        agent = self.store.get_agent_config(self.agent['id'])

        # Seed the first prediction, then provide 40 future alternating transitions.
        states = {'light.test': target_state(0)}
        self.engine.process_agent(agent, states, {'light.test'})
        self.assertEqual(self.engine.samples_seen_inside_process[-1], 0)

        for i in range(1, 41):
            states = {'light.test': target_state(i % 2, user_id='user' if i % 7 == 0 else None)}
            agent = self.store.get_agent_config(self.agent['id'])
            self.engine.process_agent(agent, states, {'light.test'})

        fresh = self.store.get_agent_config(self.agent['id'])
        self.assertEqual(fresh['benchmark_samples'], 40)
        self.assertEqual(fresh['benchmark_score'], 1.0)
        self.assertFalse(fresh['benchmark_detail']['qualification_stale'])
        self.assertEqual(fresh['benchmark_detail']['control_qualification'], 'current')
        self.assertTrue(assess_control_qualification(fresh)['passed'])
        # Fresh qualification only makes Control eligible; it never switches mode.
        self.assertEqual(fresh['mode'], 'shadow')

        # On event #2 the fresh outcome was persisted before the inner policy processing.
        self.assertEqual(self.engine.samples_seen_inside_process[1], 1)
        qualified_events = [x for x in self.store.list_events(50)
                            if x['kind'] == 'teach_rl_rebenchmark_qualified']
        self.assertEqual(len(qualified_events), 1)

    def test_own_command_echo_is_not_fresh_shadow_evidence(self):
        self.teach.finalize_retrain(self.agent['id'])
        agent = self.store.get_agent_config(self.agent['id'])
        self.engine.process_agent(agent, {'light.test': target_state(0)}, {'light.test'})
        self.engine.own_echo = True
        agent = self.store.get_agent_config(self.agent['id'])
        self.engine.process_agent(agent, {'light.test': target_state(1)}, {'light.test'})
        fresh = self.store.get_agent_config(self.agent['id'])
        self.assertEqual(fresh['benchmark_samples'], 0)
        self.assertTrue(fresh['benchmark_detail']['qualification_stale'])

    def test_other_agent_qualification_is_untouched(self):
        other = self.store.create_agent({
            'name': 'Other', 'target_entity': 'switch.other', 'target_property': 'power',
            'min_value': 0, 'max_value': 1, 'deadband': .5,
            'confidence_threshold': .78, 'action_interval': 1,
            'exploration_step': 1, 'input_entities': ['*'],
        })
        self.store.set_training_state(
            other['id'], 'qualified', score=1.0, samples=80,
            source='recorded-behaviour', detail=STRONG_BINARY_DETAIL,
        )
        before = self.store.get_agent_config(other['id'])
        self.teach.finalize_retrain(self.agent['id'])
        after = self.store.get_agent_config(other['id'])
        self.assertEqual(after['benchmark_source'], before['benchmark_source'])
        self.assertEqual(after['benchmark_samples'], before['benchmark_samples'])
        self.assertEqual(after['benchmark_detail'], before['benchmark_detail'])
        self.assertTrue(assess_control_qualification(after)['passed'])


if __name__ == '__main__':
    unittest.main()
