"""V3 native retention ledger: bounded metadata, never a second sensor history.

One indexed damage envelope per entity is deliberately conservative if multiple
disjoint mutations are merged. Triggers execute in the observation transaction,
including another process and a boot with collection disabled.
"""
import json
import math
import uuid

VERSION = 3
MAX_ENTITIES = 10000
TRIGGERS = ('ur_preinsert','ur_insert','ur_delete','ur_update','ur_ledger_delete','ur_ledger_shrink')


def _damage(entity, stamp, condition='1'):
    return f'''
        UPDATE universal_retention_meta SET revision=revision+1,
          new_entity=NOT EXISTS(SELECT 1 FROM universal_retention_entities WHERE entity_id={entity}) WHERE id=1 AND ({condition});
        INSERT INTO universal_retention_entities(entity_id,lo,hi,revision)
          SELECT {entity},{stamp},{stamp},revision FROM universal_retention_meta
          WHERE id=1 AND ({condition}) AND (entity_count<{MAX_ENTITIES} OR EXISTS(
            SELECT 1 FROM universal_retention_entities WHERE entity_id={entity}))
          ON CONFLICT(entity_id) DO UPDATE SET lo=MIN(lo,excluded.lo),hi=MAX(hi,excluded.hi),revision=excluded.revision;
        UPDATE universal_retention_meta SET entity_count=entity_count+CASE WHEN new_entity=1 AND EXISTS(
            SELECT 1 FROM universal_retention_entities WHERE entity_id={entity}) THEN 1 ELSE 0 END,
          floor_revision=CASE WHEN NOT EXISTS(SELECT 1 FROM universal_retention_entities WHERE entity_id={entity})
            THEN revision ELSE floor_revision END WHERE id=1 AND ({condition});
        UPDATE universal_collection_meta SET retention_epoch=retention_epoch+1 WHERE id=1 AND ({condition});
    '''


def definitions():
    fields=('event_key','contract_version','entity_id','event_time','received_time','state',
            'attributes_json','last_changed','last_updated','source','quality')
    changed=' OR '.join(f'OLD.{k} IS NOT NEW.{k}' for k in fields)
    conflict='EXISTS(SELECT 1 FROM feature_observation_events o WHERE o.event_key=NEW.event_key AND ('+' OR '.join(f'o.{k} IS NOT NEW.{k}' for k in fields)+'))'
    old_entity='(SELECT entity_id FROM feature_observation_events WHERE event_key=NEW.event_key)'
    old_stamp='(SELECT received_time FROM feature_observation_events WHERE event_key=NEW.event_key)'
    return dict(
        # REPLACE can suppress DELETE triggers on an external default SQLite
        # connection. Observe the original row before conflict handling instead.
        # A conflicting INSERT ... DO NOTHING can conservatively invalidate;
        # an exact duplicate (the native journal UPSERT case) does not.
        ur_preinsert=f'''CREATE TRIGGER ur_preinsert BEFORE INSERT ON feature_observation_events BEGIN
            UPDATE universal_retention_meta SET insert_existing=EXISTS(
                SELECT 1 FROM feature_observation_events WHERE event_key=NEW.event_key) WHERE id=1;
            {_damage(old_entity,old_stamp,conflict)} END''',
        ur_insert=f'''CREATE TRIGGER ur_insert AFTER INSERT ON feature_observation_events BEGIN
            UPDATE universal_retention_meta SET live_count=live_count+1-insert_existing,insert_is_past=(NEW.received_time<=sealed_through) WHERE id=1;
            {_conditional_insert_damage()}
            END''',
        ur_delete=f'''CREATE TRIGGER ur_delete AFTER DELETE ON feature_observation_events BEGIN
            UPDATE universal_retention_meta SET live_count=live_count-1,insert_existing=0 WHERE id=1;
            {_damage('OLD.entity_id','OLD.received_time')} END''',
        ur_update=f'''CREATE TRIGGER ur_update AFTER UPDATE ON feature_observation_events WHEN {changed} BEGIN
            {_damage('OLD.entity_id','OLD.received_time')}
            {_damage('NEW.entity_id','NEW.received_time')} END''',
        ur_ledger_delete='''CREATE TRIGGER ur_ledger_delete AFTER DELETE ON universal_retention_entities BEGIN
            UPDATE universal_retention_meta SET revision=revision+1,floor_revision=revision+1,
              entity_count=MAX(0,entity_count-1) WHERE id=1; END''',
        ur_ledger_shrink='''CREATE TRIGGER ur_ledger_shrink AFTER UPDATE ON universal_retention_entities
            WHEN NEW.entity_id IS NOT OLD.entity_id OR NEW.lo>OLD.lo OR NEW.hi<OLD.hi OR NEW.revision<OLD.revision BEGIN
            UPDATE universal_retention_meta SET revision=revision+1,floor_revision=revision+1 WHERE id=1; END''')


def _conditional_insert_damage():
    # A new, genuinely later receipt cannot change a sealed historical replay.
    # An insertion backdated into attested time is a destructive history change.
    return f'''
        UPDATE universal_retention_meta SET revision=revision+insert_is_past,
          new_entity=NOT EXISTS(SELECT 1 FROM universal_retention_entities WHERE entity_id=NEW.entity_id) WHERE id=1 AND insert_is_past=1;
        INSERT INTO universal_retention_entities(entity_id,lo,hi,revision)
          SELECT NEW.entity_id,NEW.received_time,NEW.received_time,revision FROM universal_retention_meta
          WHERE id=1 AND insert_is_past=1 AND (entity_count<{MAX_ENTITIES} OR EXISTS(
            SELECT 1 FROM universal_retention_entities WHERE entity_id=NEW.entity_id))
          ON CONFLICT(entity_id) DO UPDATE SET lo=MIN(lo,excluded.lo),hi=MAX(hi,excluded.hi),revision=excluded.revision;
        UPDATE universal_retention_meta SET entity_count=entity_count+CASE WHEN new_entity=1 AND EXISTS(
            SELECT 1 FROM universal_retention_entities WHERE entity_id=NEW.entity_id) THEN 1 ELSE 0 END,
          floor_revision=CASE WHEN insert_is_past=1 AND NOT EXISTS(
            SELECT 1 FROM universal_retention_entities WHERE entity_id=NEW.entity_id) THEN revision ELSE floor_revision END
          WHERE id=1 AND insert_is_past=1;
        UPDATE universal_collection_meta SET retention_epoch=retention_epoch+1
          WHERE id=1 AND (SELECT insert_is_past FROM universal_retention_meta WHERE id=1)=1;
    '''


def _normal(sql):
    return ' '.join(sql.lower().split())


def ensure_schema(conn):
    """Only called after FeatureJournal and collector schemas exist."""
    conn.execute('''CREATE TABLE IF NOT EXISTS universal_retention_meta
        (id INTEGER PRIMARY KEY CHECK(id=1),tracker TEXT NOT NULL,revision INTEGER NOT NULL,
         floor_revision INTEGER NOT NULL,sealed_through REAL NOT NULL,entity_count INTEGER NOT NULL,
         live_count INTEGER NOT NULL,insert_is_past INTEGER NOT NULL DEFAULT 0,new_entity INTEGER NOT NULL DEFAULT 0,
         insert_existing INTEGER NOT NULL DEFAULT 0)''')
    if 'insert_existing' not in {r[1] for r in conn.execute('PRAGMA table_info(universal_retention_meta)')}:
        conn.execute('ALTER TABLE universal_retention_meta ADD COLUMN insert_existing INTEGER NOT NULL DEFAULT 0')
    conn.execute('''CREATE TABLE IF NOT EXISTS universal_retention_entities
        (entity_id TEXT PRIMARY KEY,lo REAL NOT NULL,hi REAL NOT NULL,revision INTEGER NOT NULL)''')
    exists=conn.execute('SELECT tracker FROM universal_retention_meta WHERE id=1').fetchone()
    expected=definitions()
    present={r[0]:r[1] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'ur_%'")}
    if exists is None or any(_normal(present.get(k,''))!=_normal(v) for k,v in expected.items()):
        # An observed schema gap burns all earlier V3 authority. Repairing the
        # triggers cannot make a previously unverifiable segment trustworthy.
        conn.execute('DELETE FROM universal_retention_entities')
        count=conn.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
        conn.execute('INSERT OR REPLACE INTO universal_retention_meta VALUES(1,?,0,0,0,0,?,0,0,0)',(uuid.uuid4().hex,count))
        conn.execute('UPDATE universal_collection_meta SET retention_epoch=retention_epoch+1 WHERE id=1')
        for name,sql in expected.items():
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            conn.execute(sql)


def verify_schema(conn):
    expected=definitions()
    actual={r[0]:r[1] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'ur_%'")}
    if any(_normal(actual.get(k,''))!=_normal(v) for k,v in expected.items()):
        raise ValueError('granular retention trigger contract unavailable')
    row=conn.execute('SELECT tracker,revision,floor_revision,sealed_through FROM universal_retention_meta WHERE id=1').fetchone()
    if row is None:raise ValueError('granular retention metadata unavailable')
    return tuple(row)


def origin(conn):
    tracker,rev,_,_=verify_schema(conn)
    return tracker,rev


def seal(conn, through):
    conn.execute('UPDATE universal_retention_meta SET sealed_through=MAX(sealed_through,?) WHERE id=1',(through,))


def _checksum(doc):
    from universal_history_collector import digest,encoded
    return digest(encoded(doc))


def make_witness(conn, segment, proof, *, through, required):
    required=sorted(set(required))
    if not required or len(required)>MAX_ENTITIES or not set(required)<=set(proof['entity_ids']):
        raise ValueError('granular required sensor scope unavailable')
    tracker,rev,_,_=verify_schema(conn)
    if proof.get('retention_tracker')!=tracker:
        raise ValueError('granular retention tracker changed')
    seeds=[s for s in proof['seed_receipts'] if s[0] in set(required)]
    keys=[s for s in proof['seed_event_keys'] if s[0] in set(required)]
    doc=dict(version=VERSION,source_revision=segment,topology_revision=proof['topology_revision'],
        segment_proof_checksum=_checksum(proof),start=proof['start_ts'],through=through,
        required_entity_ids=required,seed_receipts=seeds,seed_event_keys=keys,
        historical_sensor_universe_revision=proof['historical_sensor_universe_revision'],
        historical_sensor_universe_count=proof['historical_sensor_universe_count'],
        retention_tracker=tracker,origin_revision=proof['retention_origin_revision'],
        issued_revision=rev,complete=True)
    doc['checksum']=_checksum(doc)
    verify_damage(conn,doc)
    return doc


def verify_damage(conn, doc):
    tracker,rev,floor,sealed=verify_schema(conn)
    if (doc['version']!=VERSION or doc['complete'] is not True or tracker!=doc['retention_tracker']
            or not floor<=doc['origin_revision']<=doc['issued_revision']<=rev or sealed<doc['through']):
        raise ValueError('granular retention revision unavailable')
    required=doc['required_entity_ids']
    if not 1<=len(required)<=MAX_ENTITIES or required!=sorted(set(required)):
        raise ValueError('granular sensor scope invalid')
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (doc['start'],doc['through'])) or doc['start']>doc['through']:
        raise ValueError('granular interval invalid')
    for offset in range(0,len(required),128):
        chunk=required[offset:offset+128];marks=','.join('?' for _ in chunk)
        if conn.execute(f'''SELECT 1 FROM universal_retention_entities WHERE entity_id IN ({marks})
            AND revision>? AND lo<=? AND hi>=? LIMIT 1''',(*chunk,doc['origin_revision'],doc['through'],doc['start'])).fetchone():
            raise ValueError('granular retention witness required history invalidated')


def verify(conn,coverage):
    from universal_history_collector import encoded,digest
    raw=coverage.granular_witness_json
    if not isinstance(raw,str) or len(raw.encode())>2*1024*1024:
        raise ValueError('granular witness unavailable or oversized')
    doc=json.loads(raw);body=dict(doc);checksum=body.pop('checksum')
    if checksum!=_checksum(body):raise ValueError('corrupt granular witness checksum')
    row=conn.execute('SELECT start,through,topology,proof,retention_epoch,proof_hash,watermark_hash FROM universal_collection_segments WHERE id=?',(coverage.source_revision,)).fetchone()
    if row is None or digest(row[3])!=row[5] or digest(encoded([coverage.source_revision,row[0],row[1],row[2],row[4],row[5]]))!=row[6]:
        raise ValueError('granular original segment unavailable or corrupt')
    proof=json.loads(row[3])
    if (proof.get('version')!=VERSION or _checksum(proof)!=doc['segment_proof_checksum']
            or doc['source_revision']!=coverage.source_revision or doc['topology_revision']!=row[2]
            or not row[0]==doc['start']==coverage.start_ts<=coverage.complete_through_ts==doc['through']<=row[1]
            or frozenset(doc['required_entity_ids'])!=coverage.entity_ids or coverage.complete is not True
            or tuple(tuple(s) for s in doc['seed_receipts'])!=coverage.seed_receipts
            or doc['retention_tracker']!=proof['retention_tracker'] or doc['origin_revision']!=proof['retention_origin_revision']):
        raise ValueError('granular witness binding mismatch')
    if (coverage.topology_revision!=doc['topology_revision'] or coverage.retention_tracker!=doc['retention_tracker']
            or coverage.retention_origin_revision!=doc['origin_revision']
            or coverage.seed_event_keys!=tuple(tuple(s) for s in doc['seed_event_keys'])):
        raise ValueError('granular coverage metadata mismatch')
    from universal_affected_sensors import topology
    _,universe,_=topology(conn,row[2])
    universe_hash=digest(encoded(universe))
    if (doc['historical_sensor_universe_revision']!=universe_hash
            or doc['historical_sensor_universe_count']!=len(universe)
            or coverage.historical_sensor_universe_revision!=universe_hash
            or coverage.historical_sensor_universe_count!=len(universe)
            or not coverage.entity_ids<=set(universe)):
        raise ValueError('granular historical topology universe mismatch')
    if proof['historical_sensor_universe_revision']!=universe_hash or proof['historical_sensor_universe_count']!=len(universe):
        raise ValueError('granular original topology universe mismatch')
    verify_damage(conn,doc)
    # Native key/clocks, not a replacement seed or current HA value.
    keys=doc['seed_event_keys']
    if (len(keys)!=len(coverage.entity_ids) or {s[0] for s in keys}!=set(coverage.entity_ids)
            or {tuple((s[0],s[2],s[3])) for s in keys}!=set(coverage.seed_receipts)
            or keys!=[s for s in proof['seed_event_keys'] if s[0] in coverage.entity_ids]
            or doc['seed_receipts']!=[s for s in proof['seed_receipts'] if s[0] in coverage.entity_ids]):
        raise ValueError('granular seed binding mismatch')
    for offset in range(0,len(keys),128):
        chunk=keys[offset:offset+128]
        marks=','.join('(?,?,?,?)' for _ in chunk)
        row=conn.execute(f'''WITH seeds(entity,key,event,received) AS (VALUES {marks})
            SELECT COUNT(*) FROM seeds s JOIN feature_observation_events o
            ON o.event_key=s.key AND o.entity_id=s.entity AND o.event_time=s.event AND o.received_time=s.received''',
            [value for s in chunk for value in s]).fetchone()
        if row is None or row[0]!=len(chunk):
            raise ValueError('granular original seed unavailable')
    return doc

