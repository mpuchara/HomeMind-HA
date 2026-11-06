"""U05b.4b.1: bounded, detached original-arbiter evidence, never dispatch.

SQLite revisions are checked AFTER the decision in the collector transaction.
This deliberately rejects changes even after the decision but before persistence.
No timestamps are used as a substitute for arbiter consistency.
"""
from collections import OrderedDict
from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3
import os
import threading
import time

from universal_history_collector import bounded_copy, digest, encoded

VERSION = 1
MAX_AGENTS = 256
MAX_ACTIONS = 256
TABLES = ('agents', 'device_explicit_mappings', 'device_resource_state',
          'app_meta', 'agent_candidate_generations')
REVISION_TABLES = TABLES + ('universal_affected_declarations',)


def _sqlite_row_bytes(rows):
    # A detached SQLite row contains scalar values. Account for the batch,
    # each row, and each scalar using the same structural budget as bounded_copy.
    size = 64 + sum(64 * (1 + len(row)) for row in rows)
    for row in rows:
        for value in row:
            if isinstance(value, str):
                size += 4 * len(value)
            elif value is not None and type(value) not in (int, float):
                raise ValueError('non-JSON receipt')
    return size


def initialize(conn):
    from universal_affected_sensors import ensure_schema
    ensure_schema(conn)
    conn.execute('CREATE TABLE IF NOT EXISTS universal_arbiter_revision '
                 '(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
    conn.execute('INSERT OR IGNORE INTO universal_arbiter_revision VALUES(1,0)')
    for table in REVISION_TABLES:
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


class RevisionValidatedArbiterSnapshot:
    """One bounded worker-owned durable baseline. No current RAM or decisions.

    Callers serialize ownership with PredecisionEvidence.preparation_lock. The
    connection permits owner handoff only so shutdown can close it; a different
    worker always invalidates it before use. Every turn verifies native DDL and
    exact revision-trigger bodies in the same transaction as the revision read.
    """
    LIMIT = 8*1024*1024

    def __init__(self, stats):
        self.stats = stats
        self.db = None
        self.identity = None
        self.configs = ()
        self.owner = None
        for name in ('hits', 'misses', 'invalidations', 'rebuilds', 'reused_turns',
                     'materialization_cpu_ms', 'validation_cpu_ms', 'memory_bytes',
                     'cleanup_failures'):
            stats['arbiter_snapshot_cache_'+name if name in ('hits', 'misses')
                  else 'arbiter_snapshot_'+name] = 0

    def close(self):
        db, self.db = self.db, None
        self.identity = None
        self.configs = ()
        self.owner = None
        self.stats['arbiter_snapshot_memory_bytes'] = 0
        if db is not None:
            self.stats['arbiter_snapshot_invalidations'] += 1
            db.close()

    @staticmethod
    def source_identity(c, path, epoch):
        rev = revision(c)
        if type(rev) is not int or rev < 0:
            raise ValueError('invalid arbiter revision')
        schema_version = c.execute('PRAGMA schema_version').fetchone()[0]
        names = list(REVISION_TABLES) + ['universal_arbiter_revision'] + [
            f'uc_revision_{table}_{op}' for table in REVISION_TABLES
            for op in ('INSERT', 'UPDATE', 'DELETE')]
        rows = {row[0]: (row[1], row[2]) for row in c.execute(
            'SELECT name,type,CASE WHEN length(sql)<=65536 THEN sql ELSE NULL END '
            f'FROM sqlite_master WHERE name IN ({",".join("?" for _ in names)})', names)}
        definitions = []
        for table in REVISION_TABLES + ('universal_arbiter_revision',):
            if table not in rows or rows[table][0] != 'table' or not rows[table][1]:
                raise ValueError('missing arbiter table: '+table)
            definitions.append((table, rows[table][1]))
        for table in REVISION_TABLES:
            for op in ('INSERT', 'UPDATE', 'DELETE'):
                name = f'uc_revision_{table}_{op}'
                expected = (f'CREATE TRIGGER {name} AFTER {op} ON {table} BEGIN '
                            'UPDATE universal_arbiter_revision SET revision=revision+1 WHERE id=1; END')
                actual = rows.get(name)
                if (actual is None or actual[0] != 'trigger' or not actual[1] or
                        ''.join(actual[1].split()) != ''.join(expected.split())):
                    raise ValueError('incomplete arbiter revision contract')
                definitions.append((name, actual[1]))
        stat = os.stat(path)
        return (stat.st_dev, stat.st_ino, rev, schema_version, tuple(definitions), epoch)

    def acquire(self, c, store, epoch):
        started = time.thread_time()
        try:
            identity = self.source_identity(c, store.path, epoch)
        except BaseException:
            self.close()
            raise
        finally:
            self.stats['arbiter_snapshot_validation_cpu_ms'] += (time.thread_time()-started)*1000
        if self.db is not None and (self.identity != identity or self.owner != threading.get_ident()):
            self.close()
        if self.db is not None:
            if self.db.in_transaction:
                self.close()
                raise ValueError('arbiter snapshot retained transaction')
            self.stats['arbiter_snapshot_cache_hits'] += 1
            self.stats['arbiter_snapshot_reused_turns'] += 1
            return self.db, self.configs, identity[2]
        self.stats['arbiter_snapshot_cache_misses'] += 1
        started = time.thread_time()
        db = None
        try:
            count = c.execute('SELECT COUNT(*) FROM (SELECT 1 FROM agents LIMIT ?)',
                              (MAX_AGENTS+1,)).fetchone()[0]
            if count > MAX_AGENTS:
                raise ValueError('agent bound')
            db = sqlite3.connect(':memory:', check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA cache_size=-1024')
            db.execute('PRAGMA max_page_count=1024')
            copied_bytes, configs = 0, []
            definitions = dict(identity[4])
            for table in TABLES:
                if table != 'agents':
                    db.execute(definitions[table])
                cursor = c.execute(f'SELECT * FROM {table} LIMIT 10001')
                count = 0
                while True:
                    rows = cursor.fetchmany(16)
                    if not rows:
                        break
                    count += len(rows)
                    copied_bytes += _sqlite_row_bytes(rows)
                    if count > 10000 or copied_bytes > 4*1024*1024:
                        raise ValueError('arbiter table bound')
                    if table == 'agents':
                        configs.extend(store._agent_dict(row) for row in rows)
                    else:
                        db.executemany(f'INSERT INTO {table} VALUES({",".join("?" for _ in rows[0])})', rows)
            db.commit()
            # SQLite pages plus conservative Python/source structural budget;
            # include a fixed connection/cache reserve, not just Python objects.
            memory = (db.execute('PRAGMA page_count').fetchone()[0] *
                      db.execute('PRAGMA page_size').fetchone()[0] + copied_bytes + 1024*1024)
            if memory > self.LIMIT:
                raise ValueError('arbiter snapshot memory bound')
            self.db, self.configs, self.identity = db, configs, identity
            self.owner = threading.get_ident()
            self.stats['arbiter_snapshot_memory_bytes'] = memory
            self.stats['arbiter_snapshot_rebuilds'] += 1
            return db, configs, identity[2]
        except BaseException:
            if db is not None:
                db.close()
            self.close()
            raise
        finally:
            self.stats['arbiter_snapshot_materialization_cpu_ms'] += (time.thread_time()-started)*1000

    def rollback(self):
        try:
            self.db.execute('ROLLBACK TO predecision_evaluation')
            self.db.execute('RELEASE predecision_evaluation')
            if self.db.in_transaction:
                raise ValueError('arbiter cleanup retained transaction')
        except BaseException:
            self.stats['arbiter_snapshot_cleanup_failures'] += 1
            self.close()
            raise


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
        self.preparation_lock = threading.Lock()
        self.arbiter_snapshot = RevisionValidatedArbiterSnapshot(self.stats)
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
        bound = dict(evidence, decision_id=intent.intent_id,
                    decision_ts=intent.created_at, inference_started_ts=started,
                    policy_version=intent.policy_version, model_revision=intent.model_revision,
                    policy_action_values=list(values))
        if 'affected_contract_version' in bound:
            from universal_affected_sensors import bind
            bound['affected_sensor_evidence']=bind(evidence['affected_sensor_evidence'],bound)
        return bound

    def close(self):
        with self.preparation_lock:
            self.arbiter_snapshot.close()

    def prepare_one(self):
        if not self.preparation_lock.acquire(blocking=False):
            return
        try:
            return self._prepare_one()
        except BaseException:
            self.arbiter_snapshot.close()
            raise
        finally:
            self.preparation_lock.release()

    def _prepare_one(self):
        """At most one target per idle collector turn; no control locks held."""
        from device_agents import DeviceAgentService
        from context import action_values
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
        evaluation_open = False
        try:
            # Native revision, schema and causal inputs share a read transaction.
            # Fresh RAM above is never retained by the durable snapshot cache.
            with self.store.conn() as c:
                c.execute('BEGIN')
                db, configs, rev = self.arbiter_snapshot.acquire(c, self.store, epoch)
                from universal_affected_sensors import latest, topology as affected_topology
                declaration = latest(c, aid)
                affected_scope = affected_topology(c, topology)
                config = next((a for a in configs if a['id'] == aid), None)
                if config is None or any(agent.get(k) != v for k, v in config.items()):
                    raise ValueError('RAM/durable config mismatch')
            # The original service reads Store's RAM-first metadata, not a fresh
            # SQL value. Preserve those exact inputs even if another process has
            # changed durable metadata without refreshing this process's cache.
            with self.store._meta_lock:
                metadata = {prefix + str(a['id']): self.store._meta_cache.get(prefix + str(a['id']))
                            for a in configs for prefix in ('manual_hold:', 'manual_hold_source:')}
            db.execute('SAVEPOINT predecision_evaluation')
            evaluation_open = True
            store = SnapshotStore(db, configs, metadata)
            context = SimpleNamespace(resolved_registry=lambda: snapshot['entities'], devices=snapshot['devices'])
            service = DeviceAgentService.__new__(DeviceAgentService)
            service.store = store
            service.engine = SimpleNamespace(context=context, state_map=snapshot['states'])
            state = snapshot['states'].get(agent['target_entity'])
            desc = service.descriptor(agent)
            # The original mask asks for this descriptor again through both
            # legal_action_mask and control_eligibility. All of its inputs are
            # private to this turn: full RAM snapshot, configs and mapping DB.
            # Resource/manual-hold writes by the mask do not alter descriptors.
            # No cache escapes the detached service or reaches the live arbiter.
            original_descriptor = service.descriptor

            def detached_descriptor(candidate):
                return desc if candidate == agent else original_descriptor(candidate)

            service.descriptor = detached_descriptor
            try:
                at = time.time()
                mask = service.legal_action_mask(agent, state, values,
                    runtime_by_agent={a: {'manual_override_until': h} for a, h in snapshot['holds'].items()}, now=at)
            finally:
                # The closure owns a bound original method. Drop the instance
                # override even on failure so it cannot retain the entire RAM
                # snapshot through service -> closure -> bound method -> service.
                del service.descriptor
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
                arbiter_schema_version=self.arbiter_snapshot.identity[3],
                registry_revision=registry_revision, epoch=epoch, topology=topology,
                related_states={eid: snapshot['states'].get(eid) for eid in desc['entities']},
                manual_metadata=metadata,
                holds=snapshot['holds'], created_ts=at, available_ts=time.time(), valid_until_ts=min(deadlines),
                mask_contract='DeviceAgent.legal_action_mask/0.14.232')
            from universal_affected_sensors import prepare
            affected, affected_status=prepare(declaration,evidence,affected_scope)
            evidence.update(affected_contract_version=1,affected_sensor_evidence=affected,
                            affected_sensor_status=affected_status)
            evidence, size = bounded_copy(evidence)
            # Never publish until temporary manual/resource writes are gone.
            self.arbiter_snapshot.rollback()
            evaluation_open = False
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
            if evaluation_open and self.arbiter_snapshot.db is not None:
                self.arbiter_snapshot.rollback()


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
    if ('arbiter_schema_version' in evidence and
            conn.execute('PRAGMA schema_version').fetchone()[0] != evidence['arbiter_schema_version']):
        raise ValueError('arbiter schema changed before evidence persistence')
    # Equal revisions extend the immutable snapshot's validity through this
    # transaction (which inserts the receipt). Later revisions do not revoke a
    # correctly persisted historical assertion.
    return dict(evidence, verified_revision=evidence['arbiter_revision'])
