"""U05b.2 independent support, baseline, regression and unlabeled-inference gates."""
import copy
from dataclasses import replace
import math
import sys
import threading
import unittest
from unittest.mock import patch

import support
sys.path.insert(0, str(support.ROOT/'tools'))
import numpy as np
from universal_action_scorer import UniversalActionScorer
from universal_neural_encoder import SharedSetEncoder
from universal_scorer_training import (TrainingConfig, train_scorer, context_from_sample,
    OfflineActionContext, checkpoint_bytes, load_core_checkpoint)
from universal_scorer_validation import (ValidationPolicy, validate_core_artifact,
    _units, _modal_value, _target_template)
from universal_training_data import chronological_split, _digest
from universal_training_fixture import make_dataset
from test_universal_scorer_training import reidentify


def train(data, split):
    result=train_scorer(UniversalActionScorer(seed=1),SharedSetEncoder(),data,**split,
                        config=TrainingConfig(epochs=24))
    if result['status']!='optimization_complete':raise AssertionError(result)
    return result


def reseal(checkpoint):
    result=copy.deepcopy(checkpoint);result.pop('checksum',None)
    result['checksum']=_digest(result)
    return result


class UnlabeledInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data,cls.split=make_dataset(samples=32)
        cls.result=train(cls.data,cls.split)

    def test_inference_context_has_no_label_or_supervision_and_reload_agrees(self):
        sample=self.data.samples[-1]
        context=context_from_sample(sample)
        self.assertIsInstance(context,OfflineActionContext)
        self.assertFalse(hasattr(context,'desired_action'))
        self.assertFalse(hasattr(context,'supervision_source'))
        first=self.result['model'].scores_context(context)
        changed=replace(sample,desired_action=float('nan'),supervision_source='candidate')
        np.testing.assert_array_equal(first,self.result['model'].scores(changed))
        restored=load_core_checkpoint(checkpoint_bytes(self.result['checkpoint']))
        np.testing.assert_array_equal(first,restored.scores_context(context))
        report=restored.rank_context(context)
        self.assertEqual(report['status'],'observer_only')
        self.assertFalse(report['control_eligible']);self.assertFalse(report['promotion_eligible'])
        self.assertTrue(all(p['confidence'] is None and p['recommendation'] is None for p in report['projections']))

    def test_zero_and_single_action_are_safe_offline_projections(self):
        context=context_from_sample(self.data.samples[0])
        empty=replace(context,available_actions=replace(context.available_actions,candidates=(),fallback='abstain'))
        report=self.result['model'].rank_context(empty)
        self.assertEqual(report['status'],'abstain');self.assertEqual(report['projections'],[])
        single=replace(context,available_actions=replace(context.available_actions,
                                                        candidates=context.available_actions.candidates[:1]))
        report=self.result['model'].rank_context(single)
        self.assertEqual(len(report['projections']),1)
        self.assertIsNone(report['projections'][0]['recommendation'])

    def test_illegal_power_fraction_future_clock_and_descriptor_mismatch_reject(self):
        context=context_from_sample(self.data.samples[0])
        action=context.available_actions.candidates[0]
        for change in (dict(value=.5),dict(kind='continuous'),dict(resources=()),
                       dict(logical_device_id=''),dict(current_value=float('nan')),
                       dict(entity_id='light.other')):
            bad=replace(action,**change)
            mask=replace(context.available_actions,candidates=(bad,*context.available_actions.candidates[1:]))
            with self.assertRaises(ValueError):self.result['model'].rank_context(replace(context,available_actions=mask))
        with self.assertRaises(ValueError):
            self.result['model'].rank_context(replace(context,registry_revision='unregistered'))
        with self.assertRaises(ValueError):
            self.result['model'].rank_context(replace(context,available_actions=replace(
                context.available_actions,mask_timestamp=context.observation_timestamp+1)))

    def test_context_is_immutable_and_historical_missing_data_does_not_fallback(self):
        context=context_from_sample(self.data.samples[0])
        for bad in (replace(context,home_observation=()),
                    replace(context,home_observation=list(context.home_observation)),
                    replace(context,history_source_revision=''),
                    replace(context,home_observation=tuple(replace(t,quality=float('nan')) for t in context.home_observation))):
            with self.assertRaises(ValueError):self.result['model'].rank_context(bad)


class QualityGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data,cls.split=make_dataset(sensors=1,samples=128,step_seconds=120.)
        cls.result=train(cls.data,cls.split)
        cls.multi,cls.multi_split=make_dataset(sensors=1,devices=2,samples=256,step_seconds=120.)
        cls.multi_result=train(cls.multi,cls.multi_split)

    def validate(self,checkpoint=None,data=None,**kwargs):
        return validate_core_artifact(self.result['checkpoint'] if checkpoint is None else checkpoint,
                                      self.data if data is None else data,**kwargs)

    def test_default_support_is_derived_and_family_allocation_increases_it(self):
        policy=ValidationPolicy()
        self.assertEqual(policy.required_units(),30)
        self.assertEqual(policy.required_units(2),36)
        self.assertGreater(policy.required_units(200),policy.required_units(2))
        for bad in (replace(policy,family_alpha=0),replace(policy,accuracy_half_width=float('nan')),
                    replace(policy,independence_window_seconds=0)):
            with self.assertRaises(ValueError):self.validate(policy=bad)

    def test_real_model_passes_baseline_gate_without_changing_any_artifact_flag(self):
        before=checkpoint_bytes(self.result['checkpoint'])
        report=self.validate()
        self.assertEqual(report['status'],'passed',report)
        self.assertEqual(report['holdout']['accuracy'],1.)
        self.assertLess(report['holdout']['baseline_accuracy'],.6)
        self.assertLess(report['holdout']['improvement_p_value_upper_bound'],.05)
        self.assertGreaterEqual(report['holdout']['independent_units'],30)
        self.assertFalse(report['trained']);self.assertFalse(report['promotion_eligible'])
        self.assertFalse(report['control_eligible'])
        self.assertEqual(checkpoint_bytes(self.result['checkpoint']),before)
        self.assertEqual(self.validate(),report)
        raw=dict(report);checksum=raw.pop('report_checksum');self.assertEqual(_digest(raw),checksum)

    def test_repeated_points_in_one_episode_cannot_inflate_support(self):
        rows=tuple(replace(s,episode_id='one-train' if s.observation_timestamp<=self.split['train_until']
                           else 'one-holdout') for s in self.data.samples)
        data=reidentify(self.data,samples=rows)
        result=train(data,self.split)
        report=validate_core_artifact(result['checkpoint'],data)
        self.assertEqual(report['status'],'insufficient_evidence')
        self.assertEqual(report['holdout_units'],1)

    def test_temporal_embargo_and_dense_episode_windows_fail_closed(self):
        data,split=make_dataset(sensors=1,samples=128)
        result=train(data,split)
        report=validate_core_artifact(result['checkpoint'],data)
        self.assertIn('split_embargo_below_independence_window',report['reasons'])
        units=_units(self.data.samples,60.*128)
        self.assertLess(len(set(units.values())),30)

    def test_whole_episode_interval_merges_overlapping_time_buckets(self):
        row=self.data.samples[0]
        points=(replace(row,sample_id='a0',episode_id='long',observation_timestamp=60.),
                replace(row,sample_id='a1',episode_id='long',observation_timestamp=240.),
                replace(row,sample_id='b',episode_id='middle',observation_timestamp=180.),
                replace(row,sample_id='c',episode_id='next',observation_timestamp=360.))
        units=_units(points,60.)
        self.assertEqual(units['a0'],units['a1']);self.assertEqual(units['a0'],units['b'])
        self.assertNotEqual(units['a0'],units['c'])

    def test_missing_episode_identity_and_changed_dataset_revision_are_rejected(self):
        rows=(replace(self.data.samples[0],episode_id=None),*self.data.samples[1:])
        data=reidentify(self.data,samples=rows)
        result=train(data,self.split)
        report=validate_core_artifact(result['checkpoint'],data)
        self.assertEqual(report['status'],'insufficient_evidence')
        self.assertIn('missing explicit episode',report['reasons'][0])
        changed=reidentify(self.data,supervision_revision='after-undo')
        report=self.validate(data=changed)
        self.assertEqual(report['status'],'dataset_invalidated')

    def test_checkpoint_checksum_manifest_and_normalization_are_rechecked(self):
        bad=copy.deepcopy(self.result['checkpoint']);bad['checksum']='corrupt'
        self.assertEqual(self.validate(checkpoint=bad)['status'],'invalid_artifact')
        bad=copy.deepcopy(self.result['checkpoint']);bad['samples'][0]['history_source_revision']='other'
        report=self.validate(checkpoint=reseal(bad))
        self.assertIn('training_manifest_mismatch',report['reasons'])
        bad=copy.deepcopy(self.result['checkpoint']);bad['normalization']['mean'][0]+=.001
        report=self.validate(checkpoint=reseal(bad))
        self.assertEqual(report['status'],'invalid_artifact')
        self.assertIn('normalization_not_derived_from_training_partition',report['reasons'])

    def test_stored_quality_metrics_cannot_fake_an_improvement(self):
        bad=copy.deepcopy(self.result['checkpoint'])
        for layer in bad['scorer']['layers'].values():
            layer['weights']=[[0. for _ in row] for row in layer['weights']]
            layer['bias']=[0. for _ in layer['bias']]
        bad['after']['holdout']['accuracy']=1.
        report=self.validate(checkpoint=reseal(bad))
        self.assertEqual(report['status'],'validation_failed')
        self.assertEqual(report['holdout']['accuracy'],0.)
        self.assertEqual(report['holdout']['abstentions'],43)

    def test_global_gain_cannot_hide_a_device_regression(self):
        training,holdout=chronological_split(self.multi,**self.multi_split)
        ordered=(*training,*holdout);calls=[0];bad_count=[0]
        def controlled_scores(*args):
            sample=ordered[calls[0]];calls[0]+=1;value=sample.desired_action
            if sample in holdout and sample.target_device.endswith('fixture0') and bad_count[0]<10:
                value=1.;bad_count[0]+=1
            return np.asarray([1.,0.]) if value==0 else np.asarray([0.,1.])
        with patch('universal_scorer_validation.logits',side_effect=controlled_scores):
            report=validate_core_artifact(self.multi_result['checkpoint'],self.multi)
        self.assertGreater(report['holdout']['mean_paired_gain'],0)
        self.assertEqual(report['status'],'validation_failed')
        self.assertIn('observed_device_property_regression',report['reasons'])

    def test_too_few_discordant_units_do_not_pass_significance(self):
        training,holdout=chronological_split(self.data,**self.split)
        ordered=(*training,*holdout);calls=[0];wins=[0]
        def controlled_scores(*args):
            sample=ordered[calls[0]];calls[0]+=1;value=0.
            if sample in holdout and sample.desired_action==1 and wins[0]<3:
                value=1.;wins[0]+=1
            return np.asarray([1.,0.]) if value==0 else np.asarray([0.,1.])
        with patch('universal_scorer_validation.logits',side_effect=controlled_scores):
            report=self.validate()
        self.assertEqual(report['status'],'validation_failed')
        self.assertEqual(report['holdout']['winning_units'],3)
        self.assertAlmostEqual(report['holdout']['improvement_p_value_upper_bound'],.125)
        self.assertIn('paired_unit_improvement_not_significant',report['reasons'])

    def test_global_device_gain_cannot_hide_increased_false_on_errors(self):
        training,holdout=chronological_split(self.data,**self.split)
        ordered=(*training,*holdout);calls=[0];errors=[0]
        def controlled_scores(*args):
            sample=ordered[calls[0]];calls[0]+=1;value=sample.desired_action
            if sample in holdout and sample.desired_action==0 and errors[0]<8:
                value=1.;errors[0]+=1
            return np.asarray([1.,0.]) if value==0 else np.asarray([0.,1.])
        with patch('universal_scorer_validation.logits',side_effect=controlled_scores):
            report=self.validate()
        self.assertGreater(report['holdout']['mean_paired_gain'],0)
        self.assertTrue(all(s['mean_paired_gain']>=0 for s in report['slices']))
        self.assertEqual(report['status'],'validation_failed')
        self.assertIn('observed_action_class_regression',report['reasons'])
        self.assertEqual(report['holdout']['false_on'],8)

    def test_perfect_accuracy_cannot_pass_when_baseline_is_already_perfect(self):
        data=reidentify(self.data,samples=tuple(replace(s,desired_action=1.) for s in self.data.samples))
        result=train(data,self.split);report=validate_core_artifact(result['checkpoint'],data)
        self.assertEqual(report['holdout']['accuracy'],1.)
        self.assertEqual(report['holdout']['baseline_accuracy'],1.)
        self.assertEqual(report['status'],'validation_failed')
        self.assertIn('no_positive_gain_over_train_only_baseline',report['reasons'])

    def test_empty_holdout_and_small_independent_sample_return_insufficient_evidence(self):
        bad=copy.deepcopy(self.result['checkpoint'])
        end=bad['split']['holdout_until']
        bad['split'].update(holdout_from=end+120,holdout_until=end+240)
        self.assertIn('empty_train_or_future_holdout',self.validate(checkpoint=reseal(bad))['reasons'])
        data,split=make_dataset(sensors=1,samples=32,step_seconds=120.)
        result=train(data,split);report=validate_core_artifact(result['checkpoint'],data)
        self.assertIn('too_few_independent_global_units',report['reasons'])

    def test_every_trained_device_requires_future_evidence(self):
        data,split=make_dataset(sensors=1,devices=128,samples=128,step_seconds=120.)
        result=train(data,split)
        report=validate_core_artifact(result['checkpoint'],data)
        self.assertEqual(report['status'],'insufficient_evidence')
        self.assertIn('missing_independent_device_property_coverage',report['reasons'])
        self.assertTrue(report['uncovered_slices'])

    def test_baseline_selection_does_not_see_future_labels(self):
        rows=tuple(replace(s,desired_action=1-s.desired_action)
                   if s.observation_timestamp>=self.split['holdout_from'] else s for s in self.data.samples)
        data=reidentify(self.data,samples=rows);result=train(data,self.split)
        changed=validate_core_artifact(result['checkpoint'],data)
        original=self.validate()
        self.assertEqual(changed['baseline_id'],original['baseline_id'])
        self.assertEqual(result['checkpoint']['scorer'],self.result['checkpoint']['scorer'])
        self.assertEqual(changed['status'],'validation_failed')

    def test_continuous_baseline_scales_and_categorical_baseline_tracks_labels(self):
        data,_=make_dataset(samples=8,action_kind='continuous')
        sample=data.samples[1];self.assertEqual(_target_template(sample),('relative_position',1.))
        actions=tuple(replace(a,lower=0.,upper=100.,value=v) for a,v in zip(sample.available_actions.candidates,(0.,40.,100.)))
        modified=replace(sample,available_actions=replace(sample.available_actions,candidates=actions))
        self.assertEqual(_modal_value(modified,('relative_position',.5)),40.)
        self.assertEqual(sample.desired_action,35.)
        data,_=make_dataset(samples=8,action_kind='categorical');sample=data.samples[1]
        actions=tuple(replace(a,option_label=label) for a,label in zip(sample.available_actions.candidates,('comfort','normal','eco')))
        modified=replace(sample,available_actions=replace(sample.available_actions,candidates=actions))
        self.assertEqual(_modal_value(modified,('categorical','comfort')),0.)

    def test_validation_can_cancel_and_never_mutates_the_parent_checkpoint(self):
        stop=threading.Event();stop.set();before=checkpoint_bytes(self.result['checkpoint'])
        report=self.validate(cancel=stop)
        self.assertEqual(report['status'],'cancelled')
        self.assertEqual(checkpoint_bytes(self.result['checkpoint']),before)
