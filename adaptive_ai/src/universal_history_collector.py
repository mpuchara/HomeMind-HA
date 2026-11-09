"""Opt-in local U05b.4a receipt writer. No trainer, dispatcher or model owner.

Sensor values live ONLY in native FeatureJournal. This module persists topology,
decision/mask receipts and conservative collection watermarks. No producer does
SQLite, waits for capacity, re-evaluates a legal mask or calls HA.
"""
from collections import deque
from itertools import islice
from dataclasses import asdict
import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid

from storage_budget import require_storage_available, StorageRetry
from sqlite_background import background_sqlite, is_sqlite_busy, run_connection_worker

MAX_RECORD = 4 * 1024 * 1024
MAX_ENTITIES = 10000
PREPARATION_POLL_SECONDS = .25


def encoded(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(raw.encode()) > MAX_RECORD:
        raise ValueError('collector receipt byte limit')
    return raw


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def sensor(eid):
    return str(eid).partition('.')[0] in ('sensor', 'binary_sensor')


def compact(state):
    if state is None:
        return None
    value = state.get('state')
    attrs = state.get('attributes') or {}
    result = {'state': value, 'last_updated': state.get('last_updated'),
              'last_changed': state.get('last_changed'),
              'attributes': {k: attrs[k] for k in ('unit_of_measurement', 'device_class','friendly_name','state_class','options') if k in attrs}}
    context=state.get('context') or {}
    if not isinstance(context,dict):raise ValueError('invalid HA context')
    result['context']={k:context[k] for k in ('id','parent_id') if k in context}
    if any(v is not None and (not isinstance(v,str) or len(v)>256) for v in result['context'].values()):
        raise ValueError('HA context bound')
    if any(not isinstance(v, (str, int, float, bool, type(None))) or len(str(v)) > 4096
           for v in [value, *(v for k,v in result['attributes'].items() if k!='options')]):
        raise ValueError('sensor receipt bounds')
    options=result['attributes'].get('options',[])
    if not isinstance(options,(list,tuple)) or len(options)>256 or any(not isinstance(v,str) or len(v)>256 for v in options):
        raise ValueError('sensor option bounds')
    if any(v is not None and (not isinstance(v,str) or len(v)>128)
           for v in (result['last_updated'],result['last_changed'])):
        raise ValueError('sensor clock bounds')
    return result


def bounded_copy(value, *, limit=262144):
    """Bound CPU/allocation without JSON serialization on producers."""
    used=0;nodes=0
    def visit(item,depth=0):
        nonlocal used,nodes
        nodes+=1;used+=64
        if nodes>65536 or depth>10 or used>limit:raise ValueError('receipt structural bound')
        if isinstance(item,str):
            used+=len(item)*4
            if used>limit:raise ValueError('receipt string bound')
            return item
        if item is None or type(item) in (bool,int,float):return item
        if isinstance(item,dict):
            if len(item)>MAX_ENTITIES:raise ValueError('receipt mapping bound')
            return {visit(k,depth+1):visit(v,depth+1) for k,v in item.items()}
        if isinstance(item,(list,tuple)):
            if len(item)>MAX_ENTITIES:raise ValueError('receipt list bound')
            return [visit(v,depth+1) for v in item]
        raise ValueError('non-JSON receipt')
    detached=visit(value)
    return detached,used


class PreparationBudget:
    """Charge one completed turn and let all time outside that turn pay its debt."""

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.ready_at = 0.

    def remaining(self):
        return max(0., self.ready_at - self.clock())

    def charge(self, started, duty_cycle):
        finished = self.clock()
        elapsed = finished - started
        # Ingress and writer notifications may wake the loop several times
        # between polls. They can advance the writer, but do not grant extra
        # snapshot turns within the same preparation polling interval.
        self.ready_at = max(started + PREPARATION_POLL_SECONDS,
                            finished + elapsed * (1. - duty_cycle) / duty_cycle)
        return elapsed


class HistoricalTrainingCollector(threading.Thread):
    daemon = True

    def __init__(self, store, journal, *, capacity=1024, byte_capacity=8*1024*1024,
                 batch_size=64, clock=time.time, idle_ready=None):
        super().__init__(name='homemind-universal-history')
        if not 1 <= capacity <= 4096 or not 1 <= batch_size <= 128 or not 1024 <= byte_capacity <= 16*1024*1024:
            raise ValueError('collector bounds')
        self.store, self.journal, self.clock = store, journal, clock
        self.capacity, self.byte_capacity, self.batch_size = capacity, byte_capacity, batch_size
        self.cv = threading.Condition(); self.pending = deque(); self.pending_bytes = 0
        self.epoch = 0; self.pending_gap = 'startup'; self.stopping = False
        self.reset_registries_pending=True
        self.session = uuid.uuid4().hex; self.registries = {}; self.connected = False
        self.segment = None; self.topology = None; self.tls = threading.local()
        self.subscription_known=False
        self.subscription_at=None
        self.topology_at=None
        self.retry = StorageRetry(); self.ready = threading.Event()
        self.idle_ready=idle_ready or (lambda:True)
        self.halt=threading.Event()
        self.preparation_budget = PreparationBudget()
        self.preparation_stats = dict(turns=0, wall_seconds=0., thread_cpu_seconds=0.,
                                      budget_deferrals=0)
        self.writing_foreground=False
        self.partial_metadata_commit=False
        self.by_kind={}
        self.stats = dict(queued=0, written=0, dropped=0, gaps=0, errors=0, max_queue=0,
                          max_queue_bytes=0, batches=0, max_write_latency_ms=0., last_error=None,
                          deferred_for_foreground=0, foreground_batches=0,
                          deferred_for_sqlite=0,
                          committed_after_epoch_change=0, native_event_upserts=0,
                          native_seed_upserts=0)
        self.latencies=deque(maxlen=2048)
        self._initialize()
        self.store._universal_training_retention_changed=self.retention_changed

    def _initialize(self):
        with self.store.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS universal_collection_meta (id INTEGER PRIMARY KEY CHECK(id=1), retention_epoch INTEGER NOT NULL)')
            c.execute('INSERT OR IGNORE INTO universal_collection_meta VALUES(1,0)')
            from universal_retention import ensure_schema
            ensure_schema(c)
            c.execute('''CREATE TABLE IF NOT EXISTS universal_collection_sessions
                (id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, reason TEXT)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_sessions_open ON universal_collection_sessions(ended)')
            c.execute('''CREATE TABLE IF NOT EXISTS universal_collection_topology
                (id TEXT PRIMARY KEY, at REAL NOT NULL, body TEXT NOT NULL, checksum TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS universal_collection_segments
                (id TEXT PRIMARY KEY, session TEXT NOT NULL, start REAL NOT NULL, through REAL NOT NULL,
                 topology TEXT NOT NULL, proof TEXT NOT NULL, retention_epoch INTEGER NOT NULL,
                 closed INTEGER NOT NULL DEFAULT 0, reason TEXT, proof_hash TEXT NOT NULL,
                 watermark_hash TEXT NOT NULL)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_segments_time ON universal_collection_segments(start,through)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_segments_topology ON universal_collection_segments(topology)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_segments_session ON universal_collection_segments(session,closed)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_segments_through ON universal_collection_segments(through)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_topology_time ON universal_collection_topology(at)')
            c.execute('''CREATE TABLE IF NOT EXISTS universal_collection_decisions
                (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, at REAL NOT NULL, topology TEXT,
                 body TEXT NOT NULL, checksum TEXT NOT NULL)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_decisions_agent_time ON universal_collection_decisions(agent_id,at)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_decisions_time ON universal_collection_decisions(at)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_decisions_topology ON universal_collection_decisions(topology)')
            c.execute('''CREATE TABLE IF NOT EXISTS universal_collection_masks
                (id TEXT PRIMARY KEY, decision_id TEXT, at REAL NOT NULL, body TEXT NOT NULL, checksum TEXT NOT NULL)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_masks_time ON universal_collection_masks(at)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_masks_decision ON universal_collection_masks(decision_id)')
            c.execute('''CREATE TABLE IF NOT EXISTS universal_collection_gaps
                (id INTEGER PRIMARY KEY, session TEXT NOT NULL, at REAL NOT NULL, reason TEXT NOT NULL)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_uc_gaps_time ON universal_collection_gaps(at)')
            # Indexed bounded startup; never replay a historical prefix into RAM.
            prior = c.execute('SELECT id FROM universal_collection_sessions WHERE ended IS NULL LIMIT 32').fetchall()
            for row in prior:
                c.execute("UPDATE universal_collection_sessions SET ended=?,reason='restart' WHERE id=?", (self.clock(),row[0]))
                c.execute("UPDATE universal_collection_segments SET closed=1,reason='restart' WHERE session=? AND closed=0", (row[0],))
            c.execute('INSERT INTO universal_collection_sessions VALUES(?,?,NULL,NULL)',(self.session,self.clock()))

    def gap(self, reason):
        with self.cv:
            if reason in ('startup','websocket_disconnected','restart','registry_announced',
                          'queue_overflow_registry','write_failure','incomplete_registry','invalid_registry') or any(r[1]=='registry' for r in self.pending):
                self.reset_registries_pending=True
            self.epoch += 1; self.pending_gap = str(reason)[:256]
            for row in self.pending:self._count_kind(row[1],'dropped')
            self.stats['dropped'] += len(self.pending); self.pending.clear(); self.pending_bytes = 0
            self.cv.notify()
        return False

    def _count_kind(self, kind, name, amount=1):
        row=self.by_kind.setdefault(kind,dict(queued=0,written=0,dropped=0,rejected=0))
        row[name]+=amount

    def notify(self, method, *args, **kwargs):
        try:
            return getattr(self,method)(*args,**kwargs)
        except Exception:
            return self.gap('capture_failure')

    def apply_registry(self, kind, callback, payload, received, fresh, epoch):
        result=callback(payload)
        if fresh and epoch==self.epoch:
            self.notify('registry',kind,payload,at=max(received,self.clock()))
        return result

    def _submit(self, kind, payload, *, size=1024):
        with self.cv:
            if self.stopping:
                self._count_kind(kind,'rejected')
                return False
            if len(self.pending) >= self.capacity or self.pending_bytes + size > self.byte_capacity:
                self.stats['dropped'] += 1
                self._count_kind(kind,'dropped')
                registry_loss=kind=='registry' or any(r[1]=='registry' for r in self.pending)
                return self.gap('queue_overflow_registry' if registry_loss else 'queue_overflow')
            self.pending.append((self.epoch, kind, payload, size, time.perf_counter()))
            self.pending_bytes += size; self.stats['queued'] += 1
            self._count_kind(kind,'queued')
            self.stats['max_queue'] = max(self.stats['max_queue'],len(self.pending))
            self.stats['max_queue_bytes'] = max(self.stats['max_queue_bytes'], self.pending_bytes)
            self.cv.notify()
        return True

    def event(self, eid, state, *, event_ts, received_ts):
        if not sensor(eid):
            return False
        try:
            if state is not None and state.get('entity_id',eid)!=eid:
                return self.gap('invalid_sensor_identity')
            row = compact(state)
            if row is None or not all(math.isfinite(float(v)) for v in (event_ts,received_ts)):
                return self.gap('missing_sensor_state')
            row,size=bounded_copy(row,limit=65536)
            return self._submit('event',(eid,row,float(event_ts),float(received_ts)),size=size+len(eid)*4)
        except (ValueError,TypeError):
            return self.gap('invalid_sensor_state')

    def registry(self, kind, entries, *, at=None):
        if kind not in ('entity','device','area') or len(entries) > MAX_ENTITIES:
            return self.gap('incomplete_registry')
        keys = ('entity_id','id','area_id','device_id','disabled_by','hidden_by','original_device_class','name')
        try:
            # Called by the existing registry worker, never the socket receiver.
            identifier={'entity':'entity_id','device':'id','area':'area_id'}[kind]
            if any(not isinstance(r,dict) or not isinstance(r.get(identifier),str) or not r[identifier] for r in entries):
                raise ValueError('incomplete registry row')
            rows = [{k:r[k] for k in keys if k in r} for r in entries]
            if len({r[identifier] for r in rows})!=len(rows):raise ValueError('duplicate registry identity')
            for row in rows:
                if any(len(str(v))>1024 for v in row.values()):
                    raise ValueError('registry bound')
            rows,size=bounded_copy(rows,limit=MAX_RECORD)
            return self._submit('registry',(kind,rows,float(self.clock() if at is None else at)),size=size)
        except (ValueError,TypeError):
            return self.gap('invalid_registry')

    def snapshot(self, states, *, received_ts, requested_ts=None, epoch=None, complete=True):
        if not complete or len(states)>MAX_ENTITIES:
            return self.gap('incomplete_snapshot')
        if requested_ts is None or epoch is None:
            return self.gap('missing_snapshot_request_boundary')
        if epoch!=self.epoch:
            self._submit('notice','rejected_snapshot_crossed_gap')
            return False
        try:
            requested_ts=float(requested_ts);received_ts=float(received_ts)
            if not math.isfinite(requested_ts) or not math.isfinite(received_ts) or not 0<requested_ts<=received_ts:
                raise ValueError('snapshot clocks')
            rows = {eid:compact(st) for eid,st in states.items() if sensor(eid)}
            if any(st is None for st in rows.values()):
                return self.gap('incomplete_snapshot')
            rows,size=bounded_copy(rows,limit=MAX_RECORD)
            return self._submit('snapshot',(rows,received_ts,requested_ts),size=size)
        except (TypeError,ValueError):
            return self.gap('invalid_snapshot')

    def connection(self, connected, *, at=None):
        self.subscription_at=float(self.clock() if at is None else at) if connected else None
        self.subscription_known=bool(connected)
        if not connected:
            return self.gap('websocket_disconnected')
        return self._submit('connected',True)

    def barrier(self, at=None):
        """Only the WS producer may attest receipt continuity through this clock."""
        return self._submit('barrier',float(self.clock() if at is None else at))

    def decision(self, intent, agent, state, *, required, affected=None, receipt=None, predecision=None):
        # Detached bounded decision payload; no DeviceAgent call, policy evaluation,
        # registry lookup or DB read here. Missing causal mask stays explicit.
        try:
            affected_evidence=None
            affected_status='legacy_explicit_receipt' if affected is not None else 'affected_sensor_evidence_unavailable'
            if predecision is not None and 'affected_contract_version' in predecision:
                # Already prepared in the SAME original decision context. RAM
                # extraction only; never infer from an area or query live SQL.
                affected_evidence=predecision.get('affected_sensor_evidence')
                affected_status=predecision.get('affected_sensor_status','affected_declaration_missing')
                affected=(affected_evidence.get('affected_sensor_ids')
                          if affected_evidence is not None and affected_evidence.get('complete') is True else None)
                receipt=None  # A new-contract receipt must be verified by our writer.
            if len(required)>MAX_ENTITIES or (affected is not None and len(affected)>MAX_ENTITIES):
                raise ValueError('decision dependency cap')
            if receipt is not None and affected is None:
                receipt=None
            fields=('intent_id','agent_id','target_entity','target_property','desired_value','created_at',
                    'policy_version','model_revision','decision_source')
            body = dict(intent={key:getattr(intent,key) for key in fields},agent=agent,state=state or {},
                        required_sensor_ids=tuple(e for e in required if sensor(e)),
                        non_sensor_feature_dependencies=tuple(e for e in required if not sensor(e)),
                        affected_sensor_ids=None if affected is None else tuple(affected),
                        affected_sensor_proof='unavailable' if affected is None else 'explicit_receipt',
                        affected_sensor_evidence=affected_evidence,affected_sensor_status=affected_status,
                        receipt=asdict(receipt) if receipt is not None else None,
                        predecision=predecision,
                        missing_proof=None if receipt is not None else 'no_predecision_legal_mask',
                        execution_evidence='native_provenance_only')
            body,size=bounded_copy(body)
            return self._submit('decision',body,size=size)
        except (TypeError,ValueError):
            return self.gap('invalid_decision_receipt')

    def mask(self, agent, state, descriptor, result, *, at):
        # Capture the ORIGINAL existing call's result; never evaluate again.
        decision_id = getattr(self.tls,'decision_id',None)
        body = dict(version=1,agent_id=agent['id'],state=dict(state or {}),
                    descriptor=descriptor,mask=result,decision_id=decision_id,
                    at=float(at),information_available_ts=max(float(at),float(self.clock())),
                    mask_contract='DeviceAgent.legal_action_mask/0.14.232',
                    descriptor_contract='DeviceAgent.descriptor/0.14.232',
                    phase='executor_guard' if decision_id else 'unlinked_observation')
        body,size=bounded_copy(body)
        return self._submit('mask',body,size=size)

    def _close(self,c,reason):
        if self.segment:
            c.execute('UPDATE universal_collection_segments SET closed=1,reason=? WHERE id=?',(reason,self.segment))
        self.segment = None

    def _journal_rows(self, rows):
        from training_budget import TRAINING_BUDGET
        keys=[]
        for offset in range(0,len(rows),128):
            keys.extend(self.journal.record_batch(rows[offset:offset+128],wait_for_store=not self.writing_foreground) or ())
            if not self.writing_foreground:
                TRAINING_BUDGET.checkpoint('universal_collection_write',thread_name='adaptive-ai-index-universal-scorer')
        return keys

    def _write(self, batch, gap, epoch, reset_registries):
        from settings import parse_ts
        require_storage_available(self.store)
        observations=[]; metadata=[]; advances={}; affected_topologies={}
        for row_epoch,kind,payload,size,queued in batch:
            if row_epoch!=epoch:
                continue
            if kind=='event':
                eid,st,event,received=payload
                observations.append(dict(entity_id=eid,state=st,event_time=event,received_time=received,
                                         source='ha_state_changed',quality=0. if st.get('state') in (None,'unknown','unavailable') else 1.))
            else:
                metadata.append((kind,payload))
        keys=self._journal_rows(observations)
        self.stats['native_event_upserts']+=len(keys)
        with self.store.conn() as c, c.storage_batch():
            c.execute('BEGIN IMMEDIATE')
            decision_rows=[];mask_rows=[]
            def flush_receipts():
                from sqlite_bulk_rows import insert_bounded_rows
                insert_bounded_rows(c,'INSERT OR IGNORE INTO universal_collection_decisions',decision_rows,6)
                insert_bounded_rows(c,'INSERT OR IGNORE INTO universal_collection_masks',mask_rows,5)
                decision_rows.clear();mask_rows.clear()
            if gap:
                self._close(c,gap)
                if reset_registries:
                    self.registries={};self.connected=self.subscription_known;self.topology=None
                    self.topology_at=None
                self.connected=self.subscription_known
                c.execute('INSERT INTO universal_collection_gaps(session,at,reason) VALUES(?,?,?)',(self.session,self.clock(),gap))
                self.stats['gaps']+=1
            retention=c.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()[0]
            for kind,payload in metadata:
                if kind=='connected':
                    self.connected=True
                elif kind=='notice':
                    c.execute('INSERT INTO universal_collection_gaps(session,at,reason) VALUES(?,?,?)',
                              (self.session,self.clock(),payload))
                elif kind=='registry':
                    name,rows,at=payload; self._close(c,'registry_revision')
                    self.registries[name]=rows
                    if set(self.registries)=={'entity','device','area'}:
                        body=encoded(dict(version=1,registries=self.registries,at=at))
                        self.topology=digest(body)
                        self.topology_at=at
                        c.execute('INSERT OR IGNORE INTO universal_collection_topology VALUES(?,?,?,?)',
                                  (self.topology,at,body,digest(body)))
                elif kind=='snapshot':
                    states,at,requested=payload;self._close(c,'reconciliation')
                    expected={r['entity_id'] for r in self.registries.get('entity',())
                              if r.get('entity_id') and sensor(r['entity_id']) and not r.get('disabled_by')}
                    if (not self.connected or not self.topology or self.subscription_at is None
                            or requested<self.subscription_at or requested<self.topology_at
                            or not expected or not expected<=states.keys()):
                        c.execute('INSERT INTO universal_collection_gaps(session,at,reason) VALUES(?,?,?)',
                                  (self.session,at,'rejected_incomplete_snapshot_boundary'))
                        continue
                    seedrows=[];seeds=[]
                    for eid,st in states.items():
                        event=parse_ts(st.get('last_updated') or st.get('last_changed'))
                        if event is None or event>at:
                            raise ValueError('snapshot lacks original event clock')
                        seedrows.append(dict(entity_id=eid,state=st,event_time=event,received_time=at,
                                             source='universal_collection_seed',quality=0. if st.get('state') in (None,'unknown','unavailable') else 1.))
                        seeds.append((eid,event,at))
                    # Seed values stay in native FeatureJournal, committed BEFORE proof.
                    # Use a separate connection; metadata transaction has not written
                    # for this snapshot path after _close only if no previous segment.
                    flush_receipts()
                    c.commit()
                    self.partial_metadata_commit=True
                    from universal_retention import origin
                    with self.store.conn() as boundary:
                        tracker,origin_rev=origin(boundary)
                    seed_keys=self._journal_rows(seedrows)
                    self.stats['native_seed_upserts']+=len(seed_keys)
                    c.execute('BEGIN IMMEDIATE')
                    current=c.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()[0]
                    if current!=retention or origin(c)!=(tracker,origin_rev) or len(seed_keys)!=len(seeds):
                        continue
                    self.segment=uuid.uuid4().hex
                    from universal_affected_sensors import topology as affected_topology
                    _,universe,_=affected_topology(c,self.topology)
                    proof=encoded(dict(version=3,source_revision=self.segment,start_ts=at,
                                       complete_through_ts=at,entity_ids=sorted(states),complete=True,
                                       seed_receipts=seeds,collector_retention_epoch=retention,
                                       topology_revision=self.topology,historical_sensor_universe_revision=digest(encoded(universe)),
                                       historical_sensor_universe_count=len(universe),retention_tracker=tracker,
                                       retention_origin_revision=origin_rev,
                                       seed_event_keys=[(seed[0],key,seed[1],seed[2]) for seed,key in zip(seeds,seed_keys)]))
                    proof_hash=digest(proof)
                    seal=digest(encoded([self.segment,at,at,self.topology,retention,proof_hash]))
                    c.execute('INSERT INTO universal_collection_segments VALUES(?,?,?,?,?,?,?,0,NULL,?,?)',
                              (self.segment,self.session,at,at,self.topology,proof,retention,proof_hash,seal))
                    from universal_retention import seal as seal_retention
                    seal_retention(c,at)
                elif kind=='barrier' and self.segment:
                    # A gap arriving during writes defeats advancement of this proof.
                    advances[self.segment]=max(payload,advances.get(self.segment,payload))
                elif kind=='decision':
                    evidence=payload.get('predecision')
                    if evidence is not None:
                        from universal_predecision import verify_for_write
                        try:
                            if evidence['epoch']!=self.epoch or evidence['topology']!=self.topology:
                                raise ValueError('predecision topology changed')
                            payload['predecision']=verify_for_write(c,evidence,payload['intent'],payload['agent'],payload['state'])
                            payload['missing_proof']='affected_sensor_evidence_unavailable' if payload['affected_sensor_ids'] is None else None
                            new_affected='affected_contract_version' in evidence
                            if new_affected:
                                from universal_affected_sensors import VERSION as AFFECTED_VERSION, validate, latest, topology as affected_topology
                                try:
                                    if evidence['affected_contract_version']!=AFFECTED_VERSION:
                                        raise ValueError('affected_unsupported_contract')
                                    proof=payload['affected_sensor_evidence']
                                    if proof is not None and latest(c,payload['agent']['id'])!=(proof['declaration_revision'],proof['declaration_json'],proof['declaration_checksum']):
                                        raise ValueError('affected_native_declaration_mismatch')
                                    if self.topology not in affected_topologies:
                                        affected_topologies[self.topology]=affected_topology(c,self.topology)
                                    ids=validate(proof,payload['predecision'],sensor_universe=affected_topologies[self.topology][1],
                                                 topology_information_ts=affected_topologies[self.topology][0])
                                    if list(ids)!=list(payload['affected_sensor_ids'] or ()):
                                        raise ValueError('affected_sensor_set_mismatch')
                                    payload['affected_sensor_status']='complete'
                                    payload['missing_proof']=None
                                except (ValueError,KeyError,TypeError) as exc:
                                    payload['affected_sensor_ids']=None
                                    payload['affected_sensor_status']=str(exc)
                                    payload['missing_proof']=str(exc)
                            if (new_affected or payload['affected_sensor_ids'] is not None) and self.topology:
                                from universal_training_data import HistoricalDecision
                                topology_at=c.execute('SELECT at FROM universal_collection_topology WHERE id=?',(self.topology,)).fetchone()[0]
                                payload['receipt']=asdict(HistoricalDecision.capture(
                                    decision_ts=payload['intent']['created_at'],event_ts=evidence['created_ts'],
                                    received_ts=evidence['available_ts'],registry_information_ts=topology_at,
                                    registry_revision=self.topology,agent=payload['agent'],descriptor=evidence['descriptor'],
                                    legal_mask=evidence['legal_mask'],state=payload['state'],
                                    required_sensor_ids=payload['required_sensor_ids'],affected_sensor_ids=payload['affected_sensor_ids'],
                                    predecision_evidence=payload['predecision'],
                                    affected_contract_version=evidence.get('affected_contract_version'),
                                    affected_sensor_evidence=payload['affected_sensor_evidence'],
                                    affected_sensor_status=payload['affected_sensor_status']))
                        except (ValueError,KeyError,TypeError):
                            payload['predecision']=None
                            payload['receipt']=None
                            payload['missing_proof']='stale_or_invalid_predecision_evidence'
                    body=encoded(payload);intent=payload['intent']
                    decision_rows.append((intent['intent_id'],intent['agent_id'],intent['created_at'],self.topology,body,digest(body)))
                elif kind=='mask':
                    body=encoded(payload)
                    mask_rows.append((digest(body),payload['decision_id'],payload['at'],body,digest(body)))
            flush_receipts()
            if self.epoch==epoch and self.pending_gap is None:
                for sid,at in advances.items():
                    row=c.execute('SELECT start,through,topology,retention_epoch,proof_hash FROM universal_collection_segments WHERE id=?',(sid,)).fetchone()
                    if row and row[3]==retention:
                        through=max(row[1],at)
                        seal=digest(encoded([sid,row[0],through,row[2],row[3],row[4]]))
                        c.execute('UPDATE universal_collection_segments SET through=?,watermark_hash=? WHERE id=?',(through,seal,sid))
                        from universal_retention import seal as seal_retention
                        seal_retention(c,through)
            if self.epoch!=epoch:
                self._close(c,'concurrent_gap')
        self.stats['written']+=len(batch);self.stats['batches']+=1
        with self.cv:
            for row in batch:self._count_kind(row[1],'written')
        if batch:
            self.stats['max_write_latency_ms']=max(self.stats['max_write_latency_ms'],(time.perf_counter()-batch[0][-1])*1000)
            with self.cv:
                self.latencies.extend((time.perf_counter()-r[-1])*1000 for r in batch)

    def drain(self, *, foreground=False):
        """Single-writer batch, also used by deterministic tests."""
        # A short foreground turn may make progress without waiting for Store or
        # SQLite. Admission never acquires Engine/context locks or blocks ingress.
        # No training-budget sleep is allowed while this writer owns Store.lock.
        with self.cv:
            limit=min(self.batch_size,64) if foreground else self.batch_size
            batch=list(islice(self.pending,limit))
            if foreground:
                # At most 1 MiB of structural payload estimates per turn, or
                # one already-bounded snapshot/receipt. Queue bounds are unchanged.
                used=0; bounded=[]
                for row in batch:
                    if bounded and used+row[3]>1048576:break
                    bounded.append(row);used+=row[3]
                batch=bounded
            gap=self.pending_gap; self.pending_gap=None;epoch=self.epoch
            reset_registries=self.reset_registries_pending;self.reset_registries_pending=False
        # Retain pending capacity/bytes until commit. A concurrent overflow/gap
        # invalidates this epoch as before; SQLITE_BUSY never creates a data gap.
        saved=(dict(self.registries),self.connected,self.topology,self.topology_at,self.segment)
        self.partial_metadata_commit=False
        try:
            if not batch and gap is None:return True
            # Admission is BEFORE any native write or SQLite transaction. Fresh
            # HA work gets its existing interactive CPU window; overload burns
            # coverage instead of pushing latency back onto inference.
            from training_budget import TRAINING_BUDGET
            if not foreground:
                TRAINING_BUDGET.checkpoint('universal_collection_admission',thread_name='adaptive-ai-index-universal-scorer')
            self.writing_foreground=foreground
            scope=self.store.background_sqlite(timeout_ms=0) if foreground else background_sqlite(self.store)
            with scope:
                self._write(batch,gap,epoch,reset_registries)
            with self.cv:
                if self.epoch==epoch:
                    for _ in batch:self.pending.popleft()
                    self.pending_bytes-=sum(r[3] for r in batch)
                else:
                    # gap() invalidated/cleared this still-reserved batch while
                    # SQLite was writing. Its completed commit is observable,
                    # even though coverage is invalid. Do not count those same
                    # envelopes as both committed and lost.
                    self.stats['dropped']-=len(batch)
                    for row in batch:self._count_kind(row[1],'dropped',-1)
                    self.stats['committed_after_epoch_change']+=len(batch)
            if foreground:self.stats['foreground_batches']+=1
            self.retry.recovered()
        except Exception as exc:
            if is_sqlite_busy(exc) and not self.partial_metadata_commit:
                self.stats['deferred_for_sqlite']+=1
                self.registries,self.connected,self.topology,self.topology_at,self.segment=saved
                with self.cv:
                    if self.epoch==epoch:
                        self.pending_gap=gap
                        self.reset_registries_pending=reset_registries
                return False
            # Snapshot seeding has an explicit metadata commit before native
            # observations. A later failure cannot rewind that durable boundary;
            # retain the existing fail-closed gap rather than replaying it as if
            # the whole metadata transaction had rolled back.
            self.gap('write_failure')
            if (threading.current_thread() is self and isinstance(exc, sqlite3.ProgrammingError)
                    and str(exc) == 'Cannot operate on a closed database.'):
                # Retry on a new worker session. Reusing this connection would
                # keep failing even when the underlying database is available.
                raise
            self.stats['errors']+=1;self.stats['last_error']=type(exc).__name__
            self.retry.defer(exc)
            return False
        finally:
            self.writing_foreground=False
        return True

    def retention_changed(self,c):
        """Called inside native FeatureJournal prune's SAME transaction, off ingress."""
        c.execute('UPDATE universal_collection_meta SET retention_epoch=retention_epoch+1 WHERE id=1')

    def housekeep(self, *, now=None, retention_seconds=14*86400):
        if not self.idle_ready():return False
        cutoff=float(self.clock() if now is None else now)-retention_seconds
        with background_sqlite(self.store),self.store.conn() as c,c.storage_batch():
            for table,column in (('decisions','at'),('masks','at'),('segments','through'),('gaps','at')):
                c.execute(f'DELETE FROM universal_collection_{table} WHERE rowid IN '
                          f'(SELECT rowid FROM universal_collection_{table} WHERE {column}<? LIMIT 128)',(cutoff,))
            c.execute('DELETE FROM universal_collection_topology WHERE id IN (SELECT id FROM universal_collection_topology t '
                      'WHERE at<? AND NOT EXISTS(SELECT 1 FROM universal_collection_segments s WHERE s.topology=t.id) '
                      'AND NOT EXISTS(SELECT 1 FROM universal_collection_decisions d WHERE d.topology=t.id) LIMIT 32)',(cutoff,))
            c.execute('DELETE FROM universal_collection_sessions WHERE id IN (SELECT id FROM universal_collection_sessions x '
                      'WHERE ended<? AND NOT EXISTS(SELECT 1 FROM universal_collection_segments s WHERE s.session=x.id) LIMIT 32)',(cutoff,))

    def run(self):
        # Short conn() transactions retain their independent commit boundaries.
        # Opening the reusable session can fail before drain()'s retry handler.
        # Use the same reopen/final-drain lifecycle as the other native writers.
        def report(exc):
            self.stats['errors']+=1;self.stats['last_error']=type(exc).__name__
        try:
            run_connection_worker(self.store,self.halt,self._run,self.retry,report)
        finally:
            evidence = getattr(self, 'predecision', None)
            if callable(getattr(evidence, 'close', None)):
                evidence.close()

    def _idle_wait(self):
        evidence = getattr(self, 'predecision', None)
        if evidence is not None and evidence.pending:
            # The periodic poll can repeatedly miss an idle slot between state
            # changes. Recheck admission at the end of the interactive window
            # when preparation is queued, without preparing in foreground.
            remaining = max(foreground_priority_remaining(evidence.engine),
                            self.preparation_budget.remaining())
            if remaining > 0:
                return min(PREPARATION_POLL_SECONDS, remaining)
        return PREPARATION_POLL_SECONDS

    def _prepare_turn(self):
        evidence = getattr(self, 'predecision', None)
        if evidence is None or not evidence.pending or self.stopping or not self.idle_ready():
            return
        budget = self.preparation_budget
        if budget.remaining() > 0:
            self.preparation_stats['budget_deferrals'] += 1
            return
        from training_budget import TRAINING_BUDGET
        duty_cycle = TRAINING_BUDGET.duty_cycle
        started = budget.clock()
        cpu_started = time.thread_time()
        try:
            # Preparation can be retried via a subsequent request. Avoid waiting
            # on SQLite while the same worker also owns the writer queue.
            with self.store.background_sqlite(timeout_ms=0):
                evidence.prepare_one()
        except Exception:
            evidence.stats['errors'] += 1
        finally:
            self.preparation_stats['wall_seconds'] += budget.charge(started, duty_cycle)
            self.preparation_stats['thread_cpu_seconds'] += time.thread_time() - cpu_started
            self.preparation_stats['turns'] += 1
            # The writer's cooperative clock is thread-local. Do not bill the
            # same preparation (or the following idle interval) to its next turn.
            TRAINING_BUDGET.end()

    def _run(self):
        evidence = getattr(self, 'predecision', None)
        if callable(getattr(evidence, 'close', None)):
            evidence.close()
        self.ready.set()
        recheck_preparation = False
        while True:
            with self.cv:
                if (not recheck_preparation and not self.pending and self.pending_gap is None
                        and not self.stopping):
                    self.cv.wait(self._idle_wait())
                if self.stopping and not self.pending and self.pending_gap is None:
                    break
            recheck_preparation = False
            if self.retry.delay()>0:
                with self.cv:self.cv.wait(min(.5,self.retry.delay()))
            elif not self.stopping and not self.idle_ready():
                # Waiting for *all* futures starved this queue under sustained
                # inference. Offer a bounded, non-waiting writer turn instead.
                # Expensive mask preparation and housekeeping remain idle-only.
                if not self.drain(foreground=True):
                    self.stats['deferred_for_foreground']+=1
                try:
                    with self.store.background_sqlite(timeout_ms=0):
                        self.journal.flush_pending_prune(wait_for_store=False)
                except Exception as exc:
                    if not is_sqlite_busy(exc):
                        self.stats['errors']+=1;self.stats['last_error']=type(exc).__name__
                self.halt.wait(.005)
                # A foreground drain can finish after the idle window opens.
                # Re-enter normal admission before waiting for another poll.
                recheck_preparation = (
                    evidence is not None and bool(evidence.pending) and not self.stopping
                    and foreground_priority_remaining(evidence.engine) <= 0
                    and self.preparation_budget.remaining() <= 0
                    and self.retry.delay() <= 0 and self.idle_ready()
                )
            else:
                if self.drain():
                    self._prepare_turn()
        with self.store.conn() as c:
            self._close(c,'clean_shutdown')
            c.execute("UPDATE universal_collection_sessions SET ended=?,reason='clean_shutdown' WHERE id=?",(self.clock(),self.session))

    def stop(self, timeout=3.):
        with self.cv:self.stopping=True;self.cv.notify_all()
        self.halt.set()
        if self.is_alive():self.join(timeout)
        stopped = not self.is_alive()
        if stopped:
            # Also dispose a baseline prepared synchronously before start().
            evidence = getattr(self, 'predecision', None)
            if callable(getattr(evidence, 'close', None)):
                evidence.close()
        return stopped

    def diagnostics(self):
        with self.cv:
            latencies=list(self.latencies)
            result=dict(self.stats,enabled=True,queue=len(self.pending),queue_bytes=self.pending_bytes,
                        epoch=self.epoch,session=self.session,worker_alive=self.is_alive(),
                        by_kind={kind:dict(row) for kind,row in self.by_kind.items()})
        latencies.sort()
        for p in (50,95,99):
            result[f'write_latency_p{p}_ms']=latencies[int(p/100*(len(latencies)-1))] if latencies else None
        result['write_latency_sample_count']=len(latencies)
        result['write_latency_scope']='bounded tail of completed envelope turns, not unique native rows'
        evidence=getattr(self,'predecision',None)
        if evidence is not None:
            result['predecision']=dict(evidence.stats,prepared_bytes=evidence.prepared_bytes)
            result['predecision_budget'] = dict(self.preparation_stats)
        return result


def foreground_priority_remaining(engine):
    from settings import OPTIONS
    window = float(OPTIONS.get('training_realtime_event_priority_seconds', .3))
    elapsed = time.perf_counter()-float(getattr(engine, 'last_event_received', 0.) or 0.)
    return window-elapsed


def foreground_idle(engine):
    """Completed futures stay indexed by target; they are not foreground work."""
    return (not any(not future.done() for future in list(getattr(engine, 'in_flight', {}).values()))
            and foreground_priority_remaining(engine) <= 0)


def install(engine, store):
    from settings import OPTIONS
    if not bool(OPTIONS.get('universal_training_collector_enabled',False)):
        return None
    previous=getattr(engine,'universal_training_collector',None)
    if previous is not None:return previous
    service=getattr(engine.executor,'device_agents',None)
    try:
        collector=HistoricalTrainingCollector(store,engine.feature_journal,idle_ready=lambda:foreground_idle(engine))
        if service is not None:
            from universal_predecision import PredecisionEvidence
            collector.predecision=PredecisionEvidence(engine,store,collector)
        engine.universal_training_collector=collector
        if service is not None:service.universal_training_collector=collector
        collector.start()
        return collector
    except Exception as exc:
        # Optional audit failure must not prevent HTTP/HA/runtime startup.
        engine.universal_training_collector=None
        engine.universal_training_collector_error=type(exc).__name__
        if service is not None:service.universal_training_collector=None
        return None


def native_decision_evidence(conn, decision_id):
    """Read native lifecycle facts without flushing or conflating dispatch/ACK/state."""
    row=conn.execute('''SELECT decision_id,generation_id,model_version,model_revision,
        model_desired,chosen_action,dispatch_status,dispatch_time,ack_time,ack_event_id
        FROM provenance_decisions WHERE decision_id=?''',(decision_id,)).fetchone()
    if row is None:return None
    result=dict(zip(('decision_id','generation_id','model_version','model_revision',
        'prediction','chosen_action','dispatch_status','dispatch_time','ack_time','ack_event_id'),row))
    event=None
    if result['ack_event_id']:
        event=conn.execute('SELECT event_id,entity_id,event_time,received_time,state_json,origin '
                           'FROM provenance_events WHERE event_id=?',(result['ack_event_id'],)).fetchone()
    result['ack_state_event']=None if event is None else dict(zip(
        ('event_id','entity_id','event_time','received_time','state_json','origin'),event))
    result['dispatch_recorded']=result['dispatch_time'] is not None
    result['state_ack_recorded']=result['ack_time'] is not None
    result['physical_execution_confirmed']=None  # ACK correlation is not independent actuator telemetry.
    return result


def verify_collection_coverage(conn, coverage):
    """A previously exported source is not authority after native retention loss."""
    if coverage.version==3:
        from universal_retention import verify
        return verify(conn,coverage)
    row=conn.execute('SELECT start,through,topology,proof,retention_epoch,proof_hash,watermark_hash '
                     'FROM universal_collection_segments WHERE id=?',(coverage.source_revision,)).fetchone()
    current=conn.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()
    if row is None or current is None or current[0]!=row[4] or row[4]!=coverage.collector_retention_epoch:
        raise ValueError('collection retention witness expired')
    if digest(row[3])!=row[5] or digest(encoded([coverage.source_revision,row[0],row[1],row[2],row[4],row[5]]))!=row[6]:
        raise ValueError('corrupt collection witness')
    proof=json.loads(row[3])
    if proof.get('version')!=2:raise ValueError('collection witness contract mismatch')
    if (coverage.start_ts!=row[0] or not row[0]<=coverage.complete_through_ts<=row[1]
            or coverage.entity_ids!=frozenset(proof['entity_ids'])
            or coverage.seed_receipts!=tuple(tuple(s) for s in proof['seed_receipts'])
            or coverage.complete is not True):
        raise ValueError('collection witness mismatch')


def verify_collected_samples(conn, doc, data):
    """Exported JSON cannot replace original native V3 receipt/provenance facts.

    Validate only samples actually admitted by U05a; its ordinary per-sample
    rejection reasons remain intact. Used receipts and the historical registry
    are rechecked again by lifecycle acceptance inside its writer transaction.
    Indexed batches bound queries and payload; no current HA/config is consulted.
    """
    from universal_ha_adapter import SemanticRegistry
    from training_budget import TRAINING_BUDGET
    if not conn.in_transaction:raise ValueError('consistent native receipt read required')
    if not data.samples:return
    proof=doc['coverage']
    row=conn.execute('SELECT at,body,checksum FROM universal_collection_topology WHERE id=?',
                     (proof['topology_revision'],)).fetchone()
    if row is None or digest(row[1])!=row[2] or row[2]!=proof['topology_revision']:
        raise ValueError('native historical registry unavailable')
    registries=json.loads(row[1])['registries']
    registry=SemanticRegistry.create(entities=registries['entity'],devices=registries['device'],revision=row[2])
    expected=[dict(effective_ts=row[0],received_ts=row[0],registry=dict(revision=registry.revision,
        entities={k:dict(v) for k,v in registry.entities.items()},
        devices={k:dict(v) for k,v in registry.devices.items()},calibrations={}))]
    if doc['epochs']!=expected:raise ValueError('native historical registry binding mismatch')
    supplied={(r['agent_id'],r['at']):r['receipt'] for r in doc['decisions']}
    if len(supplied)!=len(doc['decisions']):raise ValueError('ambiguous exported decision clock')
    wanted=sorted({(s.agent_id,s.observation_timestamp) for s in data.samples})
    retained=0
    for offset in range(0,len(wanted),64):
        keys=wanted[offset:offset+64];marks=','.join('(?,?)' for _ in keys)
        rows=conn.execute(f'''WITH wanted(agent,at) AS (VALUES {marks})
            SELECT d.id,d.agent_id,d.at,d.body,d.checksum,
                   p.agent_id,p.created_time,p.model_version,p.model_revision,p.generation_id
            FROM wanted w JOIN universal_collection_decisions d INDEXED BY idx_uc_decisions_agent_time
              ON d.agent_id=w.agent AND d.at=w.at
            LEFT JOIN provenance_decisions p ON p.decision_id=d.id LIMIT ?''',
            (*[v for key in keys for v in key],2*len(keys)+1))
        found=set()
        for index,item in enumerate(rows):
            if index>=2*len(keys):raise ValueError('native decision ambiguity bound')
            retained+=len(item[3].encode())
            if retained>16*1024*1024:raise ValueError('native receipt verification byte cap')
            if digest(item[3])!=item[4]:raise ValueError('native decision checksum mismatch')
            body=json.loads(item[3]);key=(item[1],item[2]);intent=body['intent']
            if body.get('receipt')!=supplied.get(key):raise ValueError('native original decision receipt mismatch')
            if (intent['intent_id']!=item[0] or tuple(item[5:9])!=
                    (item[1],item[2],intent['policy_version'],intent['model_revision'])):
                raise ValueError('native decision provenance binding mismatch')
            evidence=body.get('predecision')
            if evidence is not None and item[9]!=evidence['generation_id']:
                raise ValueError('native decision generation binding mismatch')
            found.add(key)
            TRAINING_BUDGET.checkpoint('universal_native_receipt_verify',thread_name='adaptive-ai-index-universal-data')
        if found!=set(keys):raise ValueError('native original decision receipt unavailable')


def source_from_collection(conn, agent_ids, *, start, through, max_decisions=2048):
    """Consistent offline adapter -> existing U05b.3 NativeDatasetSource/U05a.

    Refuses window stitching, missing masks, corrupt receipts and retention loss.
    Never flushes provenance or reads current HA/DeviceAgent metadata.
    """
    from universal_checkpoint_lifecycle import NativeDatasetSource
    from universal_training_data import HistoricalDecision, ReplayCoverage
    from universal_ha_adapter import RegistryEpoch, SemanticRegistry
    if not conn.in_transaction or not 0<=start<=through or not 1<=max_decisions<=2048:
        raise ValueError('bounded consistent collector read required')
    row=conn.execute('''SELECT id,start,through,topology,proof,retention_epoch,proof_hash,watermark_hash
        FROM universal_collection_segments WHERE start<=? AND through>=?
        ORDER BY start DESC LIMIT 1''',(start,through)).fetchone()
    if row is None:raise ValueError('incomplete replay coverage')
    original_proof=json.loads(row[4])
    current=conn.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()[0]
    if original_proof.get('version')!=3 and current!=row[5]:raise ValueError('native retention invalidated coverage')
    if digest(row[4])!=row[6] or digest(encoded([row[0],row[1],row[2],row[3],row[5],row[6]]))!=row[7]:
        raise ValueError('corrupt collection coverage')
    proof=dict(original_proof);proof['complete_through_ts']=row[2]
    proof['entity_ids']=frozenset(proof['entity_ids'])
    coverage=ReplayCoverage(**proof)
    topology=conn.execute('SELECT at,body,checksum FROM universal_collection_topology WHERE id=?',(row[3],)).fetchone()
    if topology is None or digest(topology[1])!=topology[2] or digest(topology[1])!=row[3]:
        raise ValueError('corrupt historical topology')
    doc=json.loads(topology[1]);registries=doc['registries']
    semantic=SemanticRegistry.create(entities=registries['entity'],devices=registries['device'],revision=row[3])
    decisions={}
    ids=tuple(sorted(set(agent_ids)))
    if not ids or len(ids)>256:raise ValueError('agent bounds')
    marks=','.join('?' for _ in ids)
    rows=conn.execute(f'''SELECT id,agent_id,at,body,checksum FROM universal_collection_decisions
        WHERE agent_id IN ({marks}) AND at>=? AND at<=? ORDER BY at,id LIMIT ?''',(*ids,start,through,max_decisions+1))
    retained=0
    for index,item in enumerate(rows):
        if index>=max_decisions:raise ValueError('decision read cap')
        retained+=len(item[3].encode())
        if retained>16*1024*1024:raise ValueError('decision byte read cap')
        if digest(item[3])!=item[4]:raise ValueError('corrupt historical decision')
        body=json.loads(item[3]);receipt=body.get('receipt')
        evidence=body.get('predecision')
        if evidence is not None:
            from universal_predecision import validate_binding
            validate_binding(evidence,body['intent'],body['agent'],body['state'])
            if evidence.get('verified_revision')!=evidence['arbiter_revision']:
                raise ValueError('unverified predecision evidence')
        if receipt is None:continue  # U05a returns explicit missing_receipt rejection.
        native=conn.execute('SELECT agent_id,created_time,model_version,model_revision,generation_id FROM provenance_decisions WHERE decision_id=?',
                            (item[0],)).fetchone()
        if native is None:continue
        intent=body['intent']
        if tuple(native[:4])!=(item[1],item[2],intent['policy_version'],intent['model_revision']):
            raise ValueError('native provenance binding mismatch')
        if evidence is not None and native[4]!=evidence['generation_id']:
            raise ValueError('native generation binding mismatch')
        receipt=HistoricalDecision(**receipt)
        if evidence is not None and json.loads(receipt.payload_json).get('predecision_evidence')!=evidence:
            raise ValueError('predecision receipt binding mismatch')
        if receipt.version==2:
            doc=json.loads(receipt.payload_json)
            if (doc.get('affected_sensor_evidence')!=body.get('affected_sensor_evidence')
                    or doc.get('affected_sensor_ids')!=body.get('affected_sensor_ids')
                    or doc.get('affected_sensor_status')!=body.get('affected_sensor_status')):
                raise ValueError('affected receipt binding mismatch')
        if receipt.decision_ts!=item[2] or receipt.registry_revision!=row[3]:
            raise ValueError('historical decision binding mismatch')
        key=(item[1],item[2])
        if key in decisions and decisions[key]!=receipt:raise ValueError('ambiguous historical decision clock')
        decisions[key]=receipt
        from training_budget import TRAINING_BUDGET
        TRAINING_BUDGET.checkpoint('universal_collection_read',thread_name='adaptive-ai-index-universal-data')
    if coverage.version==3:
        from dataclasses import replace
        from universal_retention import make_witness
        required=set()
        for receipt in decisions.values():
            required.update(json.loads(receipt.payload_json)['required_sensor_ids'])
        if not required:required=set(coverage.entity_ids)
        witness=make_witness(conn,row[0],original_proof,through=through,required=required)
        coverage=replace(coverage,entity_ids=frozenset(required),complete_through_ts=through,
            seed_receipts=tuple(tuple(s) for s in witness['seed_receipts']),
            seed_event_keys=tuple(tuple(s) for s in witness['seed_event_keys']),granular_witness_json=encoded(witness))
        verify_collection_coverage(conn,coverage)
    return NativeDatasetSource.create(agent_ids=ids,decisions=decisions,
        epochs=[RegistryEpoch(topology[0],topology[0],semantic)],coverage=coverage,
        as_of_ts=through,evidence='offline')
