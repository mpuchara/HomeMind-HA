"""Opt-in local U05b.4a receipt writer. No trainer, dispatcher or model owner.

Sensor values live ONLY in native FeatureJournal. This module persists topology,
decision/mask receipts and conservative collection watermarks. No producer does
SQLite, waits for capacity, re-evaluates a legal mask or calls HA.
"""
from collections import deque
from dataclasses import asdict
import hashlib
import json
import math
import threading
import time
import uuid

from storage_budget import require_storage_available, StorageRetry
from sqlite_background import background_sqlite

MAX_RECORD = 4 * 1024 * 1024
MAX_ENTITIES = 10000


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
        self.stats = dict(queued=0, written=0, dropped=0, gaps=0, errors=0, max_queue=0,
                          max_queue_bytes=0, batches=0, max_write_latency_ms=0., last_error=None,
                          deferred_for_foreground=0)
        self.latencies=deque(maxlen=2048)
        self._initialize()
        self.store._universal_training_retention_changed=self.retention_changed

    def _initialize(self):
        with self.store.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS universal_collection_meta (id INTEGER PRIMARY KEY CHECK(id=1), retention_epoch INTEGER NOT NULL)')
            c.execute('INSERT OR IGNORE INTO universal_collection_meta VALUES(1,0)')
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
            self.stats['dropped'] += len(self.pending); self.pending.clear(); self.pending_bytes = 0
            self.cv.notify()
        return False

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
                return False
            if len(self.pending) >= self.capacity or self.pending_bytes + size > self.byte_capacity:
                self.stats['dropped'] += 1
                registry_loss=kind=='registry' or any(r[1]=='registry' for r in self.pending)
                return self.gap('queue_overflow_registry' if registry_loss else 'queue_overflow')
            self.pending.append((self.epoch, kind, payload, size, time.perf_counter()))
            self.pending_bytes += size; self.stats['queued'] += 1
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
        for offset in range(0,len(rows),128):
            self.journal.record_batch(rows[offset:offset+128])
            TRAINING_BUDGET.checkpoint('universal_collection_write',thread_name='adaptive-ai-index-universal-scorer')

    def _write(self, batch, gap, epoch, reset_registries):
        from settings import parse_ts
        require_storage_available(self.store)
        observations=[]; metadata=[]; advances={}
        for row_epoch,kind,payload,size,queued in batch:
            if row_epoch!=epoch:
                continue
            if kind=='event':
                eid,st,event,received=payload
                observations.append(dict(entity_id=eid,state=st,event_time=event,received_time=received,
                                         source='ha_state_changed',quality=0. if st.get('state') in (None,'unknown','unavailable') else 1.))
            else:
                metadata.append((kind,payload))
        self._journal_rows(observations)
        with self.store.conn() as c, c.storage_batch():
            c.execute('BEGIN IMMEDIATE')
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
                    c.commit()
                    self._journal_rows(seedrows)
                    c.execute('BEGIN IMMEDIATE')
                    current=c.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()[0]
                    if current!=retention:
                        continue
                    self.segment=uuid.uuid4().hex
                    proof=encoded(dict(version=2,source_revision=self.segment,start_ts=at,
                                       complete_through_ts=at,entity_ids=sorted(states),complete=True,
                                       seed_receipts=seeds,collector_retention_epoch=retention))
                    proof_hash=digest(proof)
                    seal=digest(encoded([self.segment,at,at,self.topology,retention,proof_hash]))
                    c.execute('INSERT INTO universal_collection_segments VALUES(?,?,?,?,?,?,?,0,NULL,?,?)',
                              (self.segment,self.session,at,at,self.topology,proof,retention,proof_hash,seal))
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
                            if payload['affected_sensor_ids'] is not None and self.topology:
                                from universal_training_data import HistoricalDecision
                                topology_at=c.execute('SELECT at FROM universal_collection_topology WHERE id=?',(self.topology,)).fetchone()[0]
                                payload['receipt']=asdict(HistoricalDecision.capture(
                                    decision_ts=payload['intent']['created_at'],event_ts=evidence['created_ts'],
                                    received_ts=evidence['available_ts'],registry_information_ts=topology_at,
                                    registry_revision=self.topology,agent=payload['agent'],descriptor=evidence['descriptor'],
                                    legal_mask=evidence['legal_mask'],state=payload['state'],
                                    required_sensor_ids=payload['required_sensor_ids'],affected_sensor_ids=payload['affected_sensor_ids'],
                                    predecision_evidence=payload['predecision']))
                        except (ValueError,KeyError,TypeError):
                            payload['predecision']=None
                            payload['receipt']=None
                            payload['missing_proof']='stale_or_invalid_predecision_evidence'
                    body=encoded(payload);intent=payload['intent']
                    c.execute('INSERT OR IGNORE INTO universal_collection_decisions VALUES(?,?,?,?,?,?)',
                              (intent['intent_id'],intent['agent_id'],intent['created_at'],self.topology,body,digest(body)))
                elif kind=='mask':
                    body=encoded(payload)
                    c.execute('INSERT OR IGNORE INTO universal_collection_masks VALUES(?,?,?,?,?)',
                              (digest(body),payload['decision_id'],payload['at'],body,digest(body)))
            if self.epoch==epoch and self.pending_gap is None:
                for sid,at in advances.items():
                    row=c.execute('SELECT start,through,topology,retention_epoch,proof_hash FROM universal_collection_segments WHERE id=?',(sid,)).fetchone()
                    if row and row[3]==retention:
                        through=max(row[1],at)
                        seal=digest(encoded([sid,row[0],through,row[2],row[3],row[4]]))
                        c.execute('UPDATE universal_collection_segments SET through=?,watermark_hash=? WHERE id=?',(through,seal,sid))
            if self.epoch!=epoch:
                self._close(c,'concurrent_gap')
        self.stats['written']+=len(batch);self.stats['batches']+=1
        if batch:
            self.stats['max_write_latency_ms']=max(self.stats['max_write_latency_ms'],(time.perf_counter()-batch[0][-1])*1000)
            with self.cv:
                self.latencies.extend((time.perf_counter()-r[-1])*1000 for r in batch)

    def drain(self):
        """Single-writer batch, also used by deterministic tests."""
        with self.cv:
            batch=[self.pending.popleft() for _ in range(min(self.batch_size,len(self.pending)))]
            self.pending_bytes-=sum(r[3] for r in batch)
            gap=self.pending_gap; self.pending_gap=None;epoch=self.epoch
            reset_registries=self.reset_registries_pending;self.reset_registries_pending=False
        if not batch and gap is None:return True
        try:
            # Admission is BEFORE any native write or SQLite transaction. Fresh
            # HA work gets its existing interactive CPU window; overload burns
            # coverage instead of pushing latency back onto inference.
            from training_budget import TRAINING_BUDGET
            TRAINING_BUDGET.checkpoint('universal_collection_admission',thread_name='adaptive-ai-index-universal-scorer')
            with background_sqlite(self.store):
                self._write(batch,gap,epoch,reset_registries)
            self.retry.recovered()
        except Exception as exc:
            self.stats['errors']+=1;self.stats['last_error']=type(exc).__name__
            self.gap('write_failure');self.retry.defer(exc)
            return False
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
        self.ready.set()
        while True:
            with self.cv:
                if not self.pending and self.pending_gap is None and not self.stopping:
                    self.cv.wait(.25)
                if self.stopping and not self.pending and self.pending_gap is None:
                    break
            if self.retry.delay()>0:
                with self.cv:self.cv.wait(min(.5,self.retry.delay()))
            elif not self.stopping and not self.idle_ready():
                # Same foreground futures and realtime window as the shipped
                # runtime. Never turn a bounded data backlog into UI contention.
                self.stats['deferred_for_foreground']+=1
                self.halt.wait(.05)
            else:
                self.drain()
                evidence=getattr(self,'predecision',None)
                if evidence is not None and not self.stopping and self.idle_ready():
                    try:
                        with background_sqlite(self.store):
                            evidence.prepare_one()
                    except Exception:
                        evidence.stats['errors']+=1
        with self.store.conn() as c:
            self._close(c,'clean_shutdown')
            c.execute("UPDATE universal_collection_sessions SET ended=?,reason='clean_shutdown' WHERE id=?",(self.clock(),self.session))

    def stop(self, timeout=3.):
        with self.cv:self.stopping=True;self.cv.notify_all()
        self.halt.set()
        if self.is_alive():self.join(timeout)
        return not self.is_alive()

    def diagnostics(self):
        with self.cv:
            latencies=list(self.latencies)
            result=dict(self.stats,enabled=True,queue=len(self.pending),queue_bytes=self.pending_bytes,
                        epoch=self.epoch,session=self.session,worker_alive=self.is_alive())
        latencies.sort()
        result['write_latency_p95_ms']=latencies[int(.95*(len(latencies)-1))] if latencies else None
        evidence=getattr(self,'predecision',None)
        if evidence is not None:
            result['predecision']=dict(evidence.stats,prepared_bytes=evidence.prepared_bytes)
        return result


def install(engine, store):
    from settings import OPTIONS
    if not bool(OPTIONS.get('universal_training_collector_enabled',False)):
        return None
    previous=getattr(engine,'universal_training_collector',None)
    if previous is not None:return previous
    service=getattr(engine.executor,'device_agents',None)
    try:
        def idle_ready():
            return (not bool(getattr(engine,'in_flight',{})) and
                    time.perf_counter()-float(getattr(engine,'last_event_received',0.) or 0.) >=
                    float(OPTIONS.get('training_realtime_event_priority_seconds',.3)))
        collector=HistoricalTrainingCollector(store,engine.feature_journal,idle_ready=idle_ready)
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
    row=conn.execute('SELECT start,through,topology,proof,retention_epoch,proof_hash,watermark_hash '
                     'FROM universal_collection_segments WHERE id=?',(coverage.source_revision,)).fetchone()
    current=conn.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()
    if row is None or current is None or current[0]!=row[4] or row[4]!=coverage.collector_retention_epoch:
        raise ValueError('collection retention witness expired')
    if digest(row[3])!=row[5] or digest(encoded([coverage.source_revision,row[0],row[1],row[2],row[4],row[5]]))!=row[6]:
        raise ValueError('corrupt collection witness')
    proof=json.loads(row[3])
    if (coverage.start_ts!=row[0] or not row[0]<=coverage.complete_through_ts<=row[1]
            or coverage.entity_ids!=frozenset(proof['entity_ids'])
            or coverage.seed_receipts!=tuple(tuple(s) for s in proof['seed_receipts'])
            or coverage.complete is not True):
        raise ValueError('collection witness mismatch')


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
    current=conn.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()[0]
    if current!=row[5]:raise ValueError('native retention invalidated coverage')
    if digest(row[4])!=row[6] or digest(encoded([row[0],row[1],row[2],row[3],row[5],row[6]]))!=row[7]:
        raise ValueError('corrupt collection coverage')
    proof=json.loads(row[4]);proof['complete_through_ts']=row[2]
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
        if receipt.decision_ts!=item[2] or receipt.registry_revision!=row[3]:
            raise ValueError('historical decision binding mismatch')
        key=(item[1],item[2])
        if key in decisions and decisions[key]!=receipt:raise ValueError('ambiguous historical decision clock')
        decisions[key]=receipt
        from training_budget import TRAINING_BUDGET
        TRAINING_BUDGET.checkpoint('universal_collection_read',thread_name='adaptive-ai-index-universal-data')
    return NativeDatasetSource.create(agent_ids=ids,decisions=decisions,
        epochs=[RegistryEpoch(topology[0],topology[0],semantic)],coverage=coverage,
        as_of_ts=through,evidence='offline')
