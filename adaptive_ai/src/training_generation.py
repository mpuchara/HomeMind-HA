"""Selective, restartable preparation of one immutable training generation.

Payload reads are read-only; a bounded source mutation ledger fences prefixes.
All source tables retain their schema in the worker,
but only documented training dependencies receive rows. Copy checkpoints are
committed with their rows; they can be reused only for the same source version.
"""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from history_recovery import CORRECT_TABLES, RecoveryUnavailable, quote, correct_fingerprint, correct_predicate
from sqlite_background import is_sqlite_corruption
from training_budget import TRAINING_BUDGET

CONTRACT = 'training-history-generation-v6'
AGENT_TABLES = frozenset(CORRECT_TABLES) | {
    'rl_models', 'historical_experiences', 'tiny_mlp_shadow_models',
    'agent_candidate_generations', 'automatic_reward_experiences',
    'provenance_experiences', 'teaching_rl_jobs',
}
META_KEYS = (
    'room_belief_model_v2', 'shared_home_model_v1', 'adaptive_presence_model_v2',
    'adaptive_presence_model_v1', 'semantic_reliability_v1', 'training_revision',
    'candidate_qualification_complete', 'training_recorder_coverage_v1',
    'training_history_gaps_v1', 'discovery_recorder_gaps_v1',
    'discovery_deep_history_complete',
)


def signature(source):
    result = []
    for suffix in ('', '-wal'):
        path = Path(str(source)+suffix)
        stat = path.stat() if path.exists() else None
        result.append([stat.st_size, stat.st_mtime_ns] if stat and (not suffix or stat.st_size>32) else None)
    return result


class TrainingPreparationNoProgress(RecoveryUnavailable):
    """A nonempty keyset page must advance before any destination mutation."""


def advancing_cursor(previous, current, *, table, strategy, rows, job_id, query_key):
    left = tuple(previous) if isinstance(previous, (list, tuple)) else previous
    right = tuple(current) if isinstance(current, (list, tuple)) else current
    if left is not None and not right > left:
        raise TrainingPreparationNoProgress(json.dumps(dict(
            error='TrainingPreparationNoProgress', table_name=table,
            copy_strategy=strategy, previous_cursor=previous, current_cursor=current,
            payload_rows_fetched=rows, job_id=job_id, query_key=query_key)))


class Preparation:
    def __init__(self, source, destination, entities, start, end, options, cancelled, progress, agent_id, rebuild=False):
        self.source, self.destination = Path(source).resolve(), Path(destination)
        self.entities = sorted(set(entities))
        self.start, self.end, self.agent_id = float(start), float(end), agent_id
        self.options, self.cancelled, self.progress = options or {}, cancelled, progress
        self.rebuild=bool(rebuild)
        self.started, self.cpu_started = time.monotonic(), time.process_time()
        self.throttle = self.sqlite_elapsed = self.sqlite_cpu = self.lock_wait = 0.
        self.recovery_cpu = 0.
        self.recovery_started = None
        self.in_recovery = False
        self.attempts = 0
        self.limit = max(1,min(16384,int(self.options.get('max_recovery_attempts',4096))))
        self.depth = max(1,min(48,int(self.options.get('max_recovery_depth',32))))
        self.seconds = max(1,min(600,float(self.options.get('max_recovery_wall_seconds',120))))
        self.memory = max(1,min(32,int(self.options.get('max_recovery_memory_mb',8))))*1048576
        self.disk = max(16,min(2048,int(self.options.get('max_recovery_disk_mb',512))))*1048576
        self.source_signature = signature(self.source)
        self.identity = dict(contract=CONTRACT,source=str(self.source),entities=self.entities,
                             start=start,end=end,agent_id=agent_id,rebuild=self.rebuild)
        # At most one unfinished preparation per source/agent. Durable prefix
        # versions allow live append/WAL maintenance without invalidating cursors.
        key = hashlib.sha256((str(self.source)+'|'+str(agent_id)+'|'+CONTRACT).encode()).hexdigest()[:24]
        self.working = self.destination.parent/('.preparing-'+key+'.db')
        self.state = dict(identity=self.identity,queries={},tables={})
        self.resumed = False
        self.last_report = 0.
        self.last_disk_check = 0.
        self.pressure = None
        self.bootstrap = False
        self.initial_metadata = None
        self.agent_ids = []
        self.snapshot_max_seconds = 0.
        self.source_sql_max_seconds = 0.
        self.wal_peak_bytes = 0
        self.pending_batches = 0
        self.pending_bytes = 0
        self.last_commit = time.monotonic()
        self.commit_seconds = 0.
        self.status_write_seconds = 0.

    def checkpoint(self):
        if self.cancelled():
            raise InterruptedError('Training history preparation cancelled; copy checkpoint retained')
        before = TRAINING_BUDGET.snapshot().get('sleep_seconds',0.)
        if not self.bootstrap:
            TRAINING_BUDGET.checkpoint('training_history_preparation')
        self.throttle += max(0.,TRAINING_BUDGET.snapshot().get('sleep_seconds',0.)-before)
        from storage_files import worker_storage_budget
        guard=worker_storage_budget()
        if guard.check_operation('source_read',refresh=True)!='normal':
            raise guard.pressure()
        wal=Path(str(self.source)+'-wal')
        self.wal_peak_bytes=max(self.wal_peak_bytes,wal.stat().st_size if wal.exists() else 0)
        if time.monotonic()-self.last_disk_check>1.:
            self.last_disk_check=time.monotonic()
            used=sum(p.stat().st_size for p in self.destination.parent.iterdir()
                     if p.is_file() and p.suffix in ('.db','.db-journal') and not p.is_symlink())
            if used>2*self.disk:
                raise RecoveryUnavailable('Preparation directory data quota exhausted; previous model retained')
        if self.in_recovery and self.active_recovery_cpu() > self.seconds:
            raise RecoveryUnavailable('Active corruption recovery budget exhausted; previous model retained')

    def active_recovery_cpu(self):
        return self.recovery_cpu+(time.process_time()-self.recovery_started if self.recovery_started is not None else 0.)

    def validate_history(self, rows):
        size=sum(sum(len(v.encode('utf-8')) if isinstance(v,str) else len(v) if isinstance(v,bytes) else 8
                     for v in row) for row in rows)
        if size>self.memory:
            raise RecoveryUnavailable('Recovered row batch exceeds preparation memory budget')
        for row in rows:
            if not math.isfinite(float(row['ts'])):
                raise RecoveryUnavailable('Non-finite history timestamp')
            json.loads(row['attributes_json'])
        return size

    def sql(self, connection, method, *args):
        before, cpu = time.monotonic(), time.process_time()
        throttle_before=self.throttle
        try:
            return getattr(connection,method)(*args)
        except sqlite3.OperationalError as exc:
            if self.pressure is not None:
                raise self.pressure from exc
            if getattr(exc,'sqlite_errorcode',0) & 255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                self.lock_wait += time.monotonic()-before
            raise
        finally:
            duration, work = time.monotonic()-before, time.process_time()-cpu
            self.sqlite_elapsed += max(0.,duration-(self.throttle-throttle_before))
            self.sqlite_cpu += work
            if connection is getattr(self,'src',None):
                self.source_query_started=before
                self.source_sql_max_seconds=max(self.source_sql_max_seconds,duration)
            elif getattr(connection,'connection',None) is getattr(self,'src',None):
                self.source_sql_max_seconds=max(self.source_sql_max_seconds,time.monotonic()-getattr(self,'source_query_started',before))

    def emit(self, table, strategy, force=False):
        now = time.monotonic()
        if not force and now-self.last_report < 1.:
            return
        self.last_report = now
        metric = dict(self.state['tables'].get(table) or {})
        metric.update(agent_id=self.agent_id,table_name=table,copy_strategy=strategy,
            elapsed_seconds=round(now-self.started,6),cpu_seconds=round(time.process_time()-self.cpu_started,6),
            sqlite_wait_seconds=round(max(0.,self.sqlite_elapsed-self.sqlite_cpu-self.lock_wait),6),
            sqlite_seconds=round(self.sqlite_elapsed,6),lock_wait_seconds=round(self.lock_wait,6),
            throttle_seconds=round(self.throttle,6),recovery_attempts=self.attempts,
            wal_peak_bytes=self.wal_peak_bytes,source_owned_connections=1,
            explicit_source_transaction_active=bool(self.src.in_transaction),
            snapshot_max_seconds=self.snapshot_max_seconds,
            job_id=self.destination.stem, commit_seconds=self.commit_seconds,
            status_write_seconds=self.status_write_seconds)
        status_started = time.monotonic()
        self.progress(message=f"{'Recovering historical data' if self.in_recovery else 'Preparing training history'}: {table}, {metric.get('rows_copied',0):,} records copied",
            phase_detail='Recovering historical data' if self.in_recovery else 'Copying required metadata / selected history',
            preparation=metric,work_done=metric.get('rows_copied',0),work_total=0,
            progress=min(.09,.01+.08*len([x for x in self.state['queries'].values() if x.get('done')])/(len(self.schema)+len(self.entities)+1)))
        self.status_write_seconds += time.monotonic()-status_started

    def save(self, force=True):
        # Rows and the cursor always commit together. Bound uncommitted work by
        # eight pages, the preparation memory budget, or one second of work.
        if not force and self.pending_batches < 8 and self.pending_bytes < self.memory and time.monotonic()-self.last_commit < 1.:
            return
        started = time.monotonic()
        for metric in self.state['tables'].values():
            pending = metric.pop('pending_batches', 0)
            metric['committed_batches'] = metric.get('committed_batches', 0)+pending
        self.dst.execute('INSERT OR REPLACE INTO __training_copy_state VALUES(1,?)',(json.dumps({k:v for k,v in self.state.items() if k!='source_schema'}),))
        self.dst.commit()
        self.commit_seconds += time.monotonic()-started
        self.last_commit = time.monotonic()
        self.pending_batches = self.pending_bytes = 0

    def copy(self, table, predicate='', params=(), *, query_key=None, history=False, key='rowid', checkpoint=True, strategy='scoped_sql'):
        """Keyset paging uses the source's entity/time or agent index, before decoding."""
        query_key = query_key or table
        saved = self.state['queries'].get(query_key,{}) if checkpoint else {}
        if saved.get('done'):
            return
        metric = self.state['tables'].setdefault(table,dict(source_rows_scanned=0,rows_copied=0,bytes_copied=0,copy_strategy=strategy))
        self.emit(table,strategy,True)
        last = saved.get('last')
        while True:
            self.checkpoint()
            where = [f'({predicate})'] if predicate else []
            if table in self.state.get('boundary',{}).get('tables',{}):
                where.append('rowid<=?')
                values_boundary=[self.state['boundary']['tables'][table]['upper']]
            else:
                values_boundary=[]
            values = list(params)+values_boundary
            if last is not None:
                if key in ('ts,id','event_time,rowid'):
                    stamp, identity = key.split(',')
                    # Tuple keyset lets SQLite seek the composite index rather
                    # than rechecking the entire copied prefix for every batch.
                    where.append(f'({stamp},{identity})>(?,?)'); values += [last[0],last[1]]
                else:
                    where.append('rowid>?'); values.append(last)
            extra = '' if key == 'ts,id' else 'rowid AS __copy_rowid,'
            sql = f'SELECT {extra}* FROM {quote(table)}'+(' WHERE '+' AND '.join(where) if where else '')+f' ORDER BY {key} LIMIT 128'
            with closing(self.sql(self.src,'execute',sql,values)) as cursor:
                batch = self.sql(cursor,'fetchall')
            if not batch:
                if checkpoint:
                    self.state['queries'][query_key] = dict(done=True,last=last)
                    self.save()
                break
            if key in ('ts,id','event_time,rowid'):
                stamp, identity = key.split(',')
                new_cursor = [float(batch[-1][stamp]), int(batch[-1]['id' if identity=='id' else '__copy_rowid'])]
            else:
                new_cursor = int(batch[-1]['__copy_rowid'])
            advancing_cursor(last, new_cursor, table=table, strategy=strategy,
                             rows=len(batch), job_id=self.destination.stem, query_key=query_key)
            metric.update(query_key=query_key, previous_cursor=last, current_cursor=new_cursor,
                          last_cursor_advance_at=time.time())
            metric['source_rows_scanned'] += len(batch)
            metric['payload_rows_fetched'] = metric['source_rows_scanned']
            size = sum(sum(len(v.encode('utf-8')) if isinstance(v,str) else len(v) if isinstance(v,bytes) else 16 for v in row) for row in batch)
            if size>self.memory:
                raise RecoveryUnavailable('Preparation row batch exceeds memory budget')
            columns = [c for c in batch[0].keys() if c!='__copy_rowid']
            if history:
                for row in batch:
                    if not math.isfinite(float(row['ts'])):
                        raise RecoveryUnavailable('Non-finite history timestamp')
                    json.loads(row['attributes_json'])
            before = self.dst.total_changes
            self.sql(self.dst,'executemany',f'INSERT OR IGNORE INTO {quote(table)} ({",".join(map(quote,columns))}) VALUES ({",".join("?" for _ in columns)})',
                     [tuple(r[c] for c in columns) for r in batch])
            inserted = self.dst.total_changes-before
            metric['rows_copied'] += inserted
            metric['unique_rows_copied'] = metric['rows_copied']
            metric['duplicate_rows'] = metric.get('duplicate_rows',0)+len(batch)-inserted
            metric['bytes_copied'] += size  # legacy: payload bytes fetched, not physical disk writes
            metric['payload_bytes_fetched'] = metric['bytes_copied']
            metric['inserted_payload_bytes'] = metric.get('inserted_payload_bytes',0)+(
                size if inserted==len(batch) else 0)
            metric['inserted_payload_bytes_exact'] = metric.get('inserted_payload_bytes_exact',True) and inserted in (0,len(batch))
            if inserted:
                metric['last_new_row_at'] = time.time()
            metric['pending_batches'] = metric.get('pending_batches',0)+1
            self.pending_batches += 1
            self.pending_bytes += size
            last = new_cursor
            if checkpoint:
                self.state['queries'][query_key] = dict(done=False,last=last)
                self.save(force=False)
            self.emit(table,strategy)
        self.emit(table,strategy,True)

    def table_plan(self, item):
        name = item['name']
        columns = {r[1] for r in self.src.execute(f'PRAGMA table_info({quote(name)})')}
        ids = self.agent_ids
        marks = ','.join('?' for _ in ids)
        entity_marks = ','.join('?' for _ in self.entities)
        if name=='agents':
            return '',(), 'all_controller_configuration'
        if name=='app_meta':
            return 'key IN ('+','.join('?' for _ in META_KEYS)+')',META_KEYS,'shared_training_context_keys'
        if name=='entity_history_revision':
            return '',(), 'single_archive_revision'
        if name in CORRECT_TABLES:
            predicate, values = correct_predicate(self.src,name,ids)
            return predicate,values,'correct_dependency'
        if name in AGENT_TABLES:
            if self.rebuild and name in ('rl_models','historical_experiences'):
                return None  # a rebuild clears these only inside the generation
            if 'agent_id' in columns and ids:
                if name in ('rl_models','historical_experiences','tiny_mlp_shadow_models'):
                    return 'agent_id=?',(self.agent_id,),'selected_agent'
                return f'agent_id IN ({marks})',ids,'agent_family'
            if self.agent_id is None:
                return '',(),'legacy_fixture_scope'
            # No guessing a relation: keep schema, do not silently mis-scope Correct.
            if name in CORRECT_TABLES:
                raise RecoveryUnavailable(f'Unknown Correct dependency layout: {name}')
        if name=='tiny_mlp_correct_samples' and 'batch_id' in columns:
            return f'batch_id IN (SELECT batch_id FROM tiny_mlp_correct_batches WHERE agent_id IN ({marks}))',ids,'correct_batch_dependency'
        if name in ('provenance_events','provenance_history_links','feature_observation_events'):
            stamp = 'event_time'
            return f'entity_id IN ({entity_marks}) AND {stamp}<=?',(*self.entities,self.end),'entity_time'
        if name in ('home_checkpoints','adaptive_presence_checkpoints'):
            # Keep all as-of checkpoints that recent/optional sparse replay can
            # consume, plus its one older seed. A single start checkpoint would
            # silently change later logical chunks and older sparse replay.
            days=max(1.,float(self.options.get('agent_training_history_days',14)),
                     min(90.,float(self.options.get('agent_training_long_memory_days',14))))
            horizons=str(self.options.get('prediction_horizons_seconds','1')).replace(';',',').split(',')
            guard=max(30.,float(self.options.get('temporal_long_seconds',300)),
                      float(self.options.get('fast_precursor_off_seconds',120)),
                      *[float(x.strip()) for x in horizons if x.strip()])
            lower=max(0.,min(self.start,self.end-days*86400.)-guard)
            upper=self.state.get('boundary',{}).get('tables',{}).get(name,{}).get('upper')
            seed_boundary=' AND rowid<=?' if upper is not None else ''
            values=(lower,self.end,lower)+((upper,) if upper is not None else ())
            return '(ts>=? AND ts<=?) OR ts=(SELECT MAX(ts) FROM '+quote(name)+' WHERE ts<?'+seed_boundary+')',values,'causal_checkpoint_window_and_seed'
        return None

    def read_initial_metadata(self, connection):
        initial={}
        buffered_bytes=0
        started=time.perf_counter()
        tables={item['name']:item for item in self.state['source_schema'] if item['type']=='table'}
        for name in ('agents','app_meta','entity_history_revision','rl_models','tiny_mlp_shadow_models'):
            if name not in tables:
                continue
            plan=self.table_plan(tables[name])
            if not plan:
                self.state['tables'][name]=dict(source_rows_scanned=0,rows_copied=0,bytes_copied=0,copy_strategy='schema_only_rebuild')
                continue
            predicate,params,strategy=plan
            rows=[]
            with closing(connection.execute('SELECT * FROM '+quote(name)+(' WHERE '+predicate if predicate else ''),params)) as cursor:
                while True:
                    batch=cursor.fetchmany(128)
                    if not batch: break
                    if self.cancelled():
                        raise InterruptedError('Initial metadata snapshot cancelled')
                    buffered_bytes+=sum(sum(len(v.encode('utf-8')) if isinstance(v,str) else len(v) if isinstance(v,bytes) else 16 for v in row) for row in batch)
                    if buffered_bytes>self.memory:
                        raise RecoveryUnavailable('Initial controller/context snapshot exceeds memory budget')
                    if time.perf_counter()-started>1.:
                        raise RecoveryUnavailable('Initial controller/context snapshot exceeded bounded admission time')
                    rows.extend(batch)
            initial[name]=(rows,strategy)
        model=connection.execute('SELECT model_json FROM rl_models WHERE agent_id=?',(self.agent_id,)).fetchone() if self.agent_id else None
        self.state['model_fingerprint']=hashlib.sha256(model[0].encode()).hexdigest() if model else None
        neural=connection.execute('SELECT * FROM tiny_mlp_shadow_models WHERE agent_id=?',(self.agent_id,)).fetchone() if self.agent_id and 'tiny_mlp_shadow_models' in tables else None
        self.state['neural_fingerprint']=hashlib.sha256(json.dumps(tuple(neural),default=str).encode()).hexdigest() if neural else None
        # These rows were buffered atomically, never copied across transactions.
        # Publication checks the selected model hashes; another agent's model
        # update must not invalidate this job's historical input.
        for name in ('rl_models','tiny_mlp_shadow_models'):
            self.state['boundary']['tables'].pop(name,None)
        if self.rebuild:
            self.state['boundary']['tables'].pop('historical_experiences',None)
        return initial

    def run(self, http_gaps):
        self.destination.parent.mkdir(parents=True,exist_ok=True)
        if self.destination.exists():
            raise RecoveryUnavailable('Training generation already exists')
        if self.working.exists():
            try:
                with closing(sqlite3.connect(self.working)) as old:
                    record=old.execute('SELECT payload FROM __training_copy_state WHERE id=1').fetchone()
                    reusable=bool(record and json.loads(record[0]).get('identity')==self.identity and json.loads(record[0]).get('bootstrap_complete'))
            except (sqlite3.Error,ValueError):
                reusable=False
            if not reusable:
                # This is our disposable work file, never the original DB/WAL.
                for suffix in ('','-journal','-wal','-shm'):
                    Path(str(self.working)+suffix).unlink(missing_ok=True)
        from training_snapshot import install, capture, validate
        self.checkpoint()
        if self.working.exists():
            with closing(sqlite3.connect(self.working)) as old:
                saved=json.loads(old.execute('SELECT payload FROM __training_copy_state WHERE id=1').fetchone()[0])
                saved['source_schema']=json.loads(old.execute('SELECT payload FROM __training_source_schema WHERE id=1').fetchone()[0])
            with closing(sqlite3.connect(self.source.as_uri()+'?mode=ro',uri=True,timeout=.2)) as fence:
                validate(fence,saved['boundary'])
            self.state['boundary']=saved['boundary']
            self.state['source_schema']=saved['source_schema']
        else:
            # Registering the bounded fence IS a source metadata write. It uses
            # ordinary source admission/page limits; read-only resume does not.
            from storage_files import worker_storage_budget
            guard=worker_storage_budget()
            if guard.check(refresh=True)!='normal':
                raise guard.pressure()
            with closing(sqlite3.connect(self.source,timeout=.2)) as fence:
                page_size=fence.execute('PRAGMA page_size').fetchone()[0]
                fence.execute(f'PRAGMA max_page_count={max(1,guard.db_limit//page_size)}')
                install(fence)
                fence.commit()
                snapshot_started=time.perf_counter()
                fence.row_factory=sqlite3.Row
                fence.execute('BEGIN IMMEDIATE')
                try:
                    self.src=fence
                    self.state['boundary']=capture(fence,self.end)
                    self.state['source_schema']=[dict(row) for row in fence.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT GLOB '__training_*'").fetchall()]
                    self.agent_ids=[row[0] for row in fence.execute('SELECT id FROM agents WHERE target_entity=(SELECT target_entity FROM agents WHERE id=?)',(self.agent_id,))] if self.agent_id else []
                    self.initial_metadata=self.read_initial_metadata(fence)
                    fence.commit()
                    self.snapshot_max_seconds=max(self.snapshot_max_seconds,time.perf_counter()-snapshot_started)
                except Exception:
                    fence.rollback()
                    raise
        self.source_signature=signature(self.source)
        with closing(sqlite3.connect(self.source.as_uri()+'?mode=ro',uri=True,timeout=5)) as self.src, \
                closing(sqlite3.connect(self.working,timeout=5)) as self.dst:
            self.src.row_factory = sqlite3.Row
            self.src.execute('PRAGMA query_only=ON')
            self.src.execute(f'PRAGMA cache_size=-{self.memory//1024}')
            self.dst.execute(f'PRAGMA cache_size=-{self.memory//1024}')
            self.dst.execute(f'PRAGMA max_page_count={self.disk//4096}')
            def interrupt(throttle=True):
                from storage_files import worker_storage_budget
                from storage_budget import StoragePressure
                try:
                    before=TRAINING_BUDGET.snapshot().get('sleep_seconds',0.)
                    if throttle and not self.bootstrap:
                        TRAINING_BUDGET.checkpoint('training_history_sql_vm')
                    self.throttle+=max(0.,TRAINING_BUDGET.snapshot().get('sleep_seconds',0.)-before)
                    guard=worker_storage_budget()
                    if guard.check_operation('source_read',refresh=False)!='normal':
                        self.pressure=guard.pressure()
                        return 1
                except StoragePressure as exc:
                    self.pressure=exc
                    return 1
                return int(self.cancelled() or (self.in_recovery and self.active_recovery_cpu()>self.seconds))
            self.src.set_progress_handler(lambda: interrupt(False),10000)
            self.dst.set_progress_handler(interrupt,10000)
            self.schema = self.state['source_schema'] if 'source_schema' in self.state else []
            tables = {x['name']:x for x in self.schema if x['type']=='table' and not x['name'].startswith(('sqlite_','__training_'))}
            self.agent_ids = []
            if self.agent_id:
                self.agent_ids = [r[0] for r in self.src.execute('SELECT id FROM agents WHERE target_entity=(SELECT target_entity FROM agents WHERE id=?)',(self.agent_id,))]
            try:
                previous = self.dst.execute('SELECT payload FROM __training_copy_state WHERE id=1').fetchone()
            except sqlite3.OperationalError:
                previous = None
            if previous:
                self.state = json.loads(previous[0])
                self.state['source_schema']=json.loads(self.dst.execute('SELECT payload FROM __training_source_schema WHERE id=1').fetchone()[0])
                self.schema=self.state['source_schema']
                tables={x['name']:x for x in self.schema if x['type']=='table' and not x['name'].startswith(('sqlite_','__training_'))}
                if self.state['identity']!=self.identity:
                    raise RecoveryUnavailable('Preparation checkpoint source mismatch')
                self.resumed = True
                if self.dst.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_manifest'").fetchone():
                    ready=json.loads(self.dst.execute('SELECT payload FROM recovery_manifest').fetchone()[0])
                    if ready.get('status')=='READY' and self.dst.execute('PRAGMA quick_check').fetchone()[0]=='ok':
                        ready['resumed']=True
                        self.dst.close()
                        os.replace(self.working,self.destination)
                        return ready
            else:
                for item in tables.values():
                    self.dst.execute(item['sql'])
                self.dst.execute('CREATE TABLE __training_copy_state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
                # Immutable catalog is stored ONCE, not serialized/journalled with
                # every 128-row progress checkpoint on a multi-month archive.
                self.dst.execute('CREATE TABLE __training_source_schema(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
                self.dst.execute('INSERT INTO __training_source_schema VALUES(1,?)',(json.dumps(self.state['source_schema']),))
                self.save()
            from training_snapshot import validate
            # A bounded initial transaction captures mutable controller/context
            # metadata together. Large immutable prefixes use persisted fences.
            if not self.state.get('bootstrap_complete'):
                self.checkpoint()
                initial=self.initial_metadata
                if initial is None:
                    raise RecoveryUnavailable('Incomplete initial snapshot cannot be resumed; previous model retained')
                # All throttling, local disk writes and progress callbacks happen
                # AFTER the source reader is released, including configuration.
                for name,(rows,strategy) in initial.items():
                    metric=self.state['tables'].setdefault(name,dict(source_rows_scanned=0,rows_copied=0,bytes_copied=0,copy_strategy=strategy))
                    for offset in range(0,len(rows),128):
                        self.checkpoint()
                        batch=rows[offset:offset+128]
                        columns=batch[0].keys()
                        self.sql(self.dst,'executemany','INSERT OR IGNORE INTO '+quote(name)+' ('+','.join(map(quote,columns))+') VALUES('+','.join('?' for _ in columns)+')',[tuple(row) for row in batch])
                        metric['source_rows_scanned']+=len(batch)
                        metric['rows_copied']+=len(batch)
                        metric['bytes_copied']+=sum(sum(len(v.encode('utf-8')) if isinstance(v,str) else len(v) if isinstance(v,bytes) else 16 for v in row) for row in batch)
                    self.state['queries'][name]=dict(done=True)
                    self.save()
                    self.emit(name,strategy,True)
                self.state['bootstrap_complete']=True
                self.save()
                self.initial_metadata=None
                initial.clear()
                rows=batch=None
            if self.agent_id:
                self.agent_ids=[r[0] for r in self.dst.execute('SELECT id FROM agents WHERE target_entity=(SELECT target_entity FROM agents WHERE id=?)',(self.agent_id,))]
            # Normal metadata copying has no corruption time budget; all work still
            # uses cooperative CPU throttling, bounded batches, disk quota and cancel.
            for name,item in tables.items():
                if name in ('entity_history','agents','app_meta','entity_history_revision','rl_models','tiny_mlp_shadow_models'):
                    continue
                plan = self.table_plan(item)
                if plan is None:
                    self.state['tables'].setdefault(name,dict(source_rows_scanned=0,rows_copied=0,bytes_copied=0,copy_strategy='schema_only_not_a_training_dependency'))
                    continue
                predicate,params,strategy = plan
                if strategy=='entity_time':
                    for entity in self.entities:
                        self.copy(name,'entity_id=? AND event_time<=?',(entity,self.end),
                                  query_key=name+':'+entity,key='event_time,rowid',strategy='entity_time_keyset')
                else:
                    self.copy(name,predicate,params,strategy=strategy)
            def guard_row(table):
                metric=self.state['tables'][table]
                metric['version_guard_rows_scanned']=metric.get('version_guard_rows_scanned',0)+1
                if metric['version_guard_rows_scanned']%128==0:
                    self.checkpoint()
                    self.emit(table,'correct_version_guard')
            # Guard reads are separately visible; they are not copy output rows.
            for metric in self.state['tables'].values():
                metric['version_guard_rows_scanned']=0
            report = dict(contract=CONTRACT,status='BUILDING',source=str(self.source),agent_id=self.agent_id,
                gaps=[],skipped_records=None,archive_status='HISTORY_OK',source_signature=self.source_signature,
                correct_agent_ids=self.agent_ids or None,correct_fingerprint=correct_fingerprint(self.dst,self.agent_ids or None,guard_row),
                row_counter_semantics='payload rows fetched after SQL predicates; version guards separate; catalog/key probes excluded',
                sqlite_wait_estimated=True)
            report['model_fingerprint'] = self.state['model_fingerprint']
            if self.agent_id:
                report['neural_fingerprint'] = self.state.get('neural_fingerprint')
            failed = []
            for entity in self.entities:
                try:
                    self.copy('entity_history','entity_id=? AND ts<=?',(entity,self.end),query_key='history:'+entity,
                              history=True,key='ts,id',strategy='entity_time_keyset')
                except sqlite3.DatabaseError as exc:
                    if not is_sqlite_corruption(exc):
                        raise
                    failed.append(entity)
                    report.update(archive_status='ARCHIVE_CORRUPTED',sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),
                                  sqlite_errorname=getattr(exc,'sqlite_errorname',None),recovery_strategy='selected_covering_keys')
            if failed:
                self.recover(failed,report)
            for gap in http_gaps:
                if gap.get('entity_id') in self.entities:
                    report['gaps'].append(dict(entity_id=gap['entity_id'],start_ts=float(gap.get('start_ts',gap.get('start'))),
                        end_ts=float(gap.get('end_ts',gap.get('end'))),source='ha_recorder',
                        failure_reason='HISTORY_GAP_HTTP500' if not gap.get('failure_reason') or 'HTTP500' in gap.get('failure_reason','') else 'HISTORY_UNKNOWN',recovery_attempts=0))
            for item in self.schema:
                if item['type']=='index':
                    self.checkpoint()
                    self.sql(self.dst,'execute',item['sql'].replace('CREATE INDEX ','CREATE INDEX IF NOT EXISTS ',1).replace('CREATE UNIQUE INDEX ','CREATE UNIQUE INDEX IF NOT EXISTS ',1))
            report.update(recovered_records=self.dst.execute('SELECT COUNT(*) FROM entity_history').fetchone()[0],attempts=self.attempts,
                recovery_wall_seconds=time.monotonic()-self.started,recovery_active_cpu_seconds=self.recovery_cpu,
                preparation_cpu_seconds=time.process_time()-self.cpu_started,throttle_seconds=self.throttle,
                sqlite_wait_seconds=max(0.,self.sqlite_elapsed-self.sqlite_cpu-self.lock_wait),lock_wait_seconds=self.lock_wait,
                copy_tables=self.state['tables'],resumed=self.resumed,availability='PARTIAL' if report['gaps'] else 'HISTORY_OK')
            self.dst.execute('CREATE TABLE IF NOT EXISTS recovery_projection(id INTEGER PRIMARY KEY,lo REAL,hi REAL)')
            self.dst.execute('INSERT OR REPLACE INTO recovery_projection VALUES(1,NULL,NULL)')
            if report['gaps']:
                self.dst.execute('ALTER TABLE entity_history RENAME TO recovery_rows')
                self.dst.execute('CREATE VIEW entity_history AS SELECT * FROM recovery_rows WHERE (SELECT lo FROM recovery_projection) IS NULL OR (ts>=(SELECT lo FROM recovery_projection) AND ts<=(SELECT hi FROM recovery_projection) AND (received_ts IS NULL OR received_ts<=(SELECT hi FROM recovery_projection)))')
                if 'feature_observation_events' in tables:
                    self.dst.execute('ALTER TABLE feature_observation_events RENAME TO recovery_observation_rows')
                    self.dst.execute('CREATE VIEW feature_observation_events AS SELECT * FROM recovery_observation_rows WHERE (SELECT lo FROM recovery_projection) IS NULL OR (event_time>=(SELECT lo FROM recovery_projection) AND event_time<=(SELECT hi FROM recovery_projection) AND received_time<=(SELECT hi FROM recovery_projection))')
            else:
                self.dst.execute('CREATE VIEW recovery_rows AS SELECT * FROM entity_history')
            validate(self.src,self.state['boundary'])
            report['snapshot_lifecycle']=dict(max_explicit_transaction_seconds=self.snapshot_max_seconds,
                max_source_execute_seconds=self.source_sql_max_seconds,wal_peak_bytes=self.wal_peak_bytes,
                owned_source_connections=1,owned_generation_connections=1,
                source_readers_between_batches=0,reader_count_scope='owned preparation only; SQLite exposes no global reader count',
                consistency='durable rowid prefix fences; updates/deletes/reused ids invalidate; new appends deferred',
                boundary=self.state['boundary'])
            report['status']='READY'
            self.dst.execute('CREATE TABLE recovery_manifest(payload TEXT NOT NULL)')
            self.dst.execute('INSERT INTO recovery_manifest VALUES(?)',(json.dumps(report),))
            self.dst.commit()
            if self.sql(self.dst,'execute','PRAGMA quick_check').fetchone()[0]!='ok':
                raise RecoveryUnavailable('Recovered generation failed quick_check')
            self.emit('entity_history','complete',True)
        os.replace(self.working,self.destination)
        return report

    def recover(self, failed, report):
        """Prefer selected entity keys from a healthy covering index, never all keys."""
        self.in_recovery = True
        self.recovery_started = time.process_time()
        cache_key=hashlib.sha256((str(self.source)+'|'+','.join(failed)).encode()).hexdigest()[:20]
        cache_path=self.destination.parent/('.quarantine-'+cache_key+'.json')
        try:
            cache=json.loads(cache_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            cache={}
        known={int(k):v for k,v in cache.get('missing',{}).items()} if cache.get('signature')==self.source_signature else {}
        missing=dict(known)
        report['quarantine_cache_hit']=bool(known)
        self.dst.execute('CREATE TABLE IF NOT EXISTS recovery_witness(id INTEGER PRIMARY KEY,entity_id TEXT,ts REAL)')
        self.dst.commit()
        mapping = False
        for index in ('idx_entity_history_entity_ts','sqlite_autoindex_entity_history_1'):
            self.checkpoint()
            self.dst.execute('DELETE FROM recovery_witness')
            try:
                for entity in failed:
                    last=None
                    while True:
                        self.checkpoint()
                        predicate=' AND (ts,id)>(?,?)' if last else ''
                        params=(entity,self.end,self.state['boundary']['tables']['entity_history']['upper'])+tuple((last[0],last[1]) if last else ())
                        with closing(self.sql(self.src,'execute',f'SELECT id,entity_id,ts FROM entity_history INDEXED BY {quote(index)} WHERE entity_id=? AND ts<=? AND rowid<=?'+predicate+' ORDER BY ts,id LIMIT 128',params)) as cursor:
                            batch=self.sql(cursor,'fetchall')
                        if not batch:
                            break
                        advancing_cursor(last, (batch[-1]['ts'],batch[-1]['id']), table='entity_history', strategy='recovery_witness', rows=len(batch), job_id=self.destination.stem, query_key=entity)
                        self.dst.executemany('INSERT OR IGNORE INTO recovery_witness VALUES(?,?,?)',[tuple(r) for r in batch])
                        self.dst.commit()
                        last=(batch[-1]['ts'],batch[-1]['id'])
                mapping=True
                break
            except sqlite3.DatabaseError as exc:
                if not is_sqlite_corruption(exc) and 'no such index' not in str(exc):
                    raise
        self.dst.commit()
        def read_keys(keys, depth=0):
            fresh=[]
            for key in keys:
                if key in known:
                    report['gaps'].append(known[key])
                else:
                    fresh.append(key)
            keys=fresh
            if not keys:
                return
            self.checkpoint()
            self.attempts += 1
            if self.attempts>self.limit:
                raise RecoveryUnavailable('History recovery attempt budget exhausted; previous model retained')
            try:
                marks=','.join('?' for _ in keys)
                with closing(self.sql(self.src,'execute',f'SELECT * FROM entity_history NOT INDEXED WHERE id IN ({marks}) ORDER BY id',keys)) as cursor:
                    rows=self.sql(cursor,'fetchall')
                if rows:
                    size=self.validate_history(rows)
                    columns=rows[0].keys()
                    before=self.dst.total_changes
                    self.dst.executemany('INSERT OR IGNORE INTO entity_history VALUES('+','.join('?' for _ in columns)+')',[tuple(r) for r in rows])
                    metric=self.state['tables']['entity_history']
                    metric['source_rows_scanned']+=len(rows)
                    metric['rows_copied']+=self.dst.total_changes-before
                    metric['bytes_copied']+=size
                self.dst.commit()
            except sqlite3.DatabaseError as exc:
                if not is_sqlite_corruption(exc):
                    raise
                if len(keys)==1 or depth>=self.depth:
                    for key in keys:
                        row=self.dst.execute('SELECT entity_id,ts FROM recovery_witness WHERE id=?',(key,)).fetchone()
                        if row:
                            gap=dict(entity_id=row[0],start_ts=row[1],end_ts=row[1],source='local_archive',failure_reason='HISTORY_GAP_SQLITE_CORRUPT',recovery_attempts=self.attempts)
                            report['gaps'].append(gap)
                            missing[key]=gap
                    return
                mid=len(keys)//2
                read_keys(keys[:mid],depth+1);read_keys(keys[mid:],depth+1)
        if mapping:
            with closing(self.dst.execute('SELECT w.id FROM recovery_witness w WHERE NOT EXISTS(SELECT 1 FROM entity_history h WHERE h.id=w.id) ORDER BY w.id')) as cursor:
                while True:
                    batch=cursor.fetchmany(128)
                    if not batch:
                        break
                    read_keys([r[0] for r in batch])
                    self.emit('entity_history','selected_covering_keys')
        else:
            # With every entity index unreadable there is no trustworthy id map.
            # Retain 0.15.6's bounded PK-range fallback, filtering in SQLite, never
            # decoding/copying another device's history into Python or the generation.
            maximum=self.state['boundary']['tables']['entity_history']['upper']
            marks=','.join('?' for _ in failed)
            def physical(lo,hi,depth=0):
                self.checkpoint();self.attempts+=1
                if self.attempts>self.limit:
                    raise RecoveryUnavailable('History recovery attempt budget exhausted; previous model retained')
                try:
                    with closing(self.sql(self.src,'execute',f'SELECT * FROM entity_history NOT INDEXED WHERE id BETWEEN ? AND ? AND entity_id IN ({marks}) AND ts<=? ORDER BY id',(lo,hi,*failed,self.end))) as cursor:
                        rows=self.sql(cursor,'fetchall')
                    if rows:
                        size=self.validate_history(rows)
                        before=self.dst.total_changes
                        self.dst.executemany('INSERT OR IGNORE INTO entity_history VALUES('+','.join('?' for _ in rows[0].keys())+')',[tuple(r) for r in rows])
                        metric=self.state['tables']['entity_history']
                        metric['source_rows_scanned']+=len(rows)
                        metric['rows_copied']+=self.dst.total_changes-before
                        metric['bytes_copied']+=size
                    self.dst.commit()
                except sqlite3.DatabaseError as exc:
                    if not is_sqlite_corruption(exc):
                        raise
                    if lo==hi or depth>=self.depth:
                        report['gaps'].append(dict(entity_id='*',start_ts=self.start,end_ts=self.end,source='local_archive',failure_reason='HISTORY_UNKNOWN',recovery_attempts=self.attempts))
                        return
                    mid=(lo+hi)//2;physical(lo,mid,depth+1);physical(mid+1,hi,depth+1)
            for lo in range(1,maximum+1,128):
                physical(lo,min(maximum,lo+127))
            report['recovery_strategy']='bounded_pk_ranges_sql_filtered_no_readable_entity_index'
        if signature(self.source)==self.source_signature:
            temp=cache_path.with_suffix('.'+self.destination.name+'.tmp')
            temp.write_text(json.dumps(dict(signature=self.source_signature,missing=missing)),encoding='utf-8')
            os.replace(temp,cache_path)
        self.recovery_cpu=self.active_recovery_cpu()
        self.recovery_started=None
        self.in_recovery=False


def prepare_generation(source,destination,entities,start,end,*,options=None,cancelled=lambda:False,
                       progress=lambda **kw:None,http_gaps=(),agent_id=None,rebuild=False):
    return Preparation(source,destination,entities,start,end,options,cancelled,progress,agent_id,rebuild).run(http_gaps)
