"""Durable ingestion boundaries without retaining a WAL reader.

Only the parent installs the small mutation ledger. Workers read payloads through
short autocommit statements. Inserts above the captured rowid boundary belong to
the next training input. Updates/deletes/reused ids in a fenced prefix invalidate
it, including between processes or across a crash. No original payload is changed.
"""
import hashlib
import json
import uuid
from contextlib import contextmanager

ENTITY_PREFIX_TABLES = {'entity_history':'ts', 'feature_observation_events':'event_time',
                        'provenance_events':'event_time', 'provenance_history_links':'event_time'}


@contextmanager
def protect_training_inputs(store):
    """Coordinate routine retention with the supervised child; no held SQLite lock."""
    with store.lock:
        store._training_input_readers = getattr(store, '_training_input_readers', 0)+1
    try:
        yield
    finally:
        with store.lock:
            store._training_input_readers -= 1


def retention_protected(store, table):
    # Caller holds Store.lock; actual storage emergencies remain authoritative.
    from training_generation import AGENT_TABLES
    return bool(getattr(store, '_training_input_readers', 0)) and (
        table in ENTITY_PREFIX_TABLES or table in AGENT_TABLES or
        table in ('home_checkpoints','adaptive_presence_checkpoints'))


def quote(name):
    return '"'+name.replace('"', '""')+'"'


def install(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS __training_prefix_versions '
                       '(name TEXT PRIMARY KEY, epoch INTEGER NOT NULL, boundary INTEGER NOT NULL, end_ts REAL NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS __training_source_identity '
                       '(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL)')
    connection.execute('INSERT OR IGNORE INTO __training_source_identity VALUES(1,?)',(uuid.uuid4().hex,))
    connection.execute('CREATE TABLE IF NOT EXISTS __training_entity_prefix_versions '
                       '(table_name TEXT NOT NULL,entity_id TEXT NOT NULL,epoch INTEGER NOT NULL,'
                       'boundary INTEGER NOT NULL,end_ts REAL NOT NULL,PRIMARY KEY(table_name,entity_id))')
    # Config and shared context are captured together in the initial short read.
    # Operational telemetry is never a training dependency.
    from training_generation import AGENT_TABLES
    dependencies=set(AGENT_TABLES)|{'entity_history','feature_observation_events',
        'provenance_events','provenance_history_links','home_checkpoints','adaptive_presence_checkpoints'}
    tables={r[0]:r[1] for r in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
    for table in sorted(dependencies & tables.keys()):
        if 'WITHOUT ROWID' in (tables[table] or '').upper():
            raise RuntimeError('Training boundary requires rowid: '+table)
        literal="'"+table.replace("'","''")+"'"
        connection.execute('INSERT OR IGNORE INTO __training_prefix_versions VALUES(?,0,0,-1e300)',(table,))
        for operation, predicate in [('INSERT','NEW.rowid'),('DELETE','OLD.rowid'),
                                     ('UPDATE','MIN(OLD.rowid,NEW.rowid)')]:
            name='__training_fence_'+table+'_'+operation.lower()
            stamp={'entity_history':'ts','feature_observation_events':'event_time',
                   'provenance_events':'event_time','provenance_history_links':'event_time',
                   'home_checkpoints':'ts','adaptive_presence_checkpoints':'ts'}.get(table)
            time_guard=''
            if stamp:
                cutoff=f'(SELECT end_ts FROM __training_prefix_versions WHERE name={literal})'
                if operation=='UPDATE':
                    time_guard=f' AND (OLD.{stamp}<={cutoff} OR NEW.{stamp}<={cutoff})'
                else:
                    moment=('OLD.' if operation=='DELETE' else 'NEW.')+stamp
                    time_guard=f' AND {moment}<={cutoff}'
            sql=(f'CREATE TRIGGER {quote(name)} AFTER {operation} ON {quote(table)} '
                 f'WHEN {predicate}<=(SELECT boundary FROM __training_prefix_versions WHERE name={literal})'+time_guard+' '
                 f'BEGIN UPDATE __training_prefix_versions SET epoch=epoch+1 WHERE name={literal}; END')
            existing=connection.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
            if existing is None:
                connection.execute(sql)
            elif existing[0]!=sql:
                raise RuntimeError('Training mutation guard definition mismatch: '+name)
        if table in ENTITY_PREFIX_TABLES:
            _install_entity_guard(connection, table, ENTITY_PREFIX_TABLES[table])


def _install_entity_guard(connection, table, stamp):
    columns = [r[1] for r in connection.execute('PRAGMA table_info('+quote(table)+')')]
    if 'entity_id' not in columns or stamp not in columns:
        return
    literal = "'"+table+"'"
    ledger = '__training_entity_prefix_versions'
    # mutation_revision is bookkeeping, not a change to replay input. All
    # actual payload fields (including timestamp, entity, source and received
    # time) remain guarded. Old and new membership both matter on a key move.
    changed = ' OR '.join('OLD.'+quote(c)+' IS NOT NEW.'+quote(c)
                         for c in columns if c!='mutation_revision')
    for operation, versions in [('INSERT',('NEW',)),('DELETE',('OLD',)),('UPDATE',('OLD','NEW'))]:
        name = '__training_fence_scope_'+table+'_'+operation.lower()
        matches = ' OR '.join(f'(entity_id={v}.entity_id AND {v}.rowid<=boundary AND {v}.{stamp}<=end_ts)' for v in versions)
        guard = f' WHEN ({changed} OR OLD.rowid IS NOT NEW.rowid)' if operation=='UPDATE' else ''
        sql = (f'CREATE TRIGGER {quote(name)} AFTER {operation} ON {quote(table)}'+guard+
               f' BEGIN UPDATE {ledger} SET epoch=epoch+1 WHERE table_name={literal} AND ({matches}); END')
        existing = connection.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if existing is None:
            connection.execute(sql)
        elif existing[0]!=sql:
            raise RuntimeError('Training entity guard definition mismatch: '+name)


def schema_manifest(connection, names=None):
    objects={r[0]:r[1] for r in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    if names is None:
        tables={r[0] for r in connection.execute('SELECT name FROM __training_prefix_versions')}
        names=tables|{'agents','app_meta','entity_history_revision',
                      '__training_prefix_versions','__training_source_identity','__training_entity_prefix_versions'}
        names|={name for name in objects if name.startswith('__training_fence_')}
    return {name:hashlib.sha256(objects[name].encode()).hexdigest() if objects.get(name) is not None else None for name in sorted(names)}


def capture(connection, end_ts, entities=None):
    """Caller owns a brief write transaction; no payload scan or throttle here."""
    bounds={}
    for name,epoch,_ in connection.execute('SELECT name,epoch,boundary FROM __training_prefix_versions').fetchall():
        # AUTOINCREMENT's durable high-water survives a corrupt final payload
        # page and deleted tail rows. Explicit reuse below it invalidates fences.
        row=connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(name,)).fetchone()
        if row is None:
            row=connection.execute(f'SELECT rowid FROM {quote(name)} NOT INDEXED ORDER BY rowid DESC LIMIT 1').fetchone()
        upper=int(row[0]) if row else 0
        connection.execute('UPDATE __training_prefix_versions SET boundary=MAX(boundary,?),end_ts=MAX(end_ts,?) WHERE name=?',(upper,float(end_ts),name))
        bounds[name]=dict(epoch=int(epoch),upper=upper)
    result = dict(token=connection.execute('SELECT token FROM __training_source_identity WHERE id=1').fetchone()[0],
                  tables=bounds,end_ts=float(end_ts),schema=schema_manifest(connection))
    if entities is not None:
        entities = sorted(set(entities))
        scoped = {}
        for table in ENTITY_PREFIX_TABLES:
            if table not in bounds or not connection.execute(
                    'SELECT 1 FROM sqlite_master WHERE name=?',
                    ('__training_fence_scope_'+table+'_update',)).fetchone():
                continue
            upper = bounds[table]['upper']
            connection.executemany(
                'INSERT INTO __training_entity_prefix_versions VALUES(?,?,0,?,?) '
                'ON CONFLICT(table_name,entity_id) DO UPDATE SET '
                'boundary=MAX(boundary,excluded.boundary),end_ts=MAX(end_ts,excluded.end_ts)',
                [(table,entity,upper,float(end_ts)) for entity in entities])
            epochs = {r[0]:int(r[1]) for r in connection.execute(
                'SELECT entity_id,epoch FROM __training_entity_prefix_versions WHERE table_name=?',(table,))}
            scoped[table] = {entity:epochs[entity] for entity in entities}
        result['entity_scope'] = scoped
    return result


def validate(connection, boundary):
    if schema_manifest(connection,boundary['schema'])!=boundary['schema']:
        raise RuntimeError('Training source schema/guards changed; previous model retained')
    token=connection.execute('SELECT token FROM __training_source_identity WHERE id=1').fetchone()
    if not token or token[0]!=boundary['token']:
        raise RuntimeError('Training source identity changed; previous model retained')
    current={r[0]:(int(r[1]),int(r[2]),float(r[3])) for r in connection.execute('SELECT name,epoch,boundary,end_ts FROM __training_prefix_versions')}
    scoped = boundary.get('entity_scope',{})
    for table,expected in boundary['tables'].items():
        row = current.get(table)
        if not row or row[1]<expected['upper'] or row[2]<boundary['end_ts']:
            raise RuntimeError('Training input prefix changed: '+table+'; previous model retained')
        if table in scoped:
            actual = {r[0]:(int(r[1]),int(r[2]),float(r[3])) for r in connection.execute(
                'SELECT entity_id,epoch,boundary,end_ts FROM __training_entity_prefix_versions WHERE table_name=?',(table,))}
            for entity,epoch in scoped[table].items():
                value = actual.get(entity)
                if not value or value[0]!=epoch or value[1]<expected['upper'] or value[2]<boundary['end_ts']:
                    raise RuntimeError('Training input prefix changed: '+table+'; entity='+entity+'; previous model retained')
        elif row[0]!=expected['epoch']:
            raise RuntimeError('Training input prefix changed: '+table+'; previous model retained')


def boundary_digest(boundary):
    return hashlib.sha256(json.dumps(boundary,sort_keys=True).encode()).hexdigest()
