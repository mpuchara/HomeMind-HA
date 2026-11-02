"""Real U05b.1 learning, calculus, isolation and unverified-artifact gates."""
from dataclasses import asdict, replace
import json
import sys
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch

import support
sys.path.insert(0, str(support.ROOT/'tools'))
import numpy as np
from universal_training_fixture import make_dataset
from universal_neural_encoder import SharedSetEncoder
from universal_action_scorer import UniversalActionScorer
from universal_scorer_training import (TrainingConfig, RankingExample, train_scorer,
    loss_gradients, logits, sample_features, checkpoint_bytes, load_core_checkpoint,
    core_artifact_status, _ADMISSION)
from universal_training_data import _digest


def reidentify(dataset, **changes):
    data=replace(dataset, **changes)
    identity=_digest(dict(version=data.version,supervision=data.supervision_revision,
                          samples=[_digest(asdict(s)) for s in data.samples]))
    return replace(data,dataset_id=identity)


class UniversalScorerLearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data,cls.split=make_dataset(samples=48)

    def train(self, data=None, **kwargs):
        return train_scorer(UniversalActionScorer(seed=1),SharedSetEncoder(),
                             self.data if data is None else data,**self.split,
                             config=TrainingConfig(epochs=24),**kwargs)

    def test_real_learning_reduces_loss_and_improves_independent_future_accuracy(self):
        parent,encoder=UniversalActionScorer(seed=1),SharedSetEncoder()
        p_before,e_before=parent.checkpoint(),encoder.checkpoint()
        result=train_scorer(parent,encoder,self.data,**self.split,config=TrainingConfig(epochs=24))
        self.assertEqual(result['status'],'optimization_complete')
        artifact=result['checkpoint']
        self.assertLess(artifact['after']['train']['loss'],artifact['before']['train']['loss']/10)
        self.assertGreater(artifact['after']['holdout']['accuracy'],artifact['before']['holdout']['accuracy'])
        self.assertEqual(artifact['after']['holdout']['accuracy'],1.)
        self.assertGreater(result['updates'],0)
        self.assertNotEqual(artifact['scorer']['layers'],p_before['layers'])
        self.assertEqual(parent.checkpoint(),p_before)
        self.assertEqual(encoder.checkpoint(),e_before)
        self.assertEqual(artifact['encoder'],e_before)
        self.assertFalse(artifact['trained'])
        self.assertFalse(artifact['promotion_eligible'])
        self.assertFalse(artifact['control_eligible'])
        self.assertEqual(artifact['validation_status'],'not_validated')

    def test_training_is_deterministic_and_only_train_rows_set_normalization(self):
        a,b=self.train(),self.train()
        self.assertEqual(checkpoint_bytes(a['checkpoint']),checkpoint_bytes(b['checkpoint']))
        # Legal holdout labels change metrics but cannot influence any training weight.
        rows=tuple(replace(s,desired_action=1-s.desired_action)
                   if s.observation_timestamp>=self.split['holdout_from'] else s for s in self.data.samples)
        changed=self.train(reidentify(self.data,samples=rows))
        self.assertEqual(a['checkpoint']['scorer'],changed['checkpoint']['scorer'])
        self.assertEqual(a['checkpoint']['normalization'],changed['checkpoint']['normalization'])

    def test_serialization_reproduces_predictions_and_rejects_corruption(self):
        result=self.train();raw=checkpoint_bytes(result['checkpoint'])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'experimental-core.json'
            path.write_bytes(raw)
            restored=load_core_checkpoint(path.read_bytes())
        for sample in self.data.samples[-4:]:
            np.testing.assert_array_equal(result['model'].scores(sample),restored.scores(sample))
        damaged=json.loads(raw);damaged['scorer']['layers']['dense1']['weights'][0][0]+=1
        with self.assertRaisesRegex(ValueError,'checksum'):
            load_core_checkpoint(json.dumps(damaged).encode())
        with self.assertRaises(ValueError):
            load_core_checkpoint(b'{"core_version":1,"core_version":2}')
        damaged=json.loads(raw);damaged['trained']=True
        damaged.pop('checksum');damaged['checksum']=_digest(damaged)
        with self.assertRaisesRegex(ValueError,'falsely verified'):
            load_core_checkpoint(checkpoint_bytes(damaged))

    def test_undo_requires_rebuild_without_mutating_parent_or_artifact(self):
        result=self.train();before=checkpoint_bytes(result['checkpoint'])
        revoked=reidentify(self.data,samples=self.data.samples[1:],supervision_revision='after-undo')
        self.assertEqual(core_artifact_status(result['checkpoint'],revoked)['status'],'requires_rebuild')
        self.assertEqual(checkpoint_bytes(result['checkpoint']),before)
        self.assertEqual(core_artifact_status(result['checkpoint'],self.data)['status'],'unvalidated_core')

    def test_bad_data_never_updates_any_parent_weights(self):
        mutations=(dict(supervision_source='shadow'),dict(history_source_revision=''),
                   dict(available_actions=None),dict(desired_action=.5),dict(home_observation=()),
                   dict(sample_weight=float('nan')))
        for change in mutations:
            bad=replace(self.data.samples[0],**change)
            if change.get('sample_weight')!=change.get('sample_weight'):
                # Nonfinite metadata cannot even obtain a valid U05a content id.
                with self.assertRaises(ValueError):
                    reidentify(self.data,samples=(bad,*self.data.samples[1:]))
                continue
            data=reidentify(self.data,samples=(bad,*self.data.samples[1:]))
            parent=UniversalActionScorer(seed=1);before=parent.checkpoint()
            result=train_scorer(parent,SharedSetEncoder(),data,**self.split)
            self.assertEqual(result['status'],'insufficient_evidence')
            self.assertIsNone(result['checkpoint'])
            self.assertEqual(parent.checkpoint(),before)

    def test_duplicate_event_or_changed_dataset_identity_rejects(self):
        data=reidentify(self.data,samples=(self.data.samples[0],*self.data.samples))
        self.assertEqual(self.train(data)['status'],'insufficient_evidence')
        self.assertEqual(self.train(replace(self.data,dataset_id='forged'))['status'],'insufficient_evidence')

    def test_future_token_is_rejected(self):
        first=self.data.samples[0]
        token=replace(first.home_observation[0],received_ts=first.observation_timestamp+1)
        bad=replace(first,home_observation=(token,*first.home_observation[1:]))
        result=self.train(reidentify(self.data,samples=(bad,*self.data.samples[1:])))
        self.assertEqual(result['status'],'insufficient_evidence')

    def test_continuous_and_categorical_legal_sets_really_learn(self):
        for kind in ('continuous','categorical'):
            data,split=make_dataset(samples=32,action_kind=kind)
            result=train_scorer(UniversalActionScorer(seed=1),SharedSetEncoder(),data,**split,
                                config=TrainingConfig(epochs=24))
            self.assertEqual(result['status'],'optimization_complete',result.get('reason'))
            self.assertGreater(result['checkpoint']['after']['holdout']['accuracy'],.9)
            self.assertLess(result['checkpoint']['after']['train']['loss'],
                            result['checkpoint']['before']['train']['loss'])

    def test_empty_holdout_or_one_train_sample_returns_controlled_status(self):
        empty=train_scorer(UniversalActionScorer(),SharedSetEncoder(),self.data,
                           train_until=1,holdout_from=2,holdout_until=3)
        self.assertEqual(empty['status'],'insufficient_evidence')
        self.assertIsNone(empty['checkpoint'])

    def test_cancellation_discards_child_and_admission_prevents_parallel_fit(self):
        stop=threading.Event();stop.set()
        self.assertEqual(self.train(cancel=stop)['status'],'cancelled')
        _ADMISSION.acquire()
        try:self.assertEqual(self.train()['status'],'busy')
        finally:_ADMISSION.release()
        calls=[0]
        def cancel_during_work(*args,**kwargs):
            calls[0]+=1
            if calls[0]>=100:stop.set()
        stop.clear()
        with patch('universal_scorer_training.TRAINING_BUDGET.checkpoint',side_effect=cancel_during_work):
            result=self.train(cancel=stop)
        self.assertEqual(result['status'],'cancelled')
        self.assertIsNone(result['checkpoint'])

    def test_feature_byte_cap_and_early_stop_are_explicit(self):
        with patch('universal_scorer_training.MAX_FEATURE_BYTES',1):
            result=self.train()
        self.assertEqual(result['status'],'insufficient_evidence')
        self.assertIn('byte cap',result['reason'])
        result=train_scorer(UniversalActionScorer(seed=1),SharedSetEncoder(),self.data,**self.split,
                            config=TrainingConfig(epochs=24,min_delta=1.,patience=2))
        self.assertEqual(result['checkpoint']['epochs'],3)
        self.assertEqual(result['checkpoint']['best_epoch'],1)

    def test_no_production_runtime_or_actuation_imports(self):
        import ast
        source=(support.ROOT/'adaptive_ai/src/universal_scorer_training.py').read_text()
        tree=ast.parse(source)
        imports={node.module for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)}
        self.assertFalse(imports & {'engine','executor','ha','agent_candidates','intent'})
        # Actual runtime methods remain unbound: only explicit offline calls exist.
        import runtime_composition
        self.assertNotIn('universal_scorer_training',runtime_composition.__dict__)


class RankingCalculusTests(unittest.TestCase):
    def test_backpropagation_matches_finite_differences_for_both_layers(self):
        model=UniversalActionScorer(room_width=4,hidden_width=4)
        model.layers={k:tuple(a.astype(np.float64) for a in pair) for k,pair in model.layers.items()}
        rng=np.random.default_rng(9)
        rows=[RankingExample(rng.normal(size=(n,model.input_width)),n-1,weight,str(n))
              for n,weight in ((2,1.),(5,3.))]
        _,gradient=loss_gradients(model,rows,l2=.01)
        for key,pair in model.layers.items():
            for j,array in enumerate(pair):
                index=np.unravel_index(int(np.argmax(np.abs(gradient[key][j]))),array.shape)
                old=array[index];epsilon=1e-6
                array[index]=old+epsilon;upper=loss_gradients(model,rows,l2=.01)[0]
                array[index]=old-epsilon;lower=loss_gradients(model,rows,l2=.01)[0]
                array[index]=old
                self.assertAlmostEqual((upper-lower)/(2*epsilon),gradient[key][j][index],places=6)

    def test_dynamic_action_counts_weighting_and_finite_large_logits(self):
        model=UniversalActionScorer(room_width=4,hidden_width=4)
        rows=[RankingExample(np.full((n,model.input_width),value),0,weight,str(n))
              for n,value,weight in ((2,1.,1.),(17,10000.,3.))]
        loss,gradient=loss_gradients(model,rows)
        self.assertTrue(np.isfinite(loss))
        self.assertAlmostEqual(loss,(np.log(2)+3*np.log(17))/4)
        self.assertTrue(all(np.isfinite(a).all() for pair in gradient.values() for a in pair))
        with self.assertRaises(ValueError):
            loss_gradients(model,[replace(rows[0],target=99)])
