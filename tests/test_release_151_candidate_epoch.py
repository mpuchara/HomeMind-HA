"""Real policy decay, stable paired epochs and verified-source runtime caching."""
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import agent, state
from storage import Store
from context import ExplicitFeatureSchema, TemporalHistory
from policy import MultiHorizonPolicy
from policy_backend import model_checksum, verify_model_checksum
import context_tournament_policy_candidate as candidates
import context_tournament_promotion as promotion
import test_context_tournament_policy_candidate as fixtures


class CandidateEpoch151Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        chooser, migrate = promotion._choose_schema_after_promotion, promotion._migrate_schema
        self.addCleanup(setattr, promotion, '_choose_schema_after_promotion', chooser)
        self.addCleanup(setattr, promotion, '_migrate_schema', migrate)
        store = Store(Path(self.temp.name) / 'epoch.db')
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
                                 temporal_history=TemporalHistory(), state_revision=1,
                                 runtime={}, lock=threading.RLock())
        tournament = dict(agent_id=self.agent['id'], active_features=[primary],
                          challenger_features=[self.challenger], feature_scores={self.challenger: .9},
                          schema_revision=1, previous_schema=[])
        model = dict(version=1, action_count=2, counts={}, samples=0, active_correct=0,
                     shadow_correct=0, last_scored_ts=None,
                     evaluation_champion_revision=self.live.tournament_revision)
        self.service = fixtures.ExactPolicyPromotionTests.FakeService(store, engine, tournament, model)
        self.outcome = None

        def observe(a, states, changed):
            if self.outcome is not None:
                self.service._score_shadow_sample(a['id'], self.challenger, {}, self.outcome, 2, time.time())
                self.outcome = None
            raw = self.service._load_shadow_model(a['id'], self.challenger, 2)
            return self.service._shadow_predict_index(raw, 0, 0)

        self.service.observe_shadow = observe
        self.created = []

        def construct(*args, **kwargs):
            instance = MultiHorizonPolicy(*args, **kwargs)
            self.created.append(instance)
            return instance

        constructor = patch.object(candidates, 'MultiHorizonPolicy', side_effect=construct)
        constructor.start()
        self.addCleanup(constructor.stop)
        self.base_chooser = Mock(wraps=promotion._choose_schema_after_promotion)
        promotion._choose_schema_after_promotion = self.base_chooser
        candidates.install_policy_candidates(self.service)
        self.observe()
        self.assertEqual(len(self.created), 1)

    def observe(self):
        return self.service.observe_shadow(self.agent, self.states, set())

    @staticmethod
    def decay(policy):
        for head in policy.heads.values():
            head.last_decay_ts -= 61
        policy.decay()

    def test_champion_decay_preserves_candidate_evidence_and_instance(self):
        self.service._model['candidate_training_samples'] = 7
        before = copy.deepcopy(self.service._model['candidate_policy'])
        revision = self.live.model_revision
        self.decay(self.live)
        self.assertNotEqual(self.live.model_revision, revision)
        for _ in range(3):
            self.observe()
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.service._model['candidate_training_samples'], 7)
        self.assertEqual(self.service._model['candidate_policy'], before)

    def test_online_champion_update_preserves_candidate_epoch(self):
        self.service._model['candidate_training_samples'] = 9
        epoch = self.live.tournament_revision
        self.live.update(min(self.live.horizons), 0, {0: 1., 1: .2}, 1.)
        self.assertEqual(self.live.tournament_revision, epoch)
        self.observe()
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.service._model['candidate_training_samples'], 9)

    def test_candidate_runtime_decay_keeps_verified_source_binding(self):
        candidate = self.created[0]
        raw = copy.deepcopy(self.service._model['candidate_policy'])
        self.decay(candidate)
        self.assertNotEqual(candidate.model_revision, raw['model_revision'])
        self.observe()
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.service._model['candidate_policy'], raw)
        self.assertTrue(verify_model_checksum(raw))

    def test_valid_new_payload_with_same_revision_reloads_runtime_candidate(self):
        previous = self.created[0]
        raw = copy.deepcopy(self.service._model['candidate_policy'])
        raw['selection_meta']['source_marker'] = 'replaced-valid-payload'
        raw['model_checksum'] = model_checksum(raw)
        self.service._model['candidate_policy'] = raw
        self.observe()
        self.assertEqual(len(self.created), 2)
        self.assertIsNot(self.created[-1], previous)
        self.assertEqual(self.created[-1].selection_meta['source_marker'], 'replaced-valid-payload')

    def test_genuine_new_champion_epoch_rejects_old_candidate_proof(self):
        self.live.tournament_revision = 'new-rebuild-epoch'
        self.assertFalse(candidates.exact_candidate_version_matches(
            self.service._model, self.service._model['candidate_policy'],
            self.service._model['candidate_target_schema'], self.live, self.service._state))

    def test_paired_future_outcome_trains_after_champion_and_candidate_decay(self):
        candidate = self.created[0]
        before = candidate.total_updates
        self.decay(self.live)
        self.decay(candidate)
        self.states[self.agent['target_entity']] = state(self.agent['target_entity'], 'on')
        self.outcome = 1
        self.observe()
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.service._model['candidate_training_samples'], 1)
        self.assertGreater(candidate.total_updates, before)
        self.assertTrue(verify_model_checksum(self.service._model['candidate_policy']))
        self.observe()
        self.assertEqual(len(self.created), 1)

    def test_paired_outcome_from_previous_champion_epoch_is_not_trained(self):
        self.live.tournament_revision = 'new-rebuild-epoch'
        self.outcome = 1
        self.observe()
        self.assertEqual(self.service._model['candidate_training_samples'], 0)
        self.assertEqual(self.service._model['candidate_blocked_reason'], 'paired_data_version_changed')

    def test_requested_schema_reset_uses_stable_champion_epoch_after_decay(self):
        proposed = self.service._model['candidate_target_schema'] + ['binary_sensor.extra']
        self.base_chooser.return_value = (proposed, None)
        self.decay(self.live)
        self.assertEqual(promotion._choose_schema_after_promotion(
            self.agent, self.live, self.challenger, self.service._state), (None, None))
        self.assertEqual(self.service._model['evaluation_champion_revision'], self.live.tournament_revision)
        self.assertEqual(self.service._model['candidate_requested_schema'], proposed)
        self.observe()
        self.assertTrue(candidates.exact_candidate_version_matches(
            self.service._model, self.service._model['candidate_policy'],
            proposed, self.live, self.service._state))

    def test_legacy_policy_without_stable_epoch_keeps_revision_guard(self):
        del self.live.tournament_revision
        self.assertTrue(candidates.exact_candidate_version_matches(
            self.service._model, self.service._model['candidate_policy'],
            self.service._model['candidate_target_schema'], self.live, self.service._state))
        self.live.model_revision = 'legacy-replacement'
        self.assertFalse(candidates.exact_candidate_version_matches(
            self.service._model, self.service._model['candidate_policy'],
            self.service._model['candidate_target_schema'], self.live, self.service._state))


if __name__ == '__main__':
    unittest.main()
