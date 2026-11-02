"""Explicit synthetic U05a evidence; NEVER a production collection adapter."""
from universal_ha_adapter import (JournalObservation, RegistryEpoch, RegistryTimeline,
                                  SemanticRegistry, CausalObservationReplay)
from universal_training_data import (HistoricalDecision, Supervision, ReplayCoverage, build_dataset)


def make_dataset(*, sensors=5, devices=1, samples=64, action_kind='binary', step_seconds=2.):
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
    data = build_dataset(labels, decisions, CausalObservationReplay(rows,registry_timeline=timeline),
                         coverage=ReplayCoverage('synthetic:complete-v1',1.,100+step_seconds*samples,frozenset(ids),True))
    if len(data.samples) != samples:
        raise AssertionError(data.rejected)
    cut = samples*2//3
    return data, dict(train_until=100+step_seconds*(cut-1)+.5,holdout_from=100+step_seconds*cut,
                       holdout_until=100+step_seconds*samples)
