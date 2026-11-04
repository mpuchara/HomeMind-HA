import copy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

import support
sys.path.insert(0,str(support.ROOT/'tools'))
from ha import HAClient
from ha_native_experiments import CompactRuntimeCache,recorder_assessment,transport_case
from profile_universal_runtime import StackSampler,quantiles


class HistoryRequestTests(unittest.TestCase):
    def test_full_history_disables_significant_default_explicitly(self):
        client=HAClient('http://not-contacted/api','')
        with patch.object(client,'request',return_value=[]) as request:
            client.history(['light.example'],'2026-01-01T00:00:00Z','2026-01-02T00:00:00Z',
                           minimal=False,no_attributes=False,significant=False)
        query=parse_qs(urlsplit(request.call_args.args[1]).query,keep_blank_values=True)
        self.assertEqual(query['significant_changes_only'],['0'])
        self.assertNotIn('minimal_response',query);self.assertNotIn('no_attributes',query)

    def test_default_history_still_requests_minimal_significant_without_attributes(self):
        client=HAClient('http://not-contacted/api','')
        with patch.object(client,'request',return_value=[]) as request:
            client.history(['sensor.example'],'start','end')
        query=parse_qs(urlsplit(request.call_args.args[1]).query,keep_blank_values=True)
        self.assertIn('significant_changes_only',query)
        self.assertIn('no_attributes',query);self.assertIn('minimal_response',query)


class CompactContractTests(unittest.TestCase):
    def setUp(self):
        self.cache=CompactRuntimeCache();self.cache.begin('one')
        self.initial={'a':{'sensor.x':dict(s='0',a={'keep':1,'remove':2},c={'id':'a','parent_id':'p','user_id':'u'},lc=10.,lu=11.)}}
        self.cache.apply(self.initial,session='one',initial=True)

    def test_attribute_remove_partial_context_and_clocks_preserve_known_fields(self):
        message={'c':{'sensor.x':{'+':{'a':{'keep':3},'c':'b','lu':12.},'-':{'a':['remove']}}}}
        frozen=copy.deepcopy(message);self.cache.apply(message,session='one')
        row=self.cache.states['sensor.x']
        self.assertEqual(row['attributes'],{'keep':3})
        self.assertEqual(row['context'],{'id':'b','parent_id':'p','user_id':'u'})
        self.assertEqual((row['last_changed'],row['last_updated']),(10.,12.))
        self.cache.apply({'c':{'sensor.x':{'+':{'lc':13.,'c':{'parent_id':None}}}}},session='one')
        self.assertEqual(self.cache.states['sensor.x']['last_updated'],13.)
        self.assertIsNone(self.cache.states['sensor.x']['context']['parent_id'])
        self.assertEqual(message,frozen)

    def test_removal_reconnect_and_changed_subscription_require_fresh_base(self):
        self.cache.apply({'r':['sensor.x']},session='one');self.assertFalse(self.cache.states)
        self.cache.begin('two')
        with self.assertRaisesRegex(ValueError,'stale session'):
            self.cache.apply(self.initial,session='one',initial=True)
        with self.assertRaises(ValueError):self.cache.apply({'c':{'sensor.x':{'+':{'s':'1'}}}},session='two')
        self.assertFalse(self.cache.ready)
        self.cache.apply({'a':{}},session='two',initial=True)
        self.assertFalse(self.cache.states)  # Empty permission-filtered snapshot does not resurrect old state.
        self.assertFalse(self.cache.history_complete);self.assertFalse(self.cache.evidence_eligible)

    def test_invalid_partial_message_cannot_commit_earlier_changes(self):
        with self.assertRaises(ValueError):
            self.cache.apply({'c':{'sensor.x':{'+':{'s':'1'}},'sensor.unknown':{'+':{'s':'2'}}}},session='one')
        self.assertFalse(self.cache.ready);self.assertFalse(self.cache.states)

    def test_compact_context_id_does_not_invent_unknown_parent_or_user(self):
        initial=copy.deepcopy(self.initial);initial['a']['sensor.x']['c']='only-id'
        self.cache.apply(initial,session='one',initial=True)
        self.assertEqual(self.cache.states['sensor.x']['context'],{'id':'only-id'})

    def test_malformed_attributes_clocks_and_unknown_contract_fail_closed(self):
        for fields in ({'a':None},{'lc':float('nan')},{'unknown_clock':42}):
            self.cache.begin('one');self.cache.apply(self.initial,session='one',initial=True)
            with self.assertRaises(ValueError):self.cache.apply({'c':{'sensor.x':{'+':fields}}},session='one')
            self.assertFalse(self.cache.ready)

    def test_transport_replay_preserves_selected_state_but_never_claims_evidence(self):
        result=transport_case(50,10,40)
        self.assertTrue(result['equivalent_selected_runtime_state'])
        self.assertFalse(result['history_complete']);self.assertFalse(result['evidence_eligible'])
        self.assertIsNone(result['event_to_intent_ms'])
        self.assertEqual(result['variant_c_dual_stream']['bytes'],
                         result['variant_a_events']['bytes']+result['variant_b_filtered_compact']['bytes'])


class AuxiliaryEvidenceTests(unittest.TestCase):
    def test_recorder_full_minimal_missing_and_unavailable_do_not_certify_coverage(self):
        for rows in (None,[],[[{'entity_id':'sensor.x','state':'1','attributes':{},'context':{},
                               'last_changed':'old','last_updated':'old'}]],[[{'state':'2'}]]):
            result=recorder_assessment(rows,['sensor.x','sensor.y'])
            self.assertFalse(result['history_complete']);self.assertFalse(result['evidence_eligible'])
            self.assertIn('sensor.y',result['missing_requested_entities'])

    def test_stack_sampler_is_bounded_and_does_not_capture_local_values(self):
        secret_marker='NEVER_CAPTURE_THIS_VALUE'
        with StackSampler(True) as sampler:time.sleep(.12)
        result=sampler.summary()
        self.assertGreater(result['samples'],0)
        self.assertLessEqual(len(result['stacks']),2048)
        self.assertNotIn(secret_marker,str(result))
        self.assertIn('not CPU',result['meaning'])
        self.assertIsNone(quantiles([])['p95_ms'])
        self.assertEqual(quantiles([2.,1.,3.])['max_ms'],3.)


if __name__=='__main__':unittest.main()
