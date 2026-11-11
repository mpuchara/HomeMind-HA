"""Durable ingestion boundaries without retaining a WAL reader.

Only the parent installs the small mutation ledger. Workers read payloads through
short autocommit statements. Inserts above the captured rowid boundary belong to
the next training input. Updates/deletes/reused ids in a fenced prefix invalidate
it, including between processes or across a crash. No original payload is changed.
"""
import hashlib
import json
import uuid


def quote(name):
    return '"'+name.replace('"', '""')+'"'


def install(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS __training_prefix_versions '
                       '(name TEXT PRIMARY KEY, epoch INTEGER NOT NULL, boundary INTEGER NOT NULL, end_ts REAL NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS __training_source_identity '
                       '(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL)')
    connection.execute('INSERT OR IGNORE INTO __training_source_identity VALUES(1,?)',(uuid.uuid4().hex,))
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


def schema_manifest(connection, names=None):
    objects={r[0]:r[1] for r in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    if names is None:
        tables={r[0] for r in connection.execute('SELECT name FROM __training_prefix_versions')}
        names=tables|{'agents','app_meta','entity_history_revision',
                      '__training_prefix_versions','__training_source_identity'}
        names|={name for name in objects if name.startswith('__training_fence_')}
    return {name:hashlib.sha256(objects[name].encode()).hexdigest() if objects.get(name) is not None else None for name in sorted(names)}


def capture(connection, end_ts):
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
    return dict(token=connection.execute('SELECT token FROM __training_source_identity WHERE id=1').fetchone()[0],
                tables=bounds,end_ts=float(end_ts),schema=schema_manifest(connection))


def validate(connection, boundary):
    if schema_manifest(connection,boundary['schema'])!=boundary['schema']:
        raise RuntimeError('Training source schema/guards changed; previous model retained')
    token=connection.execute('SELECT token FROM __training_source_identity WHERE id=1').fetchone()
    if not token or token[0]!=boundary['token']:
        raise RuntimeError('Training source identity changed; previous model retained')
    current={r[0]:(int(r[1]),int(r[2]),float(r[3])) for r in connection.execute('SELECT name,epoch,boundary,end_ts FROM __training_prefix_versions')}
    for table,expected in boundary['tables'].items():
        if current.get(table,(None,0))[0]!=expected['epoch'] or current[table][1]<expected['upper'] or current[table][2]<boundary['end_ts']:
            raise RuntimeError('Training input prefix changed: '+table+'; previous model retained')


def boundary_digest(boundary):
    return hashlib.sha256(json.dumps(boundary,sort_keys=True).encode()).hexdigest()
