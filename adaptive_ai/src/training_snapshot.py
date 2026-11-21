"""Durable ingestion boundaries without retaining a WAL reader.

Only the parent installs the small mutation ledger. Workers read payloads through
short autocommit statements. Inserts above the captured rowid boundary belong to
the next training input. Updates/deletes/reused ids in a fenced prefix invalidate
it, including between processes or across a crash. No original payload is changed.
"""
import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path

from sqlite_background import is_sqlite_corruption

ENTITY_PREFIX_TABLES = {'entity_history':'ts', 'feature_observation_events':'event_time',
                        'provenance_events':'event_time', 'provenance_history_links':'event_time'}
FEATURE_EVENTS = 'feature_observation_events'
FEATURE_FENCES = '__training_feature_windows'
HISTORY_EVENTS = 'entity_history'
HISTORY_FENCES = '__training_history_windows'
WINDOWS = {FEATURE_EVENTS: ('feature', FEATURE_FENCES), HISTORY_EVENTS: ('history', HISTORY_FENCES)}


class TrainingInputChanged(RuntimeError):
    def __init__(self, table, reason):
        self.table = table
        super().__init__(f'Training input prefix changed: {table}; {reason}; previous model retained')


def changed_columns(columns, excluded=()):
    fields = (f'OLD.{quote(column)} IS NOT NEW.{quote(column)}'
              for column in columns if column not in excluded)
    return '(' + ' OR '.join([*fields, 'OLD.rowid IS NOT NEW.rowid']) + ')'


def install_trigger(connection, name, sql, legacy=None):
    existing = connection.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
    if existing is not None and existing[0] == sql:
        return
    if existing is not None:
        if existing[0] != legacy:
            raise RuntimeError('Training mutation guard definition mismatch: ' + name)
        connection.execute('DROP TRIGGER ' + quote(name))
    connection.execute(sql)


@contextmanager
def protect_training_inputs(store, owner=None):
    """Coordinate routine retention with the supervised child; no held SQLite lock."""
    # Small scheduling fixtures and legacy read-only adapters own no retention.
    if not hasattr(store, 'lock'):
        yield
        return
    with store.lock:
        readers = getattr(store, '_training_input_readers', None)
        if readers is None:
            readers = store._training_input_readers = Counter()
        readers[owner] += 1
    try:
        yield
    finally:
        with store.lock:
            readers[owner] -= 1
            if not readers[owner]:
                del readers[owner]


def retention_protected(store, table):
    # Caller holds Store.lock; actual storage emergencies remain authoritative.
    from training_generation import AGENT_TABLES
    return bool(getattr(store, '_training_input_readers', 0)) and (
        table in ENTITY_PREFIX_TABLES or table in AGENT_TABLES or
        table in ('home_checkpoints','adaptive_presence_checkpoints'))


def feature_retention_filter(connection, store):
    """Keep valid windows of supervised jobs out of routine eviction; caller holds Store.lock."""
    owners = tuple(owner for owner in getattr(store, '_training_input_readers', ()) if owner is not None)
    if not owners or not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (FEATURE_FENCES,)).fetchone():
        return '', ()
    marks = ','.join('?' for _ in owners)
    return (f' AND NOT EXISTS (SELECT 1 FROM {FEATURE_FENCES} AS fence '
            f'WHERE fence.entity_id={FEATURE_EVENTS}.entity_id AND fence.owner IN ({marks}) '
            f'AND {FEATURE_EVENTS}.rowid<=fence.boundary '
            f'AND {FEATURE_EVENTS}.event_time BETWEEN fence.start_ts AND fence.end_ts '
            'AND fence.operation IS NULL)', owners)


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
        feature_columns = ([r[1] for r in connection.execute('PRAGMA table_info('+quote(table)+')')]
                           if table == FEATURE_EVENTS else [])
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
            legacy = None
            if feature_columns and operation == 'UPDATE':
                legacy = sql
                sql = sql.replace(' BEGIN UPDATE ', ' AND '+changed_columns(feature_columns, ('protected_until',))
                                  +' BEGIN UPDATE ', 1)
            install_trigger(connection, name, sql, legacy)
        if table in ENTITY_PREFIX_TABLES:
            _install_entity_guard(connection, table, ENTITY_PREFIX_TABLES[table])
        if table in WINDOWS:
            columns = feature_columns or [r[1] for r in connection.execute('PRAGMA table_info('+quote(table)+')')]
            stamp = ENTITY_PREFIX_TABLES[table]
            if {'entity_id', stamp}.issubset(columns):
                _install_window_guards(connection, table, stamp, columns)


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
        # Most realtime rows are appended above every frozen boundary. Avoid
        # even looking up a per-entity ledger row on this hot path.
        bound = f'(SELECT boundary FROM __training_prefix_versions WHERE name={literal})'
        cutoff = f'(SELECT end_ts FROM __training_prefix_versions WHERE name={literal})'
        possible = ' OR '.join(f'({v}.rowid<={bound} AND {v}.{stamp}<={cutoff})' for v in versions)
        guard = ' WHEN ('+possible+')'
        if operation=='UPDATE':
            guard += f' AND ({changed} OR OLD.rowid IS NOT NEW.rowid)'
        members = ','.join(v+'.entity_id' for v in versions)
        sql = (f'CREATE TRIGGER {quote(name)} AFTER {operation} ON {quote(table)}'+guard+
               f' BEGIN UPDATE {ledger} SET epoch=epoch+1 WHERE table_name={literal} '
               f'AND entity_id IN ({members}) AND ({matches}); END')
        legacy = None
        if table == FEATURE_EVENTS and operation == 'UPDATE':
            legacy = sql
            sql = sql.replace(f'({changed} OR OLD.rowid IS NOT NEW.rowid)',
                              changed_columns(columns, ('protected_until', 'mutation_revision')), 1)
        install_trigger(connection, name, sql, legacy)


def _install_window_guards(connection, table, stamp, columns):
    # One durable slot per owner, indexed by entity for the mutation hot path.
    # Retain only the first affecting operation. There is no per-event log.
    kind, fences = WINDOWS[table]
    connection.execute(f'CREATE TABLE IF NOT EXISTS {fences} ('
                       'entity_id TEXT NOT NULL, owner TEXT NOT NULL, token TEXT NOT NULL, '
                       'boundary INTEGER NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL, '
                       'operation TEXT, PRIMARY KEY(entity_id,owner)) WITHOUT ROWID')
    connection.execute(f'CREATE INDEX IF NOT EXISTS {fences}_owner ON {fences}(owner)')
    bound = f"(SELECT boundary FROM __training_prefix_versions WHERE name='{table}')"
    end = f"(SELECT end_ts FROM __training_prefix_versions WHERE name='{table}')"
    for operation, versions in [('INSERT', ('NEW',)), ('DELETE', ('OLD',)), ('UPDATE', ('OLD', 'NEW'))]:
        name = f'__training_fence_{kind}_window_' + operation.lower()
        possible = ' OR '.join(f'({v}.rowid<={bound} AND {v}.{stamp}<={end})' for v in versions)
        condition = f'({possible})'
        if operation == 'UPDATE':
            metadata = ('protected_until', 'mutation_revision') if table == FEATURE_EVENTS else ('mutation_revision',)
            condition += ' AND ' + changed_columns(columns, metadata)
        entity_ids = ','.join(v+'.entity_id' for v in versions)
        members = ' OR '.join(f'(entity_id={v}.entity_id AND {v}.rowid<=boundary '
                              f'AND {v}.{stamp} BETWEEN start_ts AND end_ts)' for v in versions)
        sql = (f'CREATE TRIGGER {quote(name)} AFTER {operation} ON {table} WHEN {condition} '
               f"BEGIN UPDATE {fences} SET operation='{operation}' WHERE operation IS NULL "
               f'AND entity_id IN ({entity_ids}) AND ({members}); END')
        install_trigger(connection, name, sql)


def schema_manifest(connection, names=None):
    objects={r[0]:r[1] for r in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    if names is None:
        tables={r[0] for r in connection.execute('SELECT name FROM __training_prefix_versions')}
        names=tables|{'agents','app_meta','entity_history_revision',
                      '__training_prefix_versions','__training_source_identity','__training_entity_prefix_versions'}
        names|={name for name in objects if name.startswith('__training_fence_')}
        names |= {fences for _, fences in WINDOWS.values() if fences in objects}
    return {name:hashlib.sha256(objects[name].encode()).hexdigest() if objects.get(name) is not None else None for name in sorted(names)}


def _history_seed_floor(connection, entity, start_ts, upper):
    try:
        row = connection.execute(
            'SELECT ts FROM entity_history WHERE entity_id=? AND ts<? AND rowid<=? '
            'ORDER BY ts DESC,id DESC LIMIT 1', (entity, start_ts, upper)).fetchone()
    except sqlite3.DatabaseError as exc:
        if not is_sqlite_corruption(exc):
            raise
        row = None
    # If there is no known seed, any existing physical row moved before start
    # could become one. Keep the conservative prefix on an unreadable index too.
    return float(row[0]) if row else -1e300


def _history_seed_floors(connection, entities, start_ts, upper):
    if not entities:
        return []
    filename = next(row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main')
    if not filename:
        return [_history_seed_floor(connection, entity, start_ts, upper) for entity in entities]
    # BEGIN IMMEDIATE already excludes changes to history. Read the keys on
    # a separate connection so index damage cannot poison the ledger writer.
    uri = Path(filename).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=.2)) as reader:
        return [_history_seed_floor(reader, entity, start_ts, upper) for entity in entities]


def capture(connection, end_ts, entities=None, entity_scopes=None, *, start_ts=None, owner=None):
    """Register a boundary under a write transaction with no pending archive payload writes."""
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
            selected = sorted(set((entity_scopes or {}).get(table, entities)))
            if table in WINDOWS and start_ts is not None and owner is not None:
                kind, fences = WINDOWS[table]
                token = uuid.uuid4().hex
                connection.execute(f'DELETE FROM {fences} WHERE owner=?', (owner,))
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='agents' AND type='table'").fetchone():
                    connection.execute(f'DELETE FROM {fences} WHERE NOT EXISTS '
                                       f'(SELECT 1 FROM agents WHERE id={fences}.owner)')
                floor = float(start_ts)
                lower = (_history_seed_floors(connection, selected, floor, upper)
                         if table == HISTORY_EVENTS else [floor] * len(selected))
                connection.executemany(
                    f'INSERT INTO {fences} VALUES(?,?,?,?,?,?,NULL)',
                    ((entity, owner, token, upper, lower_ts, float(end_ts))
                     for entity, lower_ts in zip(selected, lower)))
                window = dict(owner=owner, token=token, count=len(selected))
                if table == FEATURE_EVENTS:
                    window['start_ts'] = floor
                result[kind + '_window'] = window
                continue
            connection.executemany(
                'INSERT INTO __training_entity_prefix_versions VALUES(?,?,0,?,?) '
                'ON CONFLICT(table_name,entity_id) DO UPDATE SET '
                'boundary=MAX(boundary,excluded.boundary),end_ts=MAX(end_ts,excluded.end_ts)',
                [(table,entity,upper,float(end_ts)) for entity in selected])
            epochs = {r[0]:int(r[1]) for r in connection.execute(
                'SELECT entity_id,epoch FROM __training_entity_prefix_versions WHERE table_name=?',(table,))}
            scoped[table] = {entity:epochs[entity] for entity in selected}
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
        kind, fences = WINDOWS.get(table, (None, None))
        window = boundary.get(kind + '_window') if kind else None
        if window is not None:
            floor_filter = ' AND start_ts=?' if table == FEATURE_EVENTS else ''
            floors = (window['start_ts'],) if floor_filter else ()
            rows = connection.execute(
                f'SELECT operation,entity_id FROM {fences} WHERE owner=? AND token=? AND boundary=? '
                f'AND end_ts=?{floor_filter}',
                (window['owner'], window['token'], expected['upper'], boundary['end_ts'], *floors)).fetchall()
            operation = next((row for row in rows if row[0] is not None), None)
            if operation is not None or len(rows) != window['count']:
                reason = f'{operation[0]} inside frozen {kind} window' if operation else 'window superseded'
                if table == HISTORY_EVENTS and operation:
                    reason += '; entity=' + operation[1]
                raise TrainingInputChanged(table, reason)
        elif table in scoped:
            actual = {r[0]:(int(r[1]),int(r[2]),float(r[3])) for r in connection.execute(
                'SELECT entity_id,epoch,boundary,end_ts FROM __training_entity_prefix_versions WHERE table_name=?',(table,))}
            for entity,epoch in scoped[table].items():
                value = actual.get(entity)
                if not value or value[0]!=epoch or value[1]<expected['upper'] or value[2]<boundary['end_ts']:
                    raise TrainingInputChanged(table, 'entity='+entity)
        elif row[0]!=expected['epoch']:
            raise RuntimeError('Training input prefix changed: '+table+'; previous model retained')


def release_training_windows(connection, boundary):
    for kind, fences in WINDOWS.values():
        window = (boundary or {}).get(kind + '_window')
        if window is not None:
            connection.execute(f'DELETE FROM {fences} WHERE owner=? AND token=?',
                               (window['owner'], window['token']))


def boundary_digest(boundary):
    return hashlib.sha256(json.dumps(boundary,sort_keys=True).encode()).hexdigest()
