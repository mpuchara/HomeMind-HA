import unittest
import ast
import time
from concurrent.futures import Future
from dataclasses import replace, FrozenInstanceError
from unittest.mock import patch, Mock
from support import *
import engine as engine_module
import executor as executor_module
import history as history_module
import qualification as qualification_module
from storage import Store
from engine import Engine
from intent import ActionIntent
from policy import MultiHorizonPolicy
from settings import OPTIONS


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'test.db')
        self.patches=[patch.object(module,'STORE',self.store) for module in (engine_module,executor_module,history_module)]
        for p in self.patches:p.start()
        self.e=Engine()
        self.a=self.store.create_agent(agent())
        detail={'balanced':True,'counts':{'samples':80,'correct':80,'per_action':{
            '0':{'samples':40,'correct':40},'1':{'samples':40,'correct':40}}}}
        self.store.set_training_state(self.a['id'],'qualified',score=1.0,samples=80,detail=detail)
        self.store.update_agent(self.a['id'],{'mode':'control'})
        self.a=self.store.get_agent(self.a['id'])
        self.e.state_map={self.a['target_entity']:state(self.a['target_entity'])}
        self.e.context.configure(self.e.state_map)
        self.model=self.e.policy(self.a)
        self.service=patch.object(executor_module.HA,'service',return_value=[]).start()
        self.hints=patch.object(executor_module.AUTOMATION_KNOWLEDGE,'hints_for_target',return_value=(set(),[])).start()
        self.extra=[self.service,self.hints]

    def tearDown(self):
        patch.stopall()
        self.e.control_workers.shutdown();self.e.poll_worker.shutdown()
        self.temp.cleanup()

    def intent(self, **changes):
        return ActionIntent.create(agent_id=self.a['id'],target_entity=self.a['target_entity'],target_property='power',
            desired_value=1,confidence=.95,support=.8,novelty=.1,prediction_horizon=3,
            created_at=time.time(),ttl=2,policy_version=self.model.VERSION,model_revision=self.model.model_revision,
            context_revision=self.e.context.home.revision,target_revision=0,reason='test',
            context_dependencies=()) if not changes else replace(self.intent(),**changes)

    def submit(self, **changes):
        return self.e.executor.submit(self.intent(**changes),{0:1},1)

    def test_control_dispatch(self):
        self.assertEqual(self.submit()['status'],'ACCEPTED')
        self.service.assert_called_once_with('light','turn_on',{'entity_id':'light.kitchen'})

    def test_partial_scan_does_not_block_control_transition_http(self):
        import main
        self.store.update_agent(self.a['id'], {'mode':'shadow'})
        handler=main.Handler.__new__(main.Handler)
        handler.path='/api/agents/'+self.a['id']
        handler.read_json=lambda:{'mode':'control'}
        handler.send_json=Mock()
        with patch.object(main,'STORE',self.store), patch.object(main,'ENGINE',self.e), \
             patch.object(main,'runtime_available',return_value=True), \
             patch.object(main,'assess_control_qualification',qualification_module.assess_control_qualification), \
             patch.object(self.e,'refresh_states'), \
             patch.object(executor_module.AUTOMATION_KNOWLEDGE,'scan'), \
             patch.object(executor_module.AUTOMATION_KNOWLEDGE,'error','7 automation configs unavailable'):
            handler.do_PATCH()
        self.assertEqual(handler.send_json.call_args.args[0],200)
        self.assertEqual(self.store.get_agent(self.a['id'])['mode'],'control')
        self.service.assert_not_called()

    def test_partial_scan_still_disables_known_target_automation(self):
        self.hints.return_value=(set(),[{'entity_id':'automation.stairs','enabled':True,'config_status':'cached'}])
        self.e.state_map['automation.stairs']=state('automation.stairs','on')
        def refresh():
            if self.service.called:self.e.state_map['automation.stairs']['state']='off'
        with patch.object(self.e,'refresh_states',side_effect=refresh), \
             patch.object(executor_module.AUTOMATION_KNOWLEDGE,'scan'), \
             patch.object(executor_module.AUTOMATION_KNOWLEDGE,'error','7 automation configs unavailable'):
            self.assertEqual(self.e.executor.take_control(self.a,refresh=True),['automation.stairs'])
        self.service.assert_called_once_with('automation','turn_off',{'entity_id':'automation.stairs','stop_actions':True})

    def test_partial_scan_does_not_bypass_failed_off_confirmation(self):
        self.hints.return_value=(set(),[{'entity_id':'automation.stairs','enabled':True}])
        self.e.state_map['automation.stairs']=state('automation.stairs','on')
        with patch.object(self.e,'refresh_states'), \
             patch.object(executor_module.AUTOMATION_KNOWLEDGE,'scan'), \
             patch.object(executor_module.AUTOMATION_KNOWLEDGE,'error','7 configs unavailable'):
            with self.assertRaisesRegex(RuntimeError,'OFF not confirmed'):
                self.e.executor.take_control(self.a,refresh=True)
        self.assertEqual(self.service.call_args_list[-1].args,('automation','turn_on',{'entity_id':'automation.stairs'}))

    def test_shadow_has_no_service(self):
        self.store.update_agent(self.a['id'],{'mode':'shadow'})
        self.assertEqual(self.submit()['status'],'SHADOW');self.service.assert_not_called()

    def test_shadow_verify_has_no_service(self):
        self.store.update_agent(self.a['id'],{'mode':'shadow'})
        with self.assertRaises(ValueError):self.e.executor.verify(self.a)
        self.service.assert_not_called()

    def test_expired(self):
        self.assertEqual(self.submit(created_at=time.time()-3)['status'],'EXPIRED');self.service.assert_not_called()

    def test_stale_model(self):
        self.assertTrue(self.submit(model_revision='old')['reason'].startswith('model:'));self.service.assert_not_called()

    def test_stale_context(self):
        self.assertTrue(self.submit(context_revision=100)['reason'].startswith('context:'))

    def test_stale_selected_entity(self):
        self.assertTrue(self.submit(context_dependencies=(('binary_sensor.motion',1),))['reason'].startswith('context:'))

    def test_stale_target(self):
        self.assertTrue(self.submit(target_revision=100)['reason'].startswith('state:'))

    def test_unqualified(self):
        self.store.set_training_state(self.a['id'],'needs_retrain')
        self.assertTrue(self.submit()['reason'].startswith('qualification:'));self.service.assert_not_called()

    def test_paused(self):
        self.store.update_agent(self.a['id'],{'mode':'paused'})
        self.assertTrue(self.submit()['reason'].startswith('paused:'))

    def test_disabled(self):
        self.store.update_agent(self.a['id'],{'enabled':False})
        self.assertTrue(self.submit()['reason'].startswith('disabled:'));self.service.assert_not_called()

    def test_confidence(self):
        self.assertTrue(self.submit(confidence=.5)['reason'].startswith('confidence:'))

    def test_support(self):
        self.assertTrue(self.submit(support=0)['reason'].startswith('support:'))

    def test_novelty(self):
        self.assertTrue(self.submit(novelty=1)['reason'].startswith('novelty:'))

    def test_manual(self):
        self.e.runtime[self.a['id']]={'manual_override_until':time.time()+100}
        self.assertTrue(self.submit()['reason'].startswith('manual:'))

    def test_cooldown(self):
        self.e.runtime[self.a['id']]={'last_ai_ts':time.time()}
        self.assertTrue(self.submit()['reason'].startswith('cooldown:'))

    def test_pending_ack(self):
        self.e.runtime[self.a['id']]={'pending':{'action_value':1,'acknowledged_ts':None}}
        self.assertTrue(self.submit()['reason'].startswith('acknowledgement:'))

    def test_settling(self):
        self.e.runtime[self.a['id']]={'pending':{'action_value':1,'acknowledged_ts':time.time()}}
        self.assertTrue(self.submit()['reason'].startswith('acknowledgement:'))

    def test_duplicate_value(self):
        self.e.state_map['light.kitchen']['state']='on'
        self.assertTrue(self.submit()['reason'].startswith('duplicate:'))

    def test_unavailable(self):
        self.e.state_map['light.kitchen']['state']='unavailable'
        self.assertTrue(self.submit()['reason'].startswith('unavailable:'))

    def test_ownership_conflict(self):
        other=self.store.create_agent(agent(name='Other'))
        self.store.set_training_state(other['id'],'qualified');self.store.update_agent(other['id'],{'mode':'control'})
        self.assertTrue(self.submit()['reason'].startswith('takeover:'));self.service.assert_not_called()

    def test_service_failure_not_acknowledged(self):
        self.service.side_effect=TimeoutError('offline')
        self.assertTrue(self.submit()['reason'].startswith('service:'))
        self.assertFalse(self.e.runtime[self.a['id']].get('pending'))

    def test_automation_takeover_confirmed(self):
        self.hints.return_value=(set(),[{'entity_id':'automation.stairs','enabled':True}])
        self.e.state_map['automation.stairs']=state('automation.stairs','on')
        def refresh():self.e.state_map['automation.stairs']['state']='off'
        with patch.object(self.e,'refresh_states',side_effect=refresh):
            result=self.submit()
        self.assertTrue(result['reason'].startswith('takeover:'))
        self.service.assert_called_once_with('automation','turn_off',{'entity_id':'automation.stairs','stop_actions':True})

    def test_automation_failure_blocks_device_and_rolls_back(self):
        self.hints.return_value=(set(),[{'entity_id':'automation.stairs','enabled':True}])
        self.e.state_map['automation.stairs']=state('automation.stairs','on')
        with patch.object(self.e,'refresh_states'):
            self.assertEqual(self.submit()['status'],'REJECTED')
        self.assertEqual(self.service.call_count,2)
        self.assertEqual(self.service.call_args_list[0].args,('automation','turn_off',{'entity_id':'automation.stairs','stop_actions':True}))
        self.assertEqual(self.service.call_args_list[1].args,('automation','turn_on',{'entity_id':'automation.stairs'}))

    def test_own_rest_echo_with_user_id_is_not_manual(self):
        self.e.record_command(self.a,1)
        self.e.runtime[self.a['id']]={'previous_target':0,'last_inference_ts':time.time()}
        st=state('light.kitchen','on');st['context']={'user_id':'supervisor'}
        self.e.state_map['light.kitchen']=st
        self.e.process_agent(self.a,self.e.state_map)
        self.assertEqual(self.e.runtime[self.a['id']]['manual_override_until'],0)

    def test_explicit_manual_correction_sets_hold(self):
        self.e.runtime[self.a['id']]={'previous_target':1,'last_inference_ts':time.time()}
        st=state('light.kitchen','off');st['context']={'user_id':'human'}
        self.e.state_map['light.kitchen']=st
        self.e.process_agent(self.a,self.e.state_map)
        self.assertGreater(self.e.runtime[self.a['id']]['manual_override_until'],time.time())

    def test_real_event_does_not_wait_for_periodic_throttle(self):
        self.e.runtime[self.a['id']]={'last_inference_ts':time.time()}
        self.e.process_agent(self.a,self.e.state_map,{'light.kitchen'})
        self.assertIn('last_intent',self.e.runtime[self.a['id']])

    def test_event_during_inflight_is_retried_without_busy_loop(self):
        future=Future();self.e.in_flight['light.kitchen']=future
        self.e.wake_event.clear()
        self.e.process(self.e.state_map,{'light.kitchen'})
        self.assertFalse(self.e.wake_event.is_set())
        future.set_result(None)
        self.assertTrue(self.e.wake_event.is_set())
        self.assertIn('light.kitchen',self.e.dirty_entities)

    def test_replaced_anticipation_still_gets_false_positive_feedback(self):
        ts=time.time()
        self.e.context.home.observe('motion','kitchen',0,ts-20)
        self.e.runtime[self.a['id']]={'last_inference_ts':ts,'outcomes':[
            {'action_index':1,'action_value':1,'policy_head':1,'horizon':3,'features':{0:1},
             'started_ts':ts-10,'ended_ts':ts-9,'acknowledged_ts':ts-9.9,'area_id':'kitchen','anticipated':True}]}
        self.e.process_agent(self.a,self.e.state_map)
        rt=self.e.runtime[self.a['id']]
        self.assertEqual(rt['last_reward_components']['false_positive'],-.6)
        self.assertEqual(rt['outcomes'],[])


class ContractTests(unittest.TestCase):
    def test_only_executor_calls_services(self):
        calls=[]
        for p in (ROOT/'adaptive_ai/src').glob('*.py'):
            tree=ast.parse(p.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='service':
                    calls.append(p.name)
        self.assertEqual(calls,['executor.py'])

    def test_policy_has_no_ha_dependency(self):
        text=(ROOT/'adaptive_ai/src/policy.py').read_text(encoding='utf-8')
        self.assertNotIn('from ha import',text)
        self.assertNotIn('HA.service',text)

    def test_intent_is_frozen_and_rejects_nan(self):
        fields=dict(agent_id='a',target_entity='light.a',target_property='power',desired_value=1,confidence=1,support=1,novelty=0,prediction_horizon=1,created_at=1,ttl=1,policy_version=10,model_revision='r',context_revision=0,target_revision=0,reason='x')
        i=ActionIntent.create(**fields)
        with self.assertRaises(FrozenInstanceError):i.desired_value=0
        with self.assertRaises(ValueError):ActionIntent.create(**(fields|{'desired_value':float('nan')}))


if __name__=='__main__':unittest.main()
