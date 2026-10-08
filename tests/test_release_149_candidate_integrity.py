import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import agent, state
from storage import Store
from policy import MultiHorizonPolicy
from context import ExplicitFeatureSchema
import context_tournament_policy_candidate as candidates
import context_tournament_promotion as promotion
import test_context_tournament_policy_candidate as candidate_tests
from policy_backend import verify_model_checksum


class CandidateIntegrity149Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        chooser, migrate = promotion._choose_schema_after_promotion, promotion._migrate_schema
        self.addCleanup(setattr, promotion, '_choose_schema_after_promotion', chooser)
        self.addCleanup(setattr, promotion, '_migrate_schema', migrate)
        store = Store(Path(self.temp.name) / 'integrity.db')
        self.agent = store.create_agent(agent())
        self.challenger = 'binary_sensor.precursor'
        primary = 'binary_sensor.primary'
        self.states = {self.agent['target_entity']: state(self.agent['target_entity'], 'off'),
                       primary: state(primary, 'off', device_class='occupancy'),
                       self.challenger: state(self.challenger, 'on', device_class='occupancy')}
        self.live = MultiHorizonPolicy(self.agent, self.states, {}, set())
        self.live.schema = ExplicitFeatureSchema(self.live.dims, [primary])
        self.live.selection_meta = {'selection_reasons': {primary: ['historical']}}
        engine = SimpleNamespace(models={self.agent['id']: self.live}, state_map=self.states,
                                 entity_registry={}, context_relevance={}, context=None,
                                 temporal_history=None, state_revision=1, runtime={}, lock=threading.RLock())
        tournament = dict(agent_id=self.agent['id'], active_features=[primary],
                          challenger_features=[self.challenger], feature_scores={self.challenger: .9},
                          schema_revision=1, previous_schema=[])
        model = dict(version=1, action_count=2, counts={}, samples=0, active_correct=0,
                     shadow_correct=0, last_scored_ts=None)
        self.service = candidate_tests.ExactPolicyPromotionTests.FakeService(store, engine, tournament, model)

        def observe(a, states, changed):
            raw = self.service._load_shadow_model(a['id'], self.challenger, 2)
            return self.service._shadow_predict_index(raw, 0, 0)

        self.service.observe_shadow = observe
        candidates.install_policy_candidates(self.service)
        self.service.observe_shadow(self.agent, self.states, set())

    def test_warm_candidate_has_one_checksum_verification_per_observation(self):
        raw = self.service._model['candidate_policy']
        with patch.object(candidates, 'verify_model_checksum', wraps=verify_model_checksum) as verify:
            self.service.observe_shadow(self.agent, self.states, set())
            self.assertEqual(verify.call_count, 1)
            self.assertIs(verify.call_args.args[0], raw)
            self.service.observe_shadow(self.agent, self.states, set())
            self.assertEqual(verify.call_count, 2)

    def test_mutated_warm_payload_is_rechecked_and_rebuilt_without_changing_live(self):
        before = self.live.serialize()
        self.service._model['candidate_training_samples'] = 5
        self.service._model['candidate_policy']['model_revision'] = 'tampered'
        self.assertFalse(verify_model_checksum(self.service._model['candidate_policy']))
        self.service.observe_shadow(self.agent, self.states, set())
        repaired = self.service._model['candidate_policy']
        self.assertTrue(verify_model_checksum(repaired))
        self.assertNotEqual(repaired['model_revision'], 'tampered')
        self.assertEqual(self.service._model['candidate_training_samples'], 0)
        self.assertEqual(self.live.serialize(), before)


if __name__ == '__main__':
    unittest.main()
