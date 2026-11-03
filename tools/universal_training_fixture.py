"""Explicit synthetic U05a evidence; NEVER a production collection adapter."""
from universal_ha_adapter import (JournalObservation, RegistryEpoch, RegistryTimeline,
                                  SemanticRegistry, CausalObservationReplay)
from universal_training_data import (HistoricalDecision, Supervision, ReplayCoverage, build_dataset)


def make_dataset(*, sensors=5, devices=1, samples=64, action_kind='binary', step_seconds=2., return_evidence=False):
    if not 1 <= sensors <= 1000 or not 1 <= devices <= 200 or not 8 <= samples <= 256:
        raise ValueError('synthetic fixture bounds')
    if not 2 <= step_seconds <= 3600:
        raise ValueError('synthetic timestamp step bounds')
    ids = tuple(['binary_sensor.motion'] + [f'binary_sensor.background{i}' for i in range(sensors-1)])
    registry = SemanticRegistry.create(entities={eid: {'area_id': 'room'} for eid in ids},
                                        revision='synthetic:r1')
    timeline = RegistryTimeline([RegistryEpoch(1., 1., registry)])
    rows = [JournalObservation('seed:'+eid, eid, 2., 3., 'off', {}) for eid in ids]
    labels, decisions = [], {}
    kinds={'binary':('light','power',(0.,1.)),
           'continuous':('climate','temperature',(5.,20.,35.)),
           'categorical':('select','option_index',(0.,1.,2.))}
    domain,prop,values=kinds[action_kind]
    for i in range(samples):
        at = float(100+step_seconds*i)
        positive = bool(i % 2)
        desired = values[-1] if positive else values[0]
        aid, target = f'agent{i%devices}', f'{domain}.fixture{i%devices}'
        rows.append(JournalObservation(f'motion:{i}', ids[0], at-1., at-.5,
                                       'on' if positive else 'off', {}))
        agent = dict(id=aid, target_entity=target, target_property=prop, min_value=values[0], max_value=values[-1])
        descriptor = dict(logical_device_id=f'synthetic-device:{i%devices}', device_property=prop,
                          physical_target_property=prop, area_id='room', autonomy_allowed=True,
                          resource_keys=[f'device:synthetic:{i%devices}'],
                          properties=[{'entity_id': target, 'property': prop}])
        mask = dict(resource_keys=descriptor['resource_keys'], manual_priority=True,
                    fallback='policy_choice_within_mask',
                    actions=[dict(value=a, requested=a, legal=True, reason=None) for a in values])
        state=({'entity_id':target,'state':'heat','attributes':{'temperature':20.}}
               if action_kind=='continuous' else
               {'entity_id':target,'state':'eco','attributes':{'options':['eco','normal','comfort']}}
               if action_kind=='categorical' else
               {'entity_id':target,'state':'off','attributes':{}})
        decisions[(aid, at)] = HistoricalDecision.capture(
            decision_ts=at, event_ts=at, received_ts=at, registry_information_ts=1.,
            registry_revision=registry.revision, agent=agent, descriptor=descriptor, legal_mask=mask,
            state=state,
            required_sensor_ids=ids, affected_sensor_ids=(),
        )
        labels.append(Supervision(f'synthetic-correct:{i}',f'synthetic-feedback:{i}',aid,'synthetic-g1',
                                  f'synthetic-episode:{i}',at,at+.25,desired,'correct',True))
    coverage=ReplayCoverage('synthetic:complete-v1',1.,100+step_seconds*samples,frozenset(ids),True)
    data = build_dataset(labels, decisions, CausalObservationReplay(rows,registry_timeline=timeline), coverage=coverage)
    if len(data.samples) != samples:
        raise AssertionError(data.rejected)
    cut = samples*2//3
    split=dict(train_until=100+step_seconds*(cut-1)+.5,holdout_from=100+step_seconds*cut,
               holdout_until=100+step_seconds*samples)
    if return_evidence:
        return data, split, dict(labels=labels,decisions=decisions,rows=rows,
                                 epochs=[RegistryEpoch(1.,1.,registry)],coverage=coverage)
    return data, split


def make_native_source(store):
    """Seed disposable native tables for lifecycle tests/benchmarks ONLY.

    Caller must supply an empty temporary Store, never a production database.
    """
    from types import SimpleNamespace
    from manual_feedback_contract import ManualFeedbackJournal
    from teaching_rl import RLTeaching
    from correct_data_foundation import _ensure_schema
    from observation_contract import FeatureJournal
    from universal_checkpoint_lifecycle import NativeDatasetSource
    import json

    journal = ManualFeedbackJournal(store)
    with store.conn() as c:
        if c.execute('SELECT COUNT(*) FROM manual_feedback_journal').fetchone()[0]:
            raise ValueError('synthetic lifecycle fixture requires empty journal')
    RLTeaching(store, SimpleNamespace()); _ensure_schema(store); FeatureJournal(store)
    _, split, evidence = make_dataset(sensors=1,samples=128,step_seconds=120.,return_evidence=True)
    for label in evidence['labels']:
        journal = ManualFeedbackJournal(store,clock=lambda l=label:l.available_ts)
        journal.record(agent_id=label.agent_id,selected_ts=label.decision_ts,source='correct',
                       correct_action=label.desired_action,feedback_id=label.feedback_id,
                       generation_id=label.generation_id,episode_id=label.episode_id)
        with store.conn() as c:
            lid=c.execute('''INSERT INTO teaching_rl_labels
                (agent_id,created_ts,sample_ts,desired,fingerprint,supervision_event_id)
                VALUES(?,?,?,?,?,?)''',(label.agent_id,label.available_ts,label.decision_ts,
                                      label.desired_action,'fixture',label.supervision_event_id)).lastrowid
        journal.link(label.feedback_id,'learning','teach_rl_label',lid)
    with store.conn() as c:
        for row in evidence['rows']:
            c.execute('''INSERT INTO feature_observation_events
                (event_key,contract_version,entity_id,event_time,received_time,state,
                 attributes_json,source,quality,protected_until) VALUES(?,1,?,?,?,?,?,'live',1,999999)''',
                      (row.event_key,row.entity_id,row.event_ts,row.received_ts,row.state,json.dumps(row.attributes)))
    source=NativeDatasetSource.create(agent_ids=['agent0'],decisions=evidence['decisions'],
                                     epochs=evidence['epochs'],coverage=evidence['coverage'],
                                     as_of_ts=evidence['coverage'].complete_through_ts,evidence='synthetic')
    return source,split


def make_collected_source(store):
    """Disposable synthetic stream through the native collector, with original clocks.

    Uses the supported explicit V1 decision receipt API; coverage is newly collected
    V3, never an upgrade of an older coverage proof. No physical HA claim.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from manual_feedback_contract import ManualFeedbackJournal
    from teaching_rl import RLTeaching
    from correct_data_foundation import _ensure_schema
    from observation_contract import FeatureJournal
    from provenance import ProvenanceJournal
    from universal_history_collector import HistoricalTrainingCollector, source_from_collection
    from dataclasses import replace
    import json

    now=[2.]
    clock=lambda:now[0]
    feedback=ManualFeedbackJournal(store,clock=clock)
    RLTeaching(store,SimpleNamespace());_ensure_schema(store)
    journal=FeatureJournal(store,clock=clock,retention_hours=1000000)
    collector=HistoricalTrainingCollector(store,journal,clock=clock)
    provenance=ProvenanceJournal(store)
    collector.connection(True)
    ids=('binary_sensor.motion','sensor.unrelated')
    collector.registry('entity',[dict(entity_id=eid,area_id='room') for eid in ids])
    collector.registry('device',[]);collector.registry('area',[dict(area_id='room')])
    def drain():
        while collector.pending or collector.pending_gap is not None:
            if not collector.drain():raise AssertionError(collector.diagnostics())
    drain();now[0]=20.
    collector.snapshot({eid:dict(entity_id=eid,state='off',attributes={},
        last_updated=datetime.fromtimestamp(10.,timezone.utc).isoformat()) for eid in ids},
        requested_ts=19.,received_ts=20.,epoch=collector.epoch)
    collector.barrier();drain()
    _,split,evidence=make_dataset(sensors=1,samples=128,step_seconds=120.,return_evidence=True)
    for i,label in enumerate(evidence['labels']):
        at=label.decision_ts
        row=evidence['rows'][i+1]
        now[0]=row.received_ts
        collector.event(row.entity_id,dict(entity_id=row.entity_id,state=row.state,attributes={}),
                        event_ts=row.event_ts,received_ts=row.received_ts)
        now[0]=at
        receipt=replace(evidence['decisions'][(label.agent_id,at)],
                        registry_revision=collector.topology,registry_information_ts=2.)
        agent=json.loads(receipt.payload_json)['agent']
        intent=SimpleNamespace(intent_id=f'collected:{i}',agent_id=label.agent_id,
            target_entity=agent['target_entity'],target_property=agent['target_property'],
            created_at=at,desired_value=label.desired_action,policy_version=12,model_revision='fixture',decision_source='policy')
        provenance.record_decision(decision_id=intent.intent_id,created_time=at,agent_id=label.agent_id,
            generation_id=label.generation_id,model_version=12,model_revision='fixture')
        collector.decision(intent,agent,dict(entity_id=intent.target_entity,state='off',attributes={}),
            required=ids[:1],affected=(),receipt=receipt)
        now[0]=label.available_ts
        feedback.record(agent_id=label.agent_id,selected_ts=at,source='correct',
            correct_action=label.desired_action,feedback_id=label.feedback_id,
            generation_id=label.generation_id,episode_id=label.episode_id)
        collector.barrier();drain()
    with store.conn() as c:
        c.execute('BEGIN')
        source=source_from_collection(c,['agent0'],start=20.,through=now[0])
        data=source.dataset(c)
        if len(data.samples)!=128:raise AssertionError(data.rejected)
    return source,split
