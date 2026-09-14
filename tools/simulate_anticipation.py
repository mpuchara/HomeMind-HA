"""Offline executable exercise of the production home model, policy and intent path.

No prerecorded desired prediction and no target-state-copy rule. A trained linear
policy must emit ON while kitchen occupancy and target light are still OFF.
The identical Terrace→Living prefix has irreducible branch uncertainty: a later
Bedroom event should remove the Kitchen anticipation, and non-arrival gets a penalty.

This simulator exercises anticipation, dispatch and reward semantics rather than the
qualification threshold itself. Confidence gating has dedicated Executor tests, so the
fixture uses a deliberately permissive per-agent threshold that stays below the learned
structural confidence instead of pretending a ~0.47 live confidence satisfies 0.78.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'adaptive_ai/src'))
_temp = tempfile.TemporaryDirectory(prefix='homemind-simulator-')
os.environ.setdefault('ADAPTIVE_AI_DATA', _temp.name)
from context_engine import ContextEngine
from context import TemporalHistory
from home_state import SharedHomeStateModel
from policy import MultiHorizonPolicy
from engine import Engine
from settings import DEFAULT_OPTIONS
import engine as engine_module
import executor as executor_module


def fixture():
    now=time.time()
    states={f'binary_sensor.{a}':{'entity_id':f'binary_sensor.{a}','state':'off','attributes':{'device_class':'occupancy'}}
            for a in ('terrace','living','kitchen','bedroom')}
    states['light.kitchen']={'entity_id':'light.kitchen','state':'off','attributes':{}}
    states['sensor.lux']={'entity_id':'sensor.lux','state':'0','attributes':{'unit_of_measurement':'lx'}}
    registry={eid:{'area_id':eid.split('.')[1]} for eid in states}
    context=ContextEngine(DEFAULT_OPTIONS);context.configure(states,entities=registry)
    model=context.home
    for i in range(200):
        ts=now-50000+i*100
        for a in ('terrace','living','kitchen','bedroom'):model.observe(a,a,0,ts)
        branch='bedroom' if i%10==0 else 'kitchen'
        for j,a in enumerate(('terrace','living',branch)):model.observe(a,a,1,ts+1+j*2)
        for a in ('terrace','living','kitchen','bedroom'):model.observe(a,a,0,ts+10)
        model.expire(ts+50)
    raw=model.export()
    context.home=SharedHomeStateModel(raw=raw)
    benchmark_detail={'balanced':True,'counts':{'samples':80,'correct':80,'per_action':{
        '0':{'samples':40,'correct':40},'1':{'samples':40,'correct':40}}}}
    a=dict(id='sim',name='Kitchen',target_entity='light.kitchen',target_property='power',
           input_entities=['sensor.lux'],enabled=True,mode='shadow',training_state='qualified',
           min_value=0,max_value=1,deadband=.5,confidence_threshold=.40,action_interval=1,
           exploration_step=1,micro_exploration=False,benchmark_score=1.0,benchmark_samples=80,
           benchmark_detail=benchmark_detail)
    policy=MultiHorizonPolicy(a,states,registry,set(),context_engine=context)
    temporal=TemporalHistory()

    def example(kind,ts):
        context.home=SharedHomeStateModel(raw=raw)
        for eid,st in states.items():context.observe(eid,st,ts,learn=False)
        if kind!='empty':
            context.observe('binary_sensor.terrace',dict(states['binary_sensor.terrace'],state='on'),ts+1,learn=False)
            context.observe('binary_sensor.living',dict(states['binary_sensor.living'],state='on'),ts+3,learn=False)
        at=ts+3
        if kind=='bedroom':
            context.observe('binary_sensor.bedroom',dict(states['binary_sensor.bedroom'],state='on'),ts+5,learn=False)
            at=ts+5
        return policy.features(states,temporal,at_ts=at)[0]

    for i in range(400):
        for kind,action in (('arriving',1),('empty',0),('bedroom',0)):
            x=example(kind,now-20000+i*30)
            policy.update(1,action,x,1)
    for i in range(40):
        for kind,action in (('arriving',1),('empty',0),('bedroom',0)):
            policy.heads[1].validate(action,example(kind,now-6000+i*30),1)
    return a,states,context,policy,example


def run():
    a,states,context,policy,example=fixture()
    now=time.time()
    example('arriving',now-3)
    e=Engine();e.state_map=states;e.context=context;e.models[a['id']]=policy
    fake_store=Mock();fake_store.get_agent.return_value=a;fake_store.list_agents.return_value=[a]
    fake_store.get_agent_config.return_value=a;fake_store.list_agent_configs.return_value=[a]
    fake_store.meta_get.side_effect=lambda key,default=None:default
    fake_store.meta_set.return_value=None
    results={}
    try:
        with patch.object(engine_module,'STORE',fake_store),patch.object(executor_module,'STORE',fake_store), \
             patch.object(e.executor.handoff,'store',fake_store),patch.object(e.executor.handoff.journal,'store',fake_store), \
             patch.object(executor_module.HA,'service',return_value=[]) as service, \
             patch.object(executor_module.AUTOMATION_KNOWLEDGE,'hints_for_target',return_value=(set(),[])):
            for mode in ('shadow','control'):
                a['mode']=mode
                e.runtime.clear()
                e.process_agent(a,states,{'binary_sensor.living'})
                rt=e.runtime[a['id']]
                results[mode]={'intent':rt.get('intent'),'prediction':rt.get('last_prediction'),
                               'confidence':rt.get('last_confidence'),'support':rt.get('historical_support'),
                               'novelty':rt.get('context_novelty'),'forecast':rt.get('context_meta',{}).get('home_forecast')}
                if mode=='shadow':assert service.call_count==0
            assert results['shadow']['prediction']==1, results
            assert results['control']['confidence'] >= a['confidence_threshold'], results
            assert results['control']['intent']['status']=='ACCEPTED', results
            assert states['binary_sensor.kitchen']['state']=='off'
            assert results['control']['forecast']['occupancy_now']==0
            assert service.call_count==1
            dispatched=e.runtime[a['id']]['last_intent']['created_at']
            ack_at=dispatched+.05
            arrival_at=dispatched+2
            ack=dict(states['light.kitchen'],state='on',context={'user_id':'supervisor'})
            with patch.object(engine_module,'now_ts',return_value=ack_at):
                e.on_state_changed({'entity_id':'light.kitchen','new_state':ack})
                e.process_agent(a,e.state_map)
            arrival=dict(states['binary_sensor.kitchen'],state='on')
            with patch.object(engine_module,'now_ts',return_value=arrival_at):
                e.on_state_changed({'entity_id':'binary_sensor.kitchen','new_state':arrival})
                e.process_agent(a,e.state_map)
            results['observed_anticipation_reward']=e.runtime[a['id']].get('last_reward')
            results['reward_components']=e.runtime[a['id']].get('last_reward_components')
            assert results['reward_components']['confirmed_anticipation']>0,results
            states['binary_sensor.kitchen']=dict(states['binary_sensor.kitchen'],state='off')
            states['light.kitchen']=dict(states['light.kitchen'],state='off')
            x=example('bedroom',time.time()-5)
            chosen,confidence,*_=policy.predict(x)
            results['negative_branch']={'desired_value':chosen['value'],'confidence':confidence,
                'forecast':context.forecast('light.kitchen',time.time()),
                'non_arrival_reward':e.executor.reward_engine.evaluate(anticipated=True,observation_complete=True,observation_known=True,horizon=3).value}
            assert chosen['value']==0,results
            results['anticipated_lead_seconds']=arrival_at-dispatched
            results['ha_service_calls']=service.call_count
        return results
    finally:
        e.control_workers.shutdown();e.poll_worker.shutdown()


if __name__=='__main__':
    print(json.dumps(run(),indent=2))
