"""Disposable SYNTHETIC producer; never imported/installed by production runtime.

Runs real Engine/DeviceAgent/predecision/collector/provenance/Correct interfaces.
It never constructs HistoricalDecision or edits persisted clocks. Protocol units
are independent simulated trials by scenario assumption, not proof about a home.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch


class ProducerClock:
    def __init__(self, start=1800000000.):self.value=float(start)
    def __call__(self):return self.value
    def set(self, value):
        value=float(value)
        if value<self.value:raise ValueError('producer clock cannot move backwards')
        self.value=value


def state(entity, value, stamp, **attributes):
    return dict(entity_id=entity,state=str(value),attributes=attributes,
        last_changed=datetime.fromtimestamp(stamp,timezone.utc).isoformat(),
        last_updated=datetime.fromtimestamp(stamp,timezone.utc).isoformat())


class OriginalEvidenceScenario:
    def __init__(self, store, *, sensors=5, devices=1, clock=None):
        if not 5<=sensors<=1000 or not 1<=devices<=200:
            raise ValueError('bounded synthetic scenario required')
        self.store=store;self.clock=clock or ProducerClock();self.stack=ExitStack()
        self.intents=[];self.labels=[];self.frames=[];self.closed=False
        try:self._initialize(sensors,devices)
        except BaseException:self.close();raise

    def _initialize(self,sensors,devices):
        import engine,executor,history
        from settings import OPTIONS
        from observation_contract import FeatureJournal
        from universal_history_collector import HistoricalTrainingCollector
        from universal_predecision import PredecisionEvidence
        from universal_affected_sensors import AffectedSensorDeclarations
        from agent_candidate_lineage import ensure_lineage_tables,_ensure_root
        from manual_feedback_contract import ManualFeedbackJournal
        from teaching_rl import RLTeaching
        from correct_data_foundation import _ensure_schema
        from provenance_runtime import install

        with self.store.conn() as c:
            for table in ('agents','manual_feedback_journal','feature_observation_events','universal_collection_decisions'):
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    if c.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                        raise ValueError('synthetic producer requires an empty disposable Store')
        self.stack.enter_context(patch('time.time',self.clock))
        for module in (engine,executor,history):self.stack.enter_context(patch.object(module,'STORE',self.store))
        # This sparse offline producer has no interactive user competing for CPU.
        # Production defaults and composed-runtime benchmark options are unchanged.
        self.stack.enter_context(patch.dict(OPTIONS,training_realtime_event_priority_seconds=0.0))
        self.ha=self.stack.enter_context(patch.object(executor.HA,'service',side_effect=AssertionError('offline HA call')))
        self.stack.enter_context(patch.object(executor.AUTOMATION_KNOWLEDGE,'hints_for_target',return_value=(set(),[])))
        self.feedback=ManualFeedbackJournal(self.store,clock=self.clock)
        RLTeaching(self.store,SimpleNamespace());_ensure_schema(self.store);ensure_lineage_tables(self.store)
        self.engine=engine.Engine()
        self.sensor_ids=['binary_sensor.motion','sensor.effect','sensor.lux','sensor.power','sensor.outdoor']
        self.sensor_ids += [f'sensor.background_{i}' for i in range(sensors-5)]
        self.targets=[f'light.fixture_{i}' for i in range(devices)]
        self.entities=[dict(entity_id=eid,device_id='measurements') for eid in self.sensor_ids]
        self.entities += [dict(entity_id=eid,device_id=f'light-device-{i}') for i,eid in enumerate(self.targets)]
        self.devices=[dict(id='measurements',area_id='kitchen')]+[
            dict(id=f'light-device-{i}',area_id='kitchen') for i in range(devices)]
        self.areas=[dict(area_id='kitchen',name='Synthetic Kitchen')]
        now=self.clock()
        self.engine.state_map={eid:state(eid,'off' if eid.startswith(('light.','binary_sensor.')) else '12',now,
            **({'device_class':'motion'} if eid=='binary_sensor.motion' else
               {'device_class':'illuminance'} if eid=='sensor.lux' else
               {'device_class':'power'} if eid=='sensor.power' else
               {'device_class':'temperature'} if eid.startswith('sensor.') else {})) for eid in self.sensor_ids+self.targets}
        self.engine.update_entity_registry(self.entities);self.engine.update_device_registry(self.devices)
        self.engine.update_area_registry(self.areas)
        self.agents=[]
        for i,target in enumerate(self.targets):
            created=self.store.create_agent(dict(name=f'Synthetic light {i}',target_entity=target,target_property='power',
                min_value=0,max_value=1,deadband=.5,input_entities=['binary_sensor.motion'],enabled=True,
                confidence_threshold=.78,action_interval=.25,micro_exploration=False,exploration_step=1,mode='shadow'))
            self.store.update_agent(created['id'],{'mode':'shadow'})
            _ensure_root(self.store,created['id'],generation_number=0)
        self.engine.executor.device_agents.migrate_agent_identities()
        # The workload is defined by targets, not wall-clock creation order.
        # Store ordering uses real ISO timestamps even when the producer clock
        # is controlled. Clock corrections must not rotate which roots receive
        # each ingress pulse or which roots receive the Candidate fixtures.
        by_target={a['target_entity']:a for a in self.store.list_agent_configs()}
        self.agents=[by_target[target] for target in self.targets]
        self.engine.agent_configs={a['id']:a for a in self.agents}
        self.engine.all_agent_configs=dict(self.engine.agent_configs)
        for agent in self.agents:self.engine.policy(agent)
        self.journal=FeatureJournal(self.store,clock=self.clock,retention_hours=1000000,max_events_per_entity=100000)
        self.collector=HistoricalTrainingCollector(self.store,self.journal,clock=self.clock)
        self.evidence=PredecisionEvidence(self.engine,self.store,self.collector)
        self.collector.predecision=self.evidence;self.engine.universal_training_collector=self.collector
        self.engine.executor.device_agents.universal_training_collector=self.collector
        install(SimpleNamespace(ENGINE=self.engine,STORE=self.store))
        self.collector.connection(True)
        for kind,rows,callback in [('entity',self.entities,self.engine.update_entity_registry),
                ('device',self.devices,self.engine.update_device_registry),('area',self.areas,self.engine.update_area_registry)]:
            self.collector.apply_registry(kind,callback,rows,self.clock(),True,self.collector.epoch)
        self.drain()
        self.clock.set(now+1)
        requested=self.clock();self.clock.set(now+1.1)
        self.collector.snapshot(self.engine.state_map,requested_ts=requested,received_ts=self.clock(),epoch=self.collector.epoch)
        self.collector.barrier();self.drain();self.start=self.clock()
        self.api=AffectedSensorDeclarations(self.store)
        for agent in self.agents:self.declare(agent)
        self.episode_start=now+100

    def declare(self,agent,**changes):
        args=dict(logical_device_id=self.engine.executor.device_agents.descriptor(agent)['logical_device_id'],
            topology_revision=self.collector.topology,affected_sensor_ids=['sensor.effect','sensor.lux','sensor.power'],
            complete=True,approved_by='synthetic scenario operator',
            rationale='Explicit closed simulated-world dependency assessment; no physical home claim')
        args.update(changes)
        return self.api.declare(agent['id'],**args)

    def drain(self):
        while self.collector.pending or self.collector.pending_gap is not None:
            if not self.collector.drain():raise AssertionError(self.collector.diagnostics())

    def episode(self,index,*,desired=None,source='correct',episode_id=None,before_predict=None):
        # Separate exogenous trials with reset context by scenario design. The
        # 120-second spacing alone is NOT evidence of real-world independence.
        at=self.episode_start+120*index
        action=float((index//len(self.agents)+index%len(self.agents))%2 if desired is None else desired)
        agent=self.agents[index%len(self.agents)]
        self.clock.set(at)
        observed=state('binary_sensor.motion','on' if action else 'off',at,device_class='motion')
        self.clock.set(at+.1)
        self.engine.on_state_changed(dict(entity_id='binary_sensor.motion',new_state=observed,_hm_ws_received_ts=self.clock()))
        self.drain()
        self.clock.set(at+.2);self.evidence.request(agent['id']);self.evidence.prepare_one()
        if before_predict is not None:before_predict(self,agent)
        self.clock.set(at+.3)
        result=self.engine.process_agent(agent,self.engine.state_map,{'binary_sensor.motion'})
        intent=self.engine.runtime[agent['id']].get('last_intent')
        if not intent:raise AssertionError('real Engine did not produce ActionIntent')
        self.store._flush_all_provenance()
        self.collector.barrier();self.drain()
        self.clock.set(at+.5)
        fid=f'e2e-correct:{index}'
        self.feedback.record(agent_id=agent['id'],selected_ts=intent['created_at'],source=source,
            correct_action=action,feedback_id=fid,decision_id=intent['intent_id'],
            generation_id=self.evidence.prepared[agent['id']]['generation_id'],
            episode_id=episode_id or f'synthetic-independent-trial:{index}')
        self.clock.set(at+.6)
        # A later measurement is never moved into the decision's past.
        effect=state('sensor.lux',str(1000 if action else 5),self.clock(),device_class='illuminance')
        self.engine.on_state_changed(dict(entity_id='sensor.lux',new_state=effect,_hm_ws_received_ts=self.clock()))
        self.collector.barrier();self.drain()
        self.intents.append(intent);self.labels.append(fid)
        self.frames.append(dict(index=index,at=intent['created_at'],agent_id=agent['id'],desired=action,
                                episode_id=episode_id or f'synthetic-independent-trial:{index}',status=result['status']))
        return intent

    def collect_events(self,samples=128):
        if not 8<=samples<=2048 or samples<len(self.agents):raise ValueError('bounded complete device coverage required')
        for i in range(samples):self.episode(i)
        self.store._flush_all_provenance();self.collector.barrier();self.drain()

    def collect(self,samples=128):
        self.collect_events(samples)
        return self.source()

    def source(self):
        from universal_history_collector import source_from_collection
        with self.store.conn() as c:
            c.execute('BEGIN')
            return source_from_collection(c,[a['id'] for a in self.agents],start=self.start,through=self.clock())

    def split(self):
        cut=len(self.frames)*2//3
        return dict(train_until=self.frames[cut-1]['at']+.6,holdout_from=self.frames[cut]['at'],
                    holdout_until=self.frames[-1]['at']+.6)

    def close(self):
        if self.closed:return
        self.closed=True
        engine=getattr(self,'engine',None)
        if engine is not None:
            engine.stop_event.set()
            event=getattr(engine,'provenance_writer_event',None)
            if event is not None:event.set()
            thread=getattr(engine,'provenance_writer_thread',None)
            if thread is not None:thread.join(timeout=5)
            for name in ('control_workers','poll_worker','registry_worker','housekeeping_worker'):
                worker=getattr(engine,name,None)
                if worker is not None:worker.shutdown(wait=True,cancel_futures=True)
        self.stack.close()

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
