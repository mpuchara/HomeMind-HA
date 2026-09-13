import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import *
import storage as storage_module
from storage import Store
from qualification import assess_control_qualification, wilson_lower_bound
from control import legal_value, set_review_approval, review_status
from lease_journal import LeaseJournal
from handoff import HandoffError, acquire_transaction


class QualificationTests(unittest.TestCase):
    def candidate(self, off=(40,40), on=(40,40)):
        oc,on_n=off; ic,in_n=on
        return {
            'id':'a','target_entity':'light.a','target_property':'power',
            'benchmark_samples':on_n+in_n,'benchmark_score':(oc+ic)/max(1,on_n+in_n),
            'benchmark_detail':{'balanced':True,'counts':{
                'samples':on_n+in_n,'correct':oc+ic,
                'per_action':{'0':{'correct':oc,'samples':on_n},'1':{'correct':ic,'samples':in_n}}
            }}
        }

    def test_wilson_is_conservative_at_small_n(self):
        self.assertGreater(wilson_lower_bound(20,20),.78)
        self.assertLess(wilson_lower_bound(19,20),.78)

    def test_binary_control_requires_both_actions(self):
        result=assess_control_qualification(self.candidate(off=(20,20),on=(20,20)))
        self.assertTrue(result['passed'])
        weak=assess_control_qualification(self.candidate(off=(20,20),on=(19,20)))
        self.assertFalse(weak['passed'])
        self.assertLess(weak['lower_bound'],.78)

    def test_missing_action_is_not_control_qualified(self):
        a=self.candidate();a['benchmark_detail']['counts']['per_action'].pop('0')
        result=assess_control_qualification(a)
        self.assertFalse(result['passed'])
        self.assertIn('both',result['reason'])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'p1.db')
        self.patch=patch.object(storage_module,'STORE',self.store);self.patch.start()

    def tearDown(self):
        self.patch.stop();self.tmp.cleanup()

    def test_generic_number_needs_review_and_has_step_guard(self):
        a={'id':'n','target_entity':'number.limit','target_property':'value','min_value':0,'max_value':100,'deadband':1}
        st={'entity_id':'number.limit','state':'50','attributes':{'min':0,'max':100,'step':1,'unit_of_measurement':'%'}}
        with self.assertRaisesRegex(ValueError,'review'):
            legal_value(a,st,55)
        status=set_review_approval(self.store,a,st,True)
        self.assertTrue(status['ready'])
        self.assertEqual(legal_value(a,st,55),55)
        with self.assertRaisesRegex(ValueError,'command guard'):
            legal_value(a,st,70)

    def test_capability_change_invalidates_review(self):
        a={'id':'s','target_entity':'select.mode','target_property':'option_index','min_value':0,'max_value':2,'deadband':.5}
        st={'entity_id':'select.mode','state':'eco','attributes':{'options':['eco','comfort','boost']}}
        set_review_approval(self.store,a,st,True)
        self.assertTrue(review_status(self.store,a,st)['ready'])
        changed={'entity_id':'select.mode','state':'eco','attributes':{'options':['comfort','eco','boost']}}
        self.assertFalse(review_status(self.store,a,changed)['ready'])
        with self.assertRaisesRegex(ValueError,'changed'):
            legal_value(a,changed,1)

    def test_climate_command_delta_is_bounded(self):
        a={'id':'c','target_entity':'climate.room','target_property':'temperature','min_value':15,'max_value':28,'deadband':.2}
        st={'entity_id':'climate.room','state':'heat','attributes':{'temperature':20,'min_temp':15,'max_temp':28,'target_temp_step':.5}}
        self.assertEqual(legal_value(a,st,22),22)
        with self.assertRaisesRegex(ValueError,'command guard'):
            legal_value(a,st,24)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'lease.db')

    def tearDown(self):
        self.tmp.cleanup()

    def test_lease_survives_new_journal_instance(self):
        agent={'id':'a','target_entity':'light.kitchen'}
        LeaseJournal(self.store).save(agent,['automation.old'])
        lease=LeaseJournal(self.store).get('light.kitchen')
        self.assertEqual(lease['agent_id'],'a')
        self.assertEqual(lease['disabled_automations'],['automation.old'])
        LeaseJournal(self.store).clear('light.kitchen')
        self.assertIsNone(LeaseJournal(self.store).get('light.kitchen'))

    def test_transaction_rolls_back_completed_steps(self):
        disabled=[];restored=[];checkpoints=[]
        def disable(item):
            disabled.append(item)
            if item=='b': raise RuntimeError('boom')
        def restore(item): restored.append(item)
        with self.assertRaises(HandoffError) as cm:
            acquire_transaction(['a','b'],disable,restore,lambda xs:checkpoints.append(list(xs)),lambda xs:None)
        self.assertEqual(disabled,['a','b'])
        self.assertEqual(restored,['a'])
        self.assertEqual(cm.exception.changed,['a'])
        self.assertEqual(checkpoints,[['a']])


if __name__=='__main__': unittest.main()
