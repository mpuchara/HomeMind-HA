"""Offline HA transport/Recorder contract experiments. NEVER imported by runtime.

Compact deltas can maintain current RAM state, but do not carry the original
state_changed event envelope or a durable completeness/receipt-time witness.
This prototype never yields HistoricalDecision, ReplayCoverage or AI labels.
"""
import copy
import hashlib
import json
import math
import time


class CompactRuntimeCache:
    history_complete=False
    evidence_eligible=False
    def __init__(self):self.states={};self.session=None;self.ready=False;self.gaps=[]
    def begin(self,session):
        self.states.clear();self.session=session;self.ready=False
    def disconnect(self,reason='disconnect'):
        self.gaps.append(reason);self.states.clear();self.ready=False
    @staticmethod
    def state(eid,value):
        if not isinstance(value,dict) or not {'s','a','c','lc'}<=value.keys():raise ValueError('incomplete compact initial state')
        if not isinstance(eid,str) or '.' not in eid or not isinstance(value['s'],str):raise ValueError('invalid entity/state')
        if not all(type(value[k]) in (int,float) and math.isfinite(value[k]) for k in ('lc','lu') if k in value):
            raise ValueError('invalid state clocks')
        if not isinstance(value['a'],dict):raise ValueError('invalid attributes')
        context=value['c']
        if isinstance(context,str):context={'id':context}  # No invented parent/user information.
        if not isinstance(context,dict):raise ValueError('invalid context')
        return dict(entity_id=eid,state=value['s'],attributes=copy.deepcopy(value['a']),context=copy.deepcopy(context),
                    last_changed=value['lc'],last_updated=value.get('lu',value['lc']))
    def apply(self,message,*,session,initial=False):
        if session!=self.session:raise ValueError('stale session')
        try:
            if len(json.dumps(message))>4*1024*1024:raise ValueError('message byte limit')
            if not isinstance(message,dict) or set(message)-{'a','c','r'}:raise ValueError('unknown compact fields')
            if initial:
                if set(message)-{'a'}:raise ValueError('initial snapshot must contain additions only')
                additions=message.get('a',{})
                if not isinstance(additions,dict) or len(additions)>4096:raise ValueError('entity bound')
                fresh={eid:self.state(eid,value) for eid,value in additions.items()}
                self.states=fresh;self.ready=True;return set(fresh)
            if not self.ready:raise ValueError('fresh initial snapshot required')
            if (not isinstance(message.get('a',{}),dict) or not isinstance(message.get('c',{}),dict)
                    or not isinstance(message.get('r',[]),list)):
                raise ValueError('invalid update groups')
            staged={eid:self.state(eid,value) for eid,value in message.get('a',{}).items()}
            removed=set(message.get('r',[]));changed=set(staged)|removed
            if removed & staged.keys():raise ValueError('ambiguous update')
            for eid,diff in message.get('c',{}).items():
                if eid not in self.states or eid in changed:raise ValueError('unknown or ambiguous delta base')
                if not isinstance(diff,dict) or set(diff)-{'+','-'}:raise ValueError('invalid delta')
                add=diff.get('+',{});sub=diff.get('-',{})
                if not isinstance(add,dict) or not isinstance(sub,dict):raise ValueError('invalid delta groups')
                if set(add)-{'s','a','c','lc','lu'} or set(sub)-{'a'}:raise ValueError('unknown delta fields')
                if 's' in add and not isinstance(add['s'],str):raise ValueError('invalid state')
                if 'a' in add and not isinstance(add['a'],dict):raise ValueError('invalid attributes')
                if 'a' in sub and (not isinstance(sub['a'],list) or not all(isinstance(k,str) for k in sub['a'])):
                    raise ValueError('invalid attribute removals')
                if not all(type(add[k]) in (int,float) and math.isfinite(add[k]) for k in ('lc','lu') if k in add):
                    raise ValueError('invalid clocks')
                value=copy.deepcopy(self.states[eid])
                if 's' in add:value['state']=add['s']
                if 'a' in add:value['attributes'].update(add['a'])
                for key in sub.get('a',[]):value['attributes'].pop(key,None)
                if 'c' in add:
                    context=add['c'];value['context'].update({'id':context} if isinstance(context,str) else context)
                if 'lc' in add:value['last_changed']=value['last_updated']=add['lc']
                elif 'lu' in add:value['last_updated']=add['lu']
                staged[eid]=value;changed.add(eid)
            if len((self.states.keys()|staged.keys())-removed)>4096:raise ValueError('entity bound')
            for eid in removed:self.states.pop(eid,None)
            self.states.update(staged)
            return changed
        except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError) as exc:
            self.disconnect('invalid_compact_update')
            raise ValueError('compact stream invalid; resync required') from exc


def recorder_assessment(rows,requested_entities):
    """Read-only inspection, intentionally cannot certify missing transitions."""
    seen=set();attributes=contexts=clocks=0
    for group in rows or ():
        for row in group:
            if row.get('entity_id'):seen.add(row['entity_id'])
            attributes+=int('attributes' in row);contexts+=int('context' in row)
            clocks+=int('last_updated' in row and 'last_changed' in row)
    return dict(rows=sum(len(g) for g in rows or ()),entities=sorted(seen),
        missing_requested_entities=sorted(set(requested_entities)-seen),rows_with_attributes=attributes,
        rows_with_context=contexts,rows_with_both_state_clocks=clocks,
        original_local_received_times=False,history_complete=False,evidence_eligible=False,
        reason='Recorder responses do not prove inclusion/retention/WS continuity or original local receipt clocks')


def transport_case(sensors,devices,events=400):
    """Wire/decoder microbenchmark on one frozen synthetic message corpus.

    No HA server, network, inference, Recorder IO or physical state is measured.
    Filter uses a declared synthetic dependency set, not guessed live needs.
    """
    ids=[f'sensor.input_{i}' for i in range(sensors)]+[f'light.target_{i}' for i in range(devices)]
    wanted={ids[0]}|{eid for eid in ids if eid.startswith('light.')}
    full={eid:dict(entity_id=eid,state='0',attributes={'unit_of_measurement':'x','label':'synthetic'},
        context={'id':'initial','parent_id':None,'user_id':None},last_changed=1000.,last_updated=1000.) for eid in ids}
    compact=lambda st:dict(s=st['state'],a=st['attributes'],c=st['context'],lc=st['last_changed'],lu=st['last_updated'])
    initial={'a':{eid:compact(st) for eid,st in full.items() if eid in wanted}}
    original=[];deltas=[];expected=copy.deepcopy(full)
    for i in range(events):
        # Ten percent of updates reach the selected predictor, others rotate background sensors.
        eid=ids[0] if i%10==0 else ids[i%sensors]
        old=copy.deepcopy(expected[eid]);new=copy.deepcopy(old)
        new.update(state=str(i+1),last_changed=1001.+i,last_updated=1001.+i)
        new['context']['id']=f'event-{i}'
        original.append(dict(type='event',id=2,event=dict(event_type='state_changed',time_fired=1001.+i,
            origin='LOCAL',context=new['context'],data=dict(entity_id=eid,old_state=old,new_state=new))))
        if eid in wanted:deltas.append(dict(type='event',id=3,event={'c':{eid:{'+':{'s':new['state'],'lc':1001.+i,'c':new['context']['id']}}}}))
        expected[eid]=new
    initial_a=json.dumps(list(full.values()),separators=(',',':')).encode()
    wire_a=[json.dumps(e,separators=(',',':')).encode() for e in original]
    wire_b=[json.dumps(dict(type='event',id=3,event=initial),separators=(',',':')).encode()]+[
        json.dumps(e,separators=(',',':')).encode() for e in deltas]
    started=time.perf_counter();cpu=time.process_time();cache={row['entity_id']:row for row in json.loads(initial_a)}
    for raw in wire_a:
        data=json.loads(raw)['event']['data'];cache[data['entity_id']]=data['new_state']
    a=dict(messages=1+len(wire_a),bytes=len(initial_a)+sum(map(len,wire_a)),cpu_ms=(time.process_time()-cpu)*1000,
           wall_ms=(time.perf_counter()-started)*1000,processed_entity_updates=len(full)+len(wire_a))
    started=time.perf_counter();cpu=time.process_time();runtime=CompactRuntimeCache();runtime.begin('one')
    for index,raw in enumerate(wire_b):runtime.apply(json.loads(raw)['event'],session='one',initial=index==0)
    b=dict(messages=len(wire_b),bytes=sum(map(len,wire_b)),cpu_ms=(time.process_time()-cpu)*1000,
           wall_ms=(time.perf_counter()-started)*1000,processed_entity_updates=len(wanted)+len(deltas))
    assert runtime.states=={eid:expected[eid] for eid in wanted}
    digest=lambda rows:hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
    return dict(sensors=sensors,devices=devices,events=events,synthetic_selected_entities=len(wanted),
        variant_a_events=a,variant_b_filtered_compact=b,
        variant_c_dual_stream=dict(messages=a['messages']+b['messages'],bytes=a['bytes']+b['bytes'],
            decode_cpu_sum_ms=a['cpu_ms']+b['cpu_ms'],note='logical dual subscription, not two sockets; summed decoder costs only'),
        equivalent_selected_runtime_state=True,state_sha256=digest(runtime.states),
        original_event_envelope_preserved_by_compact=False,history_complete=False,evidence_eligible=False,
        ha_server_cpu_ms=None,network_ms=None,event_to_intent_ms=None,rest_resync_ms=None,recorder_io_ms=None,
        limits='Decoder microbenchmark including compact initial snapshot, same generated changes. No end-to-end speedup claim; unknown live subscription dependency set and HA version require validation.')
