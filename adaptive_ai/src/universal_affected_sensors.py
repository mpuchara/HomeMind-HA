"""Scoped operator assertions, not inferred physical causality or dispatch authority.

Declarations are append-only. Missing/partial/automatic hypotheses never stand
for an empty complete set. Only the existing predecision worker consumes them.
"""
import json
import math
import time
import uuid

from universal_history_collector import bounded_copy, digest, encoded, sensor

VERSION = 1
TABLE = 'universal_affected_declarations'
SCOPE = 'collector_sensor_universe'
TRUST = 'operator_attested_scoped'


def ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE}
        (seq INTEGER PRIMARY KEY AUTOINCREMENT, revision TEXT UNIQUE NOT NULL,
         agent_id TEXT NOT NULL, body TEXT NOT NULL, checksum TEXT NOT NULL)''')
    conn.execute(f'CREATE INDEX IF NOT EXISTS idx_affected_agent_seq ON {TABLE}(agent_id,seq DESC)')


def topology(conn, revision):
    row = conn.execute('SELECT at,body,checksum FROM universal_collection_topology WHERE id=?', (revision,)).fetchone()
    if row is None or digest(row[1]) != row[2] or row[2] != revision:
        raise ValueError('affected_topology_unavailable_or_corrupt')
    doc = json.loads(row[1])
    entities = {r['entity_id']: r for r in doc['registries']['entity']}
    universe = sorted(eid for eid,r in entities.items() if sensor(eid) and not r.get('disabled_by'))
    return row[0], universe, entities


def latest(conn, agent_id):
    row = conn.execute(f'''SELECT revision,CASE WHEN length(CAST(body AS BLOB))<=262144 THEN body ELSE NULL END,checksum
        FROM {TABLE} WHERE agent_id=? ORDER BY seq DESC LIMIT 1''', (agent_id,)).fetchone()
    return None if row is None else tuple(row)


class AffectedSensorDeclarations:
    """Explicit local operator API. Does not enable collection or edit agents.

    Call declare(..., affected_sensor_ids=None, complete=False) to withdraw an
    assertion. Older rows remain immutable, but cannot be selected as fallback.
    Caller owns authorization to declare; approved_by is attribution, not auth.
    """
    def __init__(self, store):
        self.store = store
        from universal_predecision import initialize
        with store.conn() as conn:
            initialize(conn)

    def declare(self, agent_id, *, logical_device_id, topology_revision,
                affected_sensor_ids, complete, approved_by, rationale,
                empty_reason=None, scope=SCOPE, evidence_source='operator_declaration'):
        if type(complete) is not bool or scope != SCOPE:
            raise ValueError('affected_invalid_scope_or_completeness')
        for text in (logical_device_id, approved_by, rationale):
            if not isinstance(text,str) or not text.strip() or len(text)>1024:
                raise ValueError('affected_attribution_required')
        if evidence_source not in ('operator_declaration','automatic_hypothesis'):
            raise ValueError('affected_unknown_source')
        if evidence_source != 'operator_declaration' and complete:
            raise ValueError('affected_hypothesis_cannot_attest_completeness')
        ids = None
        if affected_sensor_ids is not None:
            if not isinstance(affected_sensor_ids,(list,tuple)) or len(affected_sensor_ids)>10000:
                raise ValueError('affected_sensor_bound')
            if any(not isinstance(e,str) or not sensor(e) for e in affected_sensor_ids):
                raise ValueError('affected_invalid_sensor')
            ids = sorted(set(affected_sensor_ids))
        if complete and ids is None:
            raise ValueError('affected_unknown_is_not_complete')
        if ids == [] and (not complete or not isinstance(empty_reason,str) or not empty_reason.strip()):
            raise ValueError('affected_empty_requires_explicit_attestation')
        if empty_reason is not None and (not isinstance(empty_reason,str) or len(empty_reason)>2048):
            raise ValueError('affected_empty_reason_bound')
        with self.store.conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM agents WHERE id=?',(str(agent_id),)).fetchone()
            if row is None: raise ValueError('affected_unknown_agent')
            agent = self.store._agent_dict(row)
            generation = conn.execute('SELECT generation_id FROM agent_candidate_generations WHERE agent_id=?',(str(agent_id),)).fetchone()
            if generation is None: raise ValueError('affected_generation_missing')
            topo_at, universe, entities = topology(conn,topology_revision)
            if agent['target_entity'] not in entities or (ids is not None and not set(ids)<=set(universe)):
                raise ValueError('affected_unknown_target_or_sensors')
            now = time.time()
            if topo_at > now: raise ValueError('affected_future_topology')
            body = dict(contract_version=VERSION, agent_id=str(agent_id), generation_id=generation[0],
                target_entity=agent['target_entity'],target_property=agent['target_property'],
                logical_device_id=logical_device_id, config_revision=digest(encoded(agent)),
                configuration_information_ts=now, topology_revision=topology_revision,
                topology_information_ts=topo_at,sensor_universe_revision=digest(encoded(universe)),
                sensor_universe_count=len(universe),
                affected_sensor_ids=ids, complete=complete, scope=scope,
                evidence_source=evidence_source, trust_level=TRUST if evidence_source=='operator_declaration' else 'unconfirmed_hypothesis',
                approved_by=approved_by, approved_ts=now if evidence_source=='operator_declaration' else None,
                recorded_ts=now, rationale=rationale,empty_reason=empty_reason,nonce=uuid.uuid4().hex)
            bounded_copy(body)
            raw=encoded(body); revision=digest(raw)
            conn.execute(f'INSERT INTO {TABLE}(revision,agent_id,body,checksum) VALUES(?,?,?,?)',
                         (revision,str(agent_id),raw,revision))
        return revision


def validate_declaration(row):
    revision, raw, checksum = row
    if not revision: raise ValueError('affected_revision_missing')
    if not isinstance(raw,str) or len(raw.encode())>262144:
        raise ValueError('affected_declaration_size_bound')
    if checksum != digest(raw) or revision != checksum:
        raise ValueError('affected_checksum_mismatch')
    doc=json.loads(raw)
    if type(doc['contract_version']) is not int or doc['contract_version'] != VERSION: raise ValueError('affected_unsupported_contract')
    ids=doc['affected_sensor_ids']
    if (type(doc['sensor_universe_count']) is not int or not 0<=doc['sensor_universe_count']<=10000
            or not isinstance(doc['sensor_universe_revision'],str) or len(doc['sensor_universe_revision'])!=64):
        raise ValueError('affected_invalid_sensor_universe')
    if ids is not None and (not isinstance(ids,list) or len(ids)>10000 or any(not isinstance(e,str) or not sensor(e) for e in ids)
                            or ids!=sorted(set(ids))):
        raise ValueError('affected_unknown_sensors')
    if doc['scope'] != SCOPE: raise ValueError('affected_scope_mismatch')
    if type(doc['complete']) is not bool: raise ValueError('affected_invalid_completeness')
    if doc['complete']:
        if doc['evidence_source']!='operator_declaration' or doc['trust_level']!=TRUST or not doc['approved_by'] or not doc['rationale']:
            raise ValueError('affected_unconfirmed_completeness')
        if ids is None: raise ValueError('affected_unknown_is_not_complete')
        if not ids and not (isinstance(doc['empty_reason'],str) and doc['empty_reason'].strip()):
            raise ValueError('affected_empty_requires_explicit_attestation')
    for key in ('configuration_information_ts','topology_information_ts','recorded_ts'):
        value=doc[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('affected_invalid_clock')
    if doc['complete'] and (not isinstance(doc['approved_ts'],(int,float)) or isinstance(doc['approved_ts'],bool)
                            or not math.isfinite(doc['approved_ts']) or doc['approved_ts']<=0):
        raise ValueError('affected_invalid_approval_clock')
    return doc


def prepare(row, parent, topo):
    """Consume the SAME SQLite snapshot as the original arbiter. No later lookup."""
    if row is None: return None, 'affected_declaration_missing'
    try:
        doc=validate_declaration(row)
        topo_at, universe, entities=topo
        if (doc['topology_revision']!=parent['topology'] or doc['sensor_universe_revision']!=digest(encoded(universe))
                or doc['sensor_universe_count']!=len(universe) or doc['topology_information_ts']!=topo_at):
            raise ValueError('affected_topology_mismatch')
        if doc['affected_sensor_ids'] is not None and not set(doc['affected_sensor_ids'])<=set(universe):
            raise ValueError('affected_unknown_sensors')
        agent=parent['agent']
        if (doc['agent_id']!=agent['id'] or doc['generation_id']!=parent['generation_id']
                or doc['target_entity']!=agent['target_entity'] or doc['target_property']!=agent['target_property']
                or doc['logical_device_id']!=parent['descriptor']['logical_device_id']
                or doc['config_revision']!=parent['config_revision']):
            raise ValueError('affected_identity_or_configuration_mismatch')
        if any(doc[k]>parent['created_ts'] for k in ('recorded_ts','configuration_information_ts','topology_information_ts')):
            raise ValueError('affected_future_information')
        if doc['complete'] and doc['approved_ts']>parent['created_ts']:
            raise ValueError('affected_future_approval')
        evidence=dict(contract_version=VERSION,declaration_revision=row[0],declaration_json=row[1],
            declaration_checksum=row[2], affected_sensor_ids=doc['affected_sensor_ids'],complete=doc['complete'],
            information_available_ts=parent['available_ts'],registry_revision=parent['registry_revision'],
            collector_epoch=parent['epoch'])
        return evidence, 'complete' if doc['complete'] else 'affected_declaration_incomplete'
    except (ValueError,KeyError,TypeError) as exc:
        return None, str(exc)


def bind(evidence, parent):
    if evidence is None:return None
    return dict(evidence,decision_id=parent['decision_id'],decision_ts=parent['decision_ts'],
                generation_id=parent['generation_id'],agent_id=parent['agent']['id'])


def validate(evidence, parent, *, sensor_universe=None,topology_information_ts=None,
             universe_revision=None,universe_count=None):
    if evidence is None: raise ValueError(parent.get('affected_sensor_status') or 'affected_declaration_missing')
    if type(evidence['contract_version']) is not int or evidence['contract_version']!=VERSION: raise ValueError('affected_unsupported_contract')
    doc=validate_declaration((evidence['declaration_revision'],evidence['declaration_json'],evidence['declaration_checksum']))
    if not doc['complete'] or evidence['complete'] is not True:
        raise ValueError('affected_declaration_incomplete')
    if evidence['affected_sensor_ids']!=doc['affected_sensor_ids']:
        raise ValueError('affected_sensor_set_mismatch')
    if (evidence['agent_id']!=parent['agent']['id'] or doc['agent_id']!=evidence['agent_id']
            or evidence['generation_id']!=parent['generation_id'] or doc['generation_id']!=parent['generation_id']
            or evidence['decision_id']!=parent['decision_id'] or evidence['decision_ts']!=parent['decision_ts']
            or evidence['registry_revision']!=parent['registry_revision'] or evidence['collector_epoch']!=parent['epoch']
            or doc['topology_revision']!=parent['topology'] or doc['config_revision']!=parent['config_revision']
            or doc['logical_device_id']!=parent['descriptor']['logical_device_id']
            or doc['target_entity']!=parent['agent']['target_entity'] or doc['target_property']!=parent['agent']['target_property']):
        raise ValueError('affected_decision_binding_mismatch')
    at=parent['inference_started_ts']
    available=evidence['information_available_ts']
    if (isinstance(available,bool) or not isinstance(available,(int,float)) or not math.isfinite(available)
            or not parent['created_ts']<=available<=parent['available_ts']<=at
            or any(doc[k]>available for k in ('recorded_ts','approved_ts','configuration_information_ts','topology_information_ts'))):
        raise ValueError('affected_future_information')
    if sensor_universe is not None:
        universe=sorted(set(sensor_universe))
        if doc['sensor_universe_count']!=len(universe) or doc['sensor_universe_revision']!=digest(encoded(universe)):
            raise ValueError('affected_historical_sensor_universe_mismatch')
        if not set(doc['affected_sensor_ids'])<=set(universe):
            raise ValueError('affected_unknown_sensors')
    if topology_information_ts is not None and doc['topology_information_ts']!=topology_information_ts:
        raise ValueError('affected_topology_information_mismatch')
    if universe_revision is not None and (doc['sensor_universe_revision']!=universe_revision or doc['sensor_universe_count']!=universe_count):
        raise ValueError('affected_historical_sensor_universe_mismatch')
    return tuple(doc['affected_sensor_ids'])
