"""U05b.4b.1: bounded, detached original-arbiter evidence, never dispatch.

SQLite revisions are checked AFTER the decision in the collector transaction.
This deliberately rejects changes even after the decision but before persistence.
No timestamps are used as a substitute for arbiter consistency.
"""
from collections import OrderedDict
from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3
import threading
import time

from universal_history_collector import bounded_copy, digest, encoded

VERSION = 1
MAX_AGENTS = 256
MAX_ACTIONS = 256
TABLES = ('agents', 'device_explicit_mappings', 'device_resource_state',
          'app_meta', 'agent_candidate_generations')


def initialize(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS universal_arbiter_revision '
                 '(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
    conn.execute('INSERT OR IGNORE INTO universal_arbiter_revision VALUES(1,0)')
    for table in TABLES:
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone():
            continue
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS uc_revision_{table}_{operation}
                AFTER {operation} ON {table} BEGIN
                UPDATE universal_arbiter_revision SET revision=revision+1 WHERE id=1; END''')


def revision(conn):
    return conn.execute('SELECT revision FROM universal_arbiter_revision WHERE id=1').fetchone()[0]


def holds(engine):
    if len(engine.runtime) > MAX_AGENTS:
        raise ValueError('runtime bound')
    return {str(aid): float(rt.get('manual_override_until') or 0.)
            for aid, rt in engine.runtime.items() if rt.get('manual_override_until')}


class SnapshotStore:
    """Only the isolated in-memory database can be changed by the original mask.

    In particular _persist_runtime_manual_holds may write this copy. Calling the
    original arbiter on the live store would change production arbitration.
    """
    def __init__(self, conn, configs, metadata):
        self.db, self.configs, self.metadata = conn, configs, metadata
        self.lock = threading.RLock()

    @contextmanager
    def conn(self):
        yield self.db

    def list_agent_configs(self):
        return self.configs

    def meta_get(self, key, default=None):
        return self.metadata.get(key, default)


class PredecisionEvidence:
    def __init__(self, engine, store, collector):
        self.engine, self.store, self.collector = engine, store, collector
        self.pending = OrderedDict()
        self.prepared = OrderedDict()
        self.lock = threading.Lock()
        self.prepared_bytes = 0
        self.stats = dict(requested=0, prepared=0, consumed=0, rejected=0, errors=0, unsupported=0)
        with store.conn() as c:
            initialize(c)

    def request(self, agent_id):
        with self.lock:
            if len(self.pending) >= MAX_AGENTS and agent_id not in self.pending:
                return
            self.pending[agent_id] = None
            self.stats['requested'] += 1

    def _matches(self, evidence, agent, state, values):
        engine = self.engine
        return (evidence['agent'] == agent and evidence['state'] == state
                and time.time() < evidence['valid_until_ts']
                and evidence['values'] == list(values)
                and evidence['registry_revision'] == engine.context.registry_revision
                and evidence['holds'] == holds(engine)
                and evidence['epoch'] == self.collector.epoch
                and all(self.store._meta_cache.get(key) == value
                        for key, value in evidence['manual_metadata'].items())
                and all(engine.state_map.get(eid) == st
                        for eid, st in evidence['related_states'].items()))

    def begin(self, agent, state, values):
        """Called before predict: RAM comparisons only, no arbiter/SQL/wait."""
        with self.lock:
            evidence = self.prepared.get(agent['id'])
        if evidence is None:
            self.request(agent['id'])
            return None
        if not self._matches(evidence, agent, state, values):
            self.request(agent['id'])
            self.stats['rejected'] += 1
            return None
        # Never move the mask or availability clocks. This clock is a distinct
        # original pre-predict boundary, not a reconstructed historical time.
        started = time.time()
        if evidence['available_ts'] > started:
            return None
        return evidence, started

    def bind(self, ticket, intent, agent, state, values):
        if ticket is None:
            return None
        evidence, started = ticket
        if not started <= intent.created_at or not self._matches(evidence, agent, state, values):
            self.stats['rejected'] += 1
            return None
        self.stats['consumed'] += 1
        return dict(evidence, decision_id=intent.intent_id,
                    decision_ts=intent.created_at, inference_started_ts=started,
                    policy_version=intent.policy_version, model_revision=intent.model_revision,
                    policy_action_values=list(values))

    def prepare_one(self):
        """At most one target per idle collector turn; no control locks held."""
        from device_agents import DeviceAgentService
        from context import action_values
        from training_budget import TRAINING_BUDGET
        TRAINING_BUDGET.checkpoint('universal_predecision_prepare',thread_name='adaptive-ai-index-universal-scorer')
        with self.lock:
            if not self.pending:
                return
            aid, _ = self.pending.popitem(last=False)
        engine = self.engine
        # Never wait behind foreground engine work.
        if not engine.lock.acquire(blocking=False):
            self.request(aid)
            return
        context_lock = getattr(engine.context, 'lock', None)
        if context_lock is not None and not context_lock.acquire(blocking=False):
            engine.lock.release()
            self.request(aid)
            return
        try:
            agent = engine.agent_configs.get(aid)
            if not agent:
                return
            snapshot, _ = bounded_copy(dict(agent=agent, states=engine.state_map,
                entities=engine.context.resolved_registry(), devices=engine.context.devices,
                holds=holds(engine)), limit=4*1024*1024)
            registry_revision = engine.context.registry_revision
            epoch = self.collector.epoch
            topology = self.collector.topology
            if topology is None:
                return
        finally:
            if context_lock is not None:
                context_lock.release()
            engine.lock.release()
        agent = snapshot['agent']
        from control import REVIEW_REQUIRED_DOMAINS
        if str(agent['target_entity']).partition('.')[0] in REVIEW_REQUIRED_DOMAINS:
            # legal_value's generic-target review reads global storage.STORE.
            # Until it has a detached-input contract, never let that live read
            # masquerade as an immutable original-arbiter evaluation.
            self.stats['unsupported'] += 1
            return
        if agent.get('target_property') == 'option_index' and float(agent['max_value'])-float(agent['min_value']) > MAX_ACTIONS-1:
            return
        values = action_values(agent)
        if not 1 <= len(values) <= MAX_ACTIONS:
            return
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            # One native read snapshot; triggers catch writes from other Store
            # instances/processes too. Only small arbiter tables are copied.
            with self.store.conn() as c:
                c.execute('BEGIN')
                rev = revision(c)
                expected = {f'uc_revision_{table}_{op}' for table in TABLES for op in ('INSERT','UPDATE','DELETE')}
                installed = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'uc_revision_%'")}
                if not expected <= installed:
                    raise ValueError('incomplete arbiter revision contract')
                copied_bytes = 0
                for table in TABLES:
                    schema = c.execute('SELECT sql FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone()
                    if schema is None:
                        raise ValueError('missing arbiter table: '+table)
                    db.execute(schema[0])
                    cursor = c.execute(f'SELECT * FROM {table} LIMIT 10001')
                    count = 0
                    while True:
                        rows = cursor.fetchmany(16)
                        if not rows:
                            break
                        count += len(rows)
                        rows, size = bounded_copy([tuple(r) for r in rows],limit=4*1024*1024)
                        copied_bytes += size
                        if count > 10000 or copied_bytes > 4*1024*1024:
                            raise ValueError('arbiter table bound')
                        db.executemany(f'INSERT INTO {table} VALUES({",".join("?" for _ in rows[0])})', rows)
                configs = [self.store._agent_dict(r) for r in db.execute('SELECT * FROM agents')]
                if len(configs) > MAX_AGENTS:
                    raise ValueError('agent bound')
                config = next((a for a in configs if a['id'] == aid), None)
                if config is None or any(agent.get(k) != v for k, v in config.items()):
                    raise ValueError('RAM/durable config mismatch')
            # The original service reads Store's RAM-first metadata, not a fresh
            # SQL value. Preserve those exact inputs even if another process has
            # changed durable metadata without refreshing this process's cache.
            with self.store._meta_lock:
                metadata = {prefix + str(a['id']): self.store._meta_cache.get(prefix + str(a['id']))
                            for a in configs for prefix in ('manual_hold:', 'manual_hold_source:')}
            store = SnapshotStore(db, configs, metadata)
            context = SimpleNamespace(resolved_registry=lambda: snapshot['entities'], devices=snapshot['devices'])
            service = DeviceAgentService.__new__(DeviceAgentService)
            service.store = store
            service.engine = SimpleNamespace(context=context, state_map=snapshot['states'])
            state = snapshot['states'].get(agent['target_entity'])
            desc = service.descriptor(agent)
            at = time.time()
            mask = service.legal_action_mask(agent, state, values,
                runtime_by_agent={a: {'manual_override_until': h} for a, h in snapshot['holds'].items()}, now=at)
            generation = db.execute('SELECT generation_id FROM agent_candidate_generations WHERE agent_id=?', (aid,)).fetchone()
            if generation is None:
                raise ValueError('missing generation')
            deadlines = [at+5.]
            for row in service._resource_rows(desc['resource_keys']).values():
                for deadline in (float(row.get('lease_until') or 0.), float(row.get('manual_hold_until') or 0.),
                                 float(row.get('last_dispatch_ts') or 0.) + float(mask['min_dwell_seconds'])):
                    if deadline > at:
                        deadlines.append(deadline)
            deadlines.extend(h for h in snapshot['holds'].values() if h > at)
            evidence = dict(version=VERSION, agent=agent, state=state, descriptor=desc,
                legal_mask=mask, values=values, generation_id=generation[0],
                config_revision=digest(encoded(agent)), arbiter_revision=rev,
                registry_revision=registry_revision, epoch=epoch, topology=topology,
                related_states={eid: snapshot['states'].get(eid) for eid in desc['entities']},
                manual_metadata=metadata,
                holds=snapshot['holds'], created_ts=at, available_ts=time.time(), valid_until_ts=min(deadlines),
                mask_contract='DeviceAgent.legal_action_mask/0.14.232')
            evidence, size = bounded_copy(evidence)
            with self.lock:
                old = self.prepared.pop(aid, None)
                if old is not None:
                    self.prepared_bytes -= old['_bytes']
                evidence['_bytes'] = size
                while self.prepared and (len(self.prepared) >= MAX_AGENTS or self.prepared_bytes + size > 8*1024*1024):
                    _, evicted = self.prepared.popitem(last=False)
                    self.prepared_bytes -= evicted['_bytes']
                self.prepared[aid] = evidence
                self.prepared_bytes += size
                self.stats['prepared'] += 1
        finally:
            db.close()


def validate_binding(evidence, intent, agent, state):
    from control import REVIEW_REQUIRED_DOMAINS
    if str(agent['target_entity']).partition('.')[0] in REVIEW_REQUIRED_DOMAINS:
        raise ValueError('generic target review lacks detached evidence contract')
    if evidence['version'] != VERSION or evidence['mask_contract'] != 'DeviceAgent.legal_action_mask/0.14.232':
        raise ValueError('unsupported predecision contract')
    if not (evidence['created_ts'] <= evidence['available_ts'] <=
            evidence['inference_started_ts'] <= evidence['decision_ts'] == intent['created_at']
            < evidence['valid_until_ts']):
        raise ValueError('predecision clocks')
    if (evidence['decision_id'] != intent['intent_id'] or evidence['agent'] != agent
            or evidence['state'] != state or agent['id'] != intent['agent_id']
            or agent['target_entity'] != intent['target_entity']
            or agent['target_property'] != intent['target_property']
            or evidence['policy_version'] != intent['policy_version']
            or evidence['model_revision'] != intent['model_revision']
            or evidence['config_revision'] != digest(encoded(agent))
            or not evidence['generation_id']):
        raise ValueError('predecision identity binding')
    values = evidence['values']
    if (not 1 <= len(values) <= MAX_ACTIONS or values != evidence['policy_action_values']
            or values != [r['requested'] for r in evidence['legal_mask']['actions']]):
        raise ValueError('incomplete predecision action set')
    from universal_actions import from_device_agent_mask
    from_device_agent_mask(agent,evidence['descriptor'],evidence['legal_mask'],state,
                           mask_timestamp=evidence['created_ts'])


def verify_for_write(conn, evidence, intent, agent, state):
    validate_binding(evidence, intent, agent, state)
    if revision(conn) != evidence['arbiter_revision']:
        raise ValueError('arbiter revision changed before evidence persistence')
    # Equal revisions extend the immutable snapshot's validity through this
    # transaction (which inserts the receipt). Later revisions do not revoke a
    # correctly persisted historical assertion.
    return dict(evidence, verified_revision=evidence['arbiter_revision'])
