import json
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from additional_signal import evaluate, normalize
from illumination_defaults import fill_missing_threshold, suggestions
from lighting_conditions import condition_tree
import test_agent_explore as fixture

LIGHT='sensor.kitchen_light'
SELECTED={'id':'stairs','target_entity':'light.stairs'}


def states(unit=None):
    return {LIGHT:{'entity_id':LIGHT,'state':'20','last_updated':100,'attributes':{'unit_of_measurement':unit} if unit else {}}}


def automation(aid='automation.kitchen',below=40,target='light.kitchen',**kw):
    return {'entity_id':aid,'enabled':True,'config_status':'fresh','target_entities':[target],
            'action_services':['light.turn_on'],'direct_on_conditions':True,
            'condition_tree':condition_tree([{'condition':'numeric_state','entity_id':LIGHT,'below':below}]),**kw}


def goal(aid='kitchen',threshold=50,**kw):
    return {'id':aid,'name':aid,'enabled':True,'training_state':'qualified',
            'additional_signal':normalize({'entity_id':LIGHT,'purpose':'avoid_bright_on','threshold':threshold,'hysteresis':2}),**kw}


class ThresholdReferenceTests(unittest.TestCase):
    def test_same_sensor_automation_default_preserves_boundary(self):
        default=suggestions(SELECTED,[],[automation()],states())[LIGHT]
        self.assertEqual(default['config']['threshold'],40)
        self.assertEqual(default['config']['hysteresis'],0)
        self.assertEqual(default['source']['id'],'automation.kitchen')

    def test_saved_choice_then_target_automation_before_other_references(self):
        own=goal('stairs',71,target_entity='light.stairs')
        autos=[automation(),automation('automation.stairs',45,'light.stairs')]
        self.assertEqual(suggestions(own,[goal()],autos,states())[LIGHT]['config']['threshold'],71)
        result=suggestions(SELECTED,[goal()],autos,states())[LIGHT]
        self.assertEqual(result['config']['threshold'],45)
        self.assertTrue(result['conflicting'])

    def test_qualified_live_goal_before_other_automation_not_unfinished_candidate(self):
        agents=[goal('waiting',10,training_state='waiting'),goal('disabled',12,enabled=False),goal()]
        result=suggestions(SELECTED,agents,[automation()],states())[LIGHT]
        self.assertEqual(result['config']['threshold'],50)
        self.assertEqual(len(result['alternatives']),2)
        self.assertEqual(result['config']['hysteresis'],2)

    def test_helper_bound_is_snapshot_and_unavailable_not_invented(self):
        sensor_states={**states(),'input_number.limit':{'state':'37'}}
        result=suggestions(SELECTED,[],[automation(below='input_number.limit')],sensor_states)[LIGHT]
        self.assertEqual(result['config']['threshold'],37)
        self.assertEqual(result['source']['bound_entity'],'input_number.limit')
        self.assertEqual(suggestions(SELECTED,[],[automation(below='input_number.limit')],states()),{})

    def test_unsupported_branches_attributes_templates_and_off_do_not_supply_defaults(self):
        low={'condition':'numeric_state','entity_id':LIGHT,'below':40}
        trees=[condition_tree([{'condition':'or','conditions':[low,{'condition':'state','entity_id':'input_boolean.override','state':'on'}]}]),
               condition_tree([{'condition':'not','conditions':[low]}]),condition_tree([{**low,'attribute':'level'}]),
               condition_tree([{**low,'value_template':'{{ value }}'}])]
        for tree in trees:
            self.assertEqual(suggestions(SELECTED,[],[automation(condition_tree=tree)],states()),{})
        for override in [{'direct_on_conditions':False},{'action_services':['light.turn_off']}]:
            self.assertEqual(suggestions(SELECTED,[],[automation(**override)],states()),{})
        for value in [True,float('nan'),-1]:
            self.assertEqual(suggestions(SELECTED,[],[automation(below=value)],states()),{})

    def test_and_tightest_bound_alternatives_and_cached_status(self):
        tree=condition_tree([{'condition':'numeric_state','entity_id':LIGHT,'below':v} for v in (40,30)])
        autos=[automation(condition_tree=tree),automation('automation.cached',70,enabled=False,config_status='cached')]
        result=suggestions(SELECTED,[],autos,states())[LIGHT]
        self.assertEqual(result['config']['threshold'],30)
        self.assertEqual(result['alternatives'][1]['source']['config_status'],'cached')
        self.assertTrue(result['conflicting'])

    def test_unit_change_and_another_sensor_do_not_reuse_goal_threshold(self):
        self.assertEqual(suggestions(SELECTED,[goal()],[],states('lx')), {})
        other=goal();other['additional_signal']=normalize({'entity_id':'sensor.other_light','purpose':'avoid_bright_on','threshold':99})
        self.assertNotIn(LIGHT,suggestions(SELECTED,[other],[],{**states(),'sensor.other_light':{'state':'30','attributes':{}}}))

    def test_explicit_edit_wins_and_absent_reference_requires_input(self):
        defaults=suggestions(SELECTED,[],[automation()],states())
        request={'entity_id':LIGHT,'purpose':'avoid_bright_on','threshold':29,'hysteresis':1}
        value,source=fill_missing_threshold(request,defaults)
        self.assertEqual(value,request);self.assertIsNone(source)
        self.assertIsNone(fill_missing_threshold({**request,'threshold':None},defaults)[0]['threshold'])
        with self.assertRaises(ValueError):fill_missing_threshold({'entity_id':LIGHT,'purpose':'avoid_bright_on'}, {})

    def test_copied_strict_below_matches_new_on_preference_at_boundary(self):
        config=suggestions(SELECTED,[],[automation()],states())[LIGHT]['config']
        for value,need in [(39,True),(40,False)]:
            current=states();current[LIGHT]['state']=str(value)
            self.assertIs(evaluate({'additional_signal':config},current,100)['need'],need)


class ThresholdWorkflowTests(unittest.TestCase):
    setUp=fixture.AgentExploreTests.setUp
    tearDown=fixture.AgentExploreTests.tearDown

    def knowledge(self):
        self.engine.state_map.update(states())
        return patch('ha.AUTOMATION_KNOWLEDGE',SimpleNamespace(lock=threading.RLock(),automations=[automation()]))

    def test_status_returns_sources_without_editing_parent(self):
        before=self.store.get_agent_config(self.root['id'])
        hidden=self.manager.workflow_autonomous(self.root['id'])
        hidden=self.manager.lineage_status(hidden['child_generation_id'])
        self.store.update_agent(hidden['agent_id'],{'additional_signal':goal(threshold=99)['additional_signal']})
        self.store.set_training_state(hidden['agent_id'],'qualified')
        from agent_candidates import candidate_training_scope
        with self.knowledge(),candidate_training_scope():result=self.manager.workflow_explore_status(self.root['id'])
        self.assertEqual(result['illumination_defaults'][LIGHT]['config']['threshold'],40)
        self.assertEqual(len(result['illumination_defaults'][LIGHT]['alternatives']),1)
        self.assertEqual(self.store.get_agent_config(self.root['id']),before)

    def test_missing_threshold_resolves_and_persists_child_and_reference_only(self):
        with self.knowledge():
            result=self.manager.workflow_explore(self.root['id'],{'mode':'additional_signal','additional_signal':{
                'entity_id':LIGHT,'purpose':'avoid_bright_on'}})
        child=self.manager.lineage_status(result['child_generation_id'])
        self.assertEqual(self.store.get_agent_config(child['agent_id'])['additional_signal']['threshold'],40)
        self.assertEqual(result['session']['requested_config']['threshold_default_source']['id'],'automation.kitchen')
        self.assertIsNone(self.store.get_agent_config(self.root['id'])['additional_signal'])
        self.executor.submit.assert_not_called()

    def test_user_edited_threshold_is_the_trained_child_configuration(self):
        with self.knowledge():
            result=self.manager.workflow_explore(self.root['id'],{'mode':'additional_signal','additional_signal':{
                'entity_id':LIGHT,'purpose':'avoid_bright_on','threshold':27,'hysteresis':1}})
        child=self.manager.lineage_status(result['child_generation_id'])
        self.assertEqual(self.store.get_agent_config(child['agent_id'])['additional_signal']['threshold'],27)
        self.executor.submit.assert_not_called()


class ThresholdEditorTests(unittest.TestCase):
    def test_editor_loads_reference_and_keeps_manual_edits(self):
        if not shutil.which('node'):self.skipTest('Node is required for the UI harness')
        result=subprocess.run(['node','tests/illumination_defaults_ui_harness.js'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
