import unittest
from support import *
from unittest.mock import patch
from policy import DiagonalLinUCB, MultiHorizonPolicy
from policy_backend import PolicyBackend
from context import ExplicitFeatureSchema, TemporalHistory
from context_engine import ContextEngine
from settings import DEFAULT_OPTIONS
from rewards import RewardEngine


class PolicyTests(unittest.TestCase):
    def test_decay_preserves_ridge_and_halves_evidence(self):
        h=DiagonalLinUCB(4,[0,1]);h.update(1,{0:1,1:2},1)
        old=h.last_decay_ts;h.decay(old+30*86400)
        self.assertAlmostEqual(h.a[1][1],3)
        self.assertAlmostEqual(h.b[1][1],1)
        self.assertAlmostEqual(h.counts[1],.5)
        self.assertAlmostEqual(h.ctx_sq[1][1],2)

    def test_old_samples_have_less_weight(self):
        h=DiagonalLinUCB(4,[0,1]);h.update(1,{0:1},1,h.last_decay_ts-30*86400)
        self.assertAlmostEqual(h.counts[1],.5,places=5)

    def test_validation_decays(self):
        h=DiagonalLinUCB(4,[0,1]);h.validate(0,{0:1},1)
        h.decay(h.last_decay_ts+30*86400)
        self.assertAlmostEqual(h.validation_weight,.5)

    def test_negative_validation_reduces_action_confidence(self):
        h=DiagonalLinUCB(4,[0,1])
        features={0:1}
        # Make ON (arm 1) the deterministic prediction and establish strong held-out
        # reliability for that action before presenting explicit rejections.
        for _ in range(20):
            h.update(1,features,1)
        for _ in range(12):
            h.validate(1,features,1)

        on_before=h.calibration(1)
        off_before=h.calibration(0)
        validation_weight_before=h.validation_weight
        validation_samples_before=h.validation_samples

        # Rejecting OFF while the policy predicted ON must not turn ON into an implied
        # success and must not change either action's calibration.
        h.validate(0,features,-1)
        self.assertEqual(h.calibration(1),on_before)
        self.assertEqual(h.calibration(0),off_before)
        self.assertEqual(h.validation_weight,validation_weight_before)
        self.assertEqual(h.validation_samples,validation_samples_before)

        # Explicitly rejecting the action the policy predicted records failures only for
        # that predicted arm, lowering ON reliability without certifying OFF.
        for _ in range(6):
            h.validate(1,features,-1)

        on_after=h.calibration(1)
        off_after=h.calibration(0)
        self.assertLess(on_after['accuracy'],on_before['accuracy'])
        self.assertLess(on_after['ceiling'],on_before['ceiling'])
        self.assertGreater(on_after['samples'],on_before['samples'])
        self.assertEqual(off_after,off_before)

    def test_decay_roundtrip(self):
        h=DiagonalLinUCB(4,[0,1]);h.update(1,{1:1},1);h.decay(h.last_decay_ts+86400)
        loaded=DiagonalLinUCB(4,[0,1],model=h.export())
        self.assertEqual(loaded.last_decay_ts,h.last_decay_ts)
        self.assertEqual(loaded.a,h.a)

    def test_lazy_decay_is_not_every_second(self):
        h=DiagonalLinUCB(4,[0,1]);before=h.last_decay_ts
        h.decay(before+1)
        self.assertEqual(h.last_decay_ts,before)

    def test_derived_slots_do_not_collide(self):
        c=ContextEngine(DEFAULT_OPTIONS)
        states={'light.kitchen':state('light.kitchen')}
        c.configure(states,entities={'light.kitchen':{'area_id':'kitchen'}})
        p=MultiHorizonPolicy(agent(),states,{},set(),context_engine=c)
        features,labels,_=p.features(states,TemporalHistory(),at_ts=1700000000)
        self.assertEqual(len([k for k in labels if k>=p.dims-7]),7)
        self.assertTrue(all(labels[k][0].startswith('home:') for k in range(p.dims-7,p.dims)))
        self.assertIsInstance(p,PolicyBackend)

    def test_version_mismatch_requires_retrain(self):
        with self.assertRaisesRegex(ValueError,'NEEDS_RETRAIN'):
            MultiHorizonPolicy.deserialize({'version':9},agent=agent(),state_map={},registry={},hint_entities=set())

    def test_inference_export_distinct_from_training(self):
        p=MultiHorizonPolicy(agent(),{}, {},set())
        p.update(1,1,{0:1},1)
        exported=p.inference_export()
        self.assertIn('theta',exported['heads']['1'])
        self.assertNotIn('b',exported['heads']['1'])
        self.assertIn('b',p.serialize()['heads']['1'])


class RewardTests(unittest.TestCase):
    def setUp(self): self.r=RewardEngine()

    def test_manual_is_strong_negative(self):
        self.assertEqual(self.r.evaluate(manual_correction=True,accepted=True).value,-1)

    def test_weak_acceptance(self):
        self.assertEqual(self.r.evaluate(accepted=True).value,.15)

    def test_ack_alone_not_acceptance(self):
        self.assertEqual(self.r.evaluate().value,0)

    def test_confirmed_anticipation(self):
        r=self.r.evaluate(anticipated=True,arrival_delay=2,horizon=3)
        self.assertGreater(r.value,.45)
        self.assertGreater(r.components['useful_anticipation'],0)

    def test_false_positive(self):
        r=self.r.evaluate(anticipated=True,observation_complete=True,observation_known=True)
        self.assertEqual(r.components['false_positive'],-.6)

    def test_unknown_sensing_not_false_positive(self):
        self.assertEqual(self.r.evaluate(anticipated=True,observation_complete=True).value,0)

    def test_too_early(self):
        self.assertEqual(self.r.evaluate(anticipated=True,arrival_delay=8,horizon=3).value,-.35)

    def test_chatter(self):
        self.assertEqual(self.r.evaluate(chatter=True).value,-.2)

    def test_components_always_sum_to_bounded_total(self):
        for manual in (True,False):
            for delay in (None,1,10):
                result=self.r.evaluate(manual_correction=manual,accepted=True,anticipated=True,arrival_delay=delay,chatter=True,observation_complete=True,observation_known=True)
                self.assertAlmostEqual(sum(result.components.values()),result.value)
                self.assertLessEqual(abs(result.value),1)
