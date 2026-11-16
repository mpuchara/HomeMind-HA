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
import shutil
from pathlib import Path
import sqlite3
import time

from history_recovery import CORRECT_TABLES, RecoveryUnavailable, quote, correct_fingerprint, correct_predicate
from sqlite_background import is_sqlite_corruption, is_sqlite_busy
from training_budget import TRAINING_BUDGET
from storage_budget import StoragePressure

CONTRACT = 'training-history-generation-v7'
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


MIB = 1024 * 1024

def scratch_bytes(path):
    total = 0
    for suffix in ('', '-journal', '-wal', '-shm'):
        try:
            total += Path(str(path)+suffix).stat().st_size
        except FileNotFoundError:
            pass
    return total


def generation_capacity(source, working, guard):
    """Normal copy capacity, not the corruption-salvage budget.

    Keep room for a second generation-sized allocation (rollback journal,
    indexes or subsequent writes) and the parent's frozen HA reserve.
    Existing committed scratch is credited only to this job, never to peers.
    """
    free = shutil.disk_usage(Path(working).parent).free
    try:
        existing = Path(working).stat().st_size
    except FileNotFoundError:
        existing = 0
    headroom = max(0, free - guard.reserve)
    estimate = max(512*MIB, 2*scratch_bytes(source) + 64*MIB)
    # Credit only the existing main file, once. Existing sidecars already use
    # filesystem space and cannot be credited towards max_page_count.
    return min(int(guard.db_limit), estimate, (existing + headroom)//2)


def generation_pressure(reason, working, limit, reserve):
    free = shutil.disk_usage(Path(working).parent).free
    error = StoragePressure(
        f'Training generation storage: {reason}; scratch={scratch_bytes(working)} bytes, '
        f'generation_limit={limit} bytes, filesystem_free={free} bytes, '
        f'HA_reserve={reserve} bytes; previous model and committed copy retained')
    error.reason = reason
    return error


def sleep_total():
    metrics = TRAINING_BUDGET.snapshot()
    return metrics.get('throttle_sleep_seconds', 0.) + metrics.get('interactive_sleep_seconds', 0.)


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
        self.options, self.cancelled, self.progress = options or {}, cancelled, progress
        self.end, self.agent_id = float(end), agent_id
        self.start = max(float(start), self.end-7*86400.)
        self.target_entities = list(self.entities)  # legacy/unscoped caller
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
        self.recovery_disk = max(16,min(2048,int(self.options.get('max_recovery_disk_mb',512))))*MIB
        self.disk = self.recovery_disk  # recalculated from the actual filesystem at admission
        self.recovery_start_bytes = 0
        self.reserve = 0
        self.source_signature = signature(self.source)
        self.identity = dict(contract=CONTRACT,source=str(self.source),entities=self.entities,
                             start=self.start,end=self.end,agent_id=agent_id,rebuild=self.rebuild)
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
        self.operation = None
        self.operation_vm_steps = 0
        self.operation_started = 0.
        self.last_prefix_check = 0.
        self.failure_stage = 'admission'

    def checkpoint(self):
        if self.cancelled():
            raise InterruptedError('Training history preparation cancelled; copy checkpoint retained')
        before = sleep_total()
        if not self.bootstrap:
            TRAINING_BUDGET.checkpoint('training_history_preparation')
        self.throttle += max(0.,sleep_total()-before)
        from storage_files import worker_storage_budget
        guard=worker_storage_budget()
        if guard.check_operation('source_read',refresh=True)!='normal':
            raise guard.pressure()
        wal=Path(str(self.source)+'-wal')
        self.wal_peak_bytes=max(self.wal_peak_bytes,wal.stat().st_size if wal.exists() else 0)
        if time.monotonic()-self.last_disk_check>1.:
            self.last_disk_check=time.monotonic()
            if shutil.disk_usage(self.working.parent).free < self.reserve + 8*MIB:
                raise generation_pressure('generation_disk_reserve', self.working, self.disk, self.reserve)
        # Fail early on a real in-scope mutation instead of spending hours
        # copying a generation we already know cannot be published.
        if self.state.get('boundary',{}).get('entity_scope') is not None and getattr(self,'src',None) is not None:
            now=time.monotonic()
            if now-self.last_prefix_check>=30.:
                from training_snapshot import validate
                validate(self.src,self.state['boundary'])
                self.last_prefix_check=now
        if self.in_recovery and scratch_bytes(self.working)-self.recovery_start_bytes > self.recovery_disk:
            raise RecoveryUnavailable('Additional corruption recovery disk budget exhausted; previous model retained')
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

    def begin_operation(self, name):
        """Finalization has no history cursor. Publish its own measured work."""
        self.operation = name
        self.failure_stage='finalize:'+name
        self.operation_vm_steps = 0
        self.operation_started = time.monotonic()
        self.emit('entity_history','generation_finalization',True)

    def operation_step(self, steps=10000):
        if self.operation is not None:
            self.operation_vm_steps += steps
            self.emit('entity_history','generation_finalization')

    def end_operation(self):
        self.operation = None

    def emit(self, table, strategy, force=False):
        now = time.monotonic()
        if not force and now-self.last_report < 1.:
            return
        self.last_report = now
        metric = dict(self.state['tables'].get(table) or {})
        metric.update(agent_id=self.agent_id,table_name=table,copy_strategy=strategy,
            stage="Recovering corruption" if self.in_recovery else "Preparing generation",
            elapsed_seconds=round(now-self.started,6),cpu_seconds=round(time.process_time()-self.cpu_started,6),
            sqlite_wait_seconds=round(max(0.,self.sqlite_elapsed-self.sqlite_cpu-self.lock_wait),6),
            sqlite_seconds=round(self.sqlite_elapsed,6),lock_wait_seconds=round(self.lock_wait,6),
            throttle_seconds=round(self.throttle,6),recovery_attempts=self.attempts,
            wal_peak_bytes=self.wal_peak_bytes,source_owned_connections=1,
            explicit_source_transaction_active=bool(self.src.in_transaction),
            snapshot_max_seconds=self.snapshot_max_seconds,
            job_id=self.destination.stem, commit_seconds=self.commit_seconds,
            status_write_seconds=self.status_write_seconds, generation_limit_bytes=self.disk,
            generation_scratch_bytes=scratch_bytes(self.working), recovery_disk_limit_bytes=self.recovery_disk)
        message = f"{'Recovering historical data' if self.in_recovery else 'Preparing training history'}: {table}, {metric.get('rows_copied',0):,} records copied"
        detail = 'Recovering historical data' if self.in_recovery else 'Copying required metadata / selected history'
        if self.operation is not None:
            metric.update(stage='Finalizing generation',operation=self.operation,
                          operation_vm_steps=self.operation_vm_steps,
                          operation_elapsed_seconds=round(now-self.operation_started,3),
                          query_key=None,current_cursor=None,previous_cursor=None)
            message = 'Finalizing training generation: '+self.operation
            detail = self.operation
        status_started = time.monotonic()
        self.progress(message=message,
            phase_detail=detail,
            preparation=metric,work_done=metric.get('rows_copied',0),work_total=0,
            progress=min(.09,.01+.08*len([x for x in self.state['queries'].values() if x.get('done')])/(len(self.schema)+len(self.entities)+1)))
        self.status_write_seconds += time.monotonic()-status_started

    def save(self, force=True):
        # Rows and the cursor always commit together. Bound uncommitted work by
        # 64 bounded pages, the payload memory budget, or five seconds of work.
        # Scratch is rebuildable. Rows and their cursor still commit together.
        if not force and self.pending_batches < 64 and self.pending_bytes < self.memory and time.monotonic()-self.last_commit < 5.:
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

    def read_page(self, cursor, limit):
        """Consume a keyset page under a byte budget with bounded read-ahead.

        The next query starts at the last *accepted* row, so a row read ahead
        but not copied remains visible on the following page. No source reader
        survives this call's enclosing closing(cursor) block.
        """
        rows, size, fetched = [], 0, 0
        while len(rows) < limit:
            chunk = self.sql(cursor,'fetchmany',min(32,limit-len(rows)))
            if not chunk:
                break
            fetched += len(chunk)
            for row in chunk:
                row_size = sum(len(v.encode('utf-8')) if isinstance(v,str) else
                               len(v) if isinstance(v,bytes) else 16 for v in row)
                if size + row_size > self.memory:
                    if not rows:
                        raise RecoveryUnavailable('Preparation row exceeds memory budget')
                    return rows, size, fetched
                rows.append(row)
                size += row_size
        return rows, size, fetched

    def copy(self, table, predicate='', params=(), *, query_key=None, history=False, key='rowid', checkpoint=True, strategy='scoped_sql', physical=False):
        """Keyset paging uses the source's entity/time or agent index, before decoding."""
        query_key = query_key or table
        self.failure_stage='copy:'+query_key
        saved = self.state['queries'].get(query_key,{}) if checkpoint else {}
        if saved.get('done'):
            return
        metric = self.state['tables'].setdefault(table,dict(source_rows_scanned=0,rows_copied=0,bytes_copied=0,copy_strategy=strategy))
        last = saved.get('last')
        metric.update(query_key=query_key,previous_cursor=last,current_cursor=last)
        self.emit(table,strategy,True)
        # Larger archive pages amortize source seeks, guards and durable cursor writes.
        # Entity-scoped metadata has the same indexed, bounded reader contract.
        page_limit = 512 if history or strategy=='entity_time_keyset' else 128
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
            sql = f'SELECT {extra}* FROM {quote(table)}'+(' NOT INDEXED' if physical else '')+(' WHERE '+' AND '.join(where) if where else '')+f' ORDER BY {key} LIMIT {page_limit}'
            with closing(self.sql(self.src,'execute',sql,values)) as cursor:
                batch, size, fetched = self.read_page(cursor,page_limit)
            if not batch:
                if checkpoint:
                    self.state['queries'][query_key] = dict(done=True,last=last)
                    if strategy=='entity_time_keyset':
                        # Empty/small entities no longer cause one fsync apiece.
                        self.pending_batches += 1
                        self.save(force=False)
                    else:
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
            metric['payload_rows_fetched'] = metric.get('payload_rows_fetched',metric['source_rows_scanned']) + fetched
            metric['payload_read_ahead_rows'] = metric.get('payload_read_ahead_rows',0) + fetched-len(batch)
            metric['source_rows_scanned'] += len(batch)
            metric['copy_pages'] = metric.get('copy_pages',0)+1
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
            # Historical replay only loads provenance for target_history_id,
            # never for every high-rate context/radar sensor.
            selected = self.target_entities if name.startswith('provenance_') else self.entities
            slots = ','.join('?' for _ in selected)
            return f'entity_id IN ({slots}) AND event_time>=? AND event_time<=?',(*selected,self.start,self.end),'entity_time'
        if name in ('home_checkpoints','adaptive_presence_checkpoints'):
            # Keep all as-of checkpoints that recent/optional sparse replay can
            # consume, plus its one older seed. A single start checkpoint would
            # silently change later logical chunks and older sparse replay.
            days=max(1.,min(7.,float(self.options.get('agent_training_history_days',7))),
                     min(7.,float(self.options.get('agent_training_long_memory_days',7))))
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
            except sqlite3.Error as exc:
                # A busy reader does not prove that committed scratch is invalid.
                if is_sqlite_busy(exc):
                    raise
                reusable=False
            except ValueError:
                reusable=False
            if not reusable:
                # This is our disposable work file, never the original DB/WAL.
                for suffix in ('','-journal','-wal','-shm'):
                    Path(str(self.working)+suffix).unlink(missing_ok=True)
        from training_snapshot import install, capture, validate
        from storage_files import worker_storage_budget
        guard = worker_storage_budget()
        self.reserve = guard.reserve
        self.disk = generation_capacity(self.source, self.working, guard)
        if self.disk < 16*MIB or self.disk < scratch_bytes(self.working):
            raise generation_pressure('generation_capacity', self.working, self.disk, self.reserve)
        self.checkpoint()
        self.failure_stage='validate_retained_prefix' if self.working.exists() else 'source_fence'
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
            with closing(sqlite3.connect(self.source,timeout=2.)) as fence:
                fence.execute('PRAGMA synchronous=NORMAL')
                page_size=fence.execute('PRAGMA page_size').fetchone()[0]
                fence.execute(f'PRAGMA max_page_count={max(1,guard.db_limit//page_size)}')
                fence.row_factory=sqlite3.Row
                # Acquire the writer slot before any reads/ledger writes, avoiding
                # a deferred read->write upgrade (SQLITE_BUSY_SNAPSHOT).
                # Install and capture must use one transaction, not two acquisitions.
                fence.execute('BEGIN IMMEDIATE')
                snapshot_started=time.perf_counter()
                try:
                    install(fence)
                    self.src=fence
                    if self.agent_id:
                        target=fence.execute('SELECT target_entity FROM agents WHERE id=?',(self.agent_id,)).fetchone()
                        if target is None:
                            raise RecoveryUnavailable('Selected agent is missing')
                        self.target_entities=[target[0]]
                    self.state['boundary']=capture(fence,self.end,self.entities,
                        entity_scopes={'provenance_events':self.target_entities,
                                       'provenance_history_links':self.target_entities})
                    self.state['source_schema']=[dict(row) for row in fence.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT GLOB '__training_*'").fetchall()]
                    self.agent_ids=[row[0] for row in fence.execute('SELECT id FROM agents WHERE target_entity=(SELECT target_entity FROM agents WHERE id=?)',(self.agent_id,))] if self.agent_id else []
                    self.initial_metadata=self.read_initial_metadata(fence)
                    fence.commit()
                    self.snapshot_max_seconds=max(self.snapshot_max_seconds,time.perf_counter()-snapshot_started)
                except Exception:
                    fence.rollback()
                    raise
        self.failure_stage='open_generation'
        self.source_signature=signature(self.source)
        with closing(sqlite3.connect(self.source.as_uri()+'?mode=ro',uri=True,timeout=5)) as self.src, \
                closing(sqlite3.connect(self.working,timeout=5)) as self.dst:
            self.src.row_factory = sqlite3.Row
            self.src.execute('PRAGMA query_only=ON')
            # Payload budget and SQLite page cache are different resources.
            # Split the already RAM-admitted worker cache across these two connections.
            cache_mb = max(2,min(256,int(self.options.get('training_worker_effective_sqlite_cache_mb',16))))
            self.src.execute(f'PRAGMA cache_size=-{cache_mb*512}')
            self.dst.execute(f'PRAGMA cache_size=-{cache_mb*512}')
            # Only the private, disposable copy uses WAL/NORMAL. This removes
            # the two filesystem syncs per tiny rollback-journal transaction.
            # A lost power-cycle tail is reconstructed from the atomic cursor.
            self.dst.execute('PRAGMA journal_mode=WAL').fetchone()
            self.dst.execute('PRAGMA synchronous=NORMAL')
            self.dst.execute('PRAGMA wal_autocheckpoint=1024')
            self.dst.execute('PRAGMA journal_size_limit=4194304')
            page_size = self.dst.execute('PRAGMA page_size').fetchone()[0]
            self.dst.execute(f'PRAGMA max_page_count={self.disk//page_size}')
            def interrupt(throttle=True):
                from storage_files import worker_storage_budget
                from storage_budget import StoragePressure
                try:
                    before=sleep_total()
                    if throttle and not self.bootstrap:
                        TRAINING_BUDGET.checkpoint('training_history_sql_vm')
                    self.throttle+=max(0.,sleep_total()-before)
                    guard=worker_storage_budget()
                    if guard.check_operation('source_read',refresh=False)!='normal':
                        self.pressure=guard.pressure()
                        return 1
                except StoragePressure as exc:
                    self.pressure=exc
                    return 1
                if self.cancelled() or (self.in_recovery and self.active_recovery_cpu()>self.seconds):
                    return 1
                # VM counters are accepted only in named finalization operations,
                # never as a substitute for advancing an ordinary copy cursor.
                if throttle:
                    try:
                        self.operation_step()
                    except StoragePressure as exc:
                        self.pressure=exc
                        return 1
                return 0
            self.src.set_progress_handler(lambda: interrupt(False),10000)
            self.dst.set_progress_handler(interrupt,10000)
            self.schema = self.state['source_schema'] if 'source_schema' in self.state else []
            tables = {x['name']:x for x in self.schema if x['type']=='table' and not x['name'].startswith(('sqlite_','__training_'))}
            self.agent_ids = []
            if self.agent_id:
                self.agent_ids = [r[0] for r in self.src.execute('SELECT id FROM agents WHERE target_entity=(SELECT target_entity FROM agents WHERE id=?)',(self.agent_id,))]
            try:
                previous = self.dst.execute('SELECT payload FROM __training_copy_state WHERE id=1').fetchone()
            except sqlite3.OperationalError as exc:
                if is_sqlite_busy(exc) or 'no such table' not in str(exc).lower():
                    raise
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
                    self.begin_operation('Validate retained generation (quick_check)')
                    valid_ready = ready.get('status')=='READY' and self.sql(self.dst,'execute','PRAGMA quick_check').fetchone()[0]=='ok'
                    self.end_operation()
                    if valid_ready:
                        ready['resumed']=True
                        ready['generation_limit_bytes']=self.disk
                        self.finish_scratch()
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
                target=self.dst.execute('SELECT target_entity FROM agents WHERE id=?',(self.agent_id,)).fetchone()
                if target is None:
                    raise RecoveryUnavailable('Selected agent is missing from frozen configuration')
                self.target_entities=[target[0]]
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
                    # Copy these after the authoritative history, including at
                    # most one pre-window causal seed's target provenance.
                    continue
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
                row_counter_semantics='source_rows_scanned counts accepted rows; payload_rows_fetched includes bounded read-ahead; version guards separate; catalog/key probes excluded',
                sqlite_wait_estimated=True)
            report['model_fingerprint'] = self.state['model_fingerprint']
            if self.agent_id:
                report['neural_fingerprint'] = self.state.get('neural_fingerprint')
            failed = []
            for entity in self.entities:
                try:
                    self.copy('entity_history','entity_id=? AND ts>=? AND ts<=?',(entity,self.start,self.end),query_key='history:'+entity,
                              history=True,key='ts,id',strategy='entity_time_keyset')
                except sqlite3.DatabaseError as exc:
                    if not is_sqlite_corruption(exc):
                        raise
                    failed.append(entity)
                    report.update(archive_status='ARCHIVE_CORRUPTED',sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),
                                  sqlite_errorname=getattr(exc,'sqlite_errorname',None),recovery_strategy='selected_covering_keys')
            self.save()
            if failed:
                self.recover(failed,report)
            # A single older row is a causal seed, never a replay sample.
            # An unreadable old seed is not evidence of any state; wait for
            # the first actual observation in the seven-day window instead.
            upper=self.state['boundary']['tables']['entity_history']['upper']
            for entity in self.entities:
                try:
                    self.copy('entity_history',
                        'entity_id=? AND id=(SELECT id FROM entity_history WHERE entity_id=? AND ts<? AND rowid<=? ORDER BY ts DESC,id DESC LIMIT 1)',
                        (entity,entity,self.start,upper),query_key='seed:'+entity,
                        history=True,key='ts,id',strategy='entity_time_keyset')
                except sqlite3.DatabaseError as exc:
                    if not is_sqlite_corruption(exc):
                        raise
                    self.state.setdefault('unreadable_causal_seeds',[])
                    if entity not in self.state['unreadable_causal_seeds']:
                        self.state['unreadable_causal_seeds'].append(entity)
            self.save()
            for name in ('provenance_events','provenance_history_links','feature_observation_events'):
                if name not in tables:
                    continue
                selected=self.target_entities if name.startswith('provenance_') else self.entities
                for entity in selected:
                    self.copy_event_window(name,entity,self.start,self.end)
                    if name.startswith('provenance_'):
                        seed=self.dst.execute('SELECT ts FROM entity_history WHERE entity_id=? AND ts<? LIMIT 1',(entity,self.start)).fetchone()
                        if seed:
                            self.copy_event_window(name,entity,float(seed[0]),float(seed[0]),suffix=':seed')
                self.save()
            report['gaps'].extend(self.state.get('metadata_gaps',[]))
            report['unreadable_causal_seeds']=self.state.get('unreadable_causal_seeds',[])
            report['metadata_recoveries']=self.state.get('metadata_recoveries',[])
            report['training_window']=dict(start_ts=self.start,end_ts=self.end,max_days=7,
                target_provenance_entities=self.target_entities,causal_seed_only_before_start=True)
            if report['metadata_recoveries']:
                report['archive_status']='ARCHIVE_CORRUPTED'
            validate(self.src,self.state['boundary'])
            for gap in http_gaps:
                if gap.get('entity_id') in self.entities:
                    report['gaps'].append(dict(entity_id=gap['entity_id'],start_ts=float(gap.get('start_ts',gap.get('start'))),
                        end_ts=float(gap.get('end_ts',gap.get('end'))),source='ha_recorder',
                        failure_reason='HISTORY_GAP_HTTP500' if not gap.get('failure_reason') or 'HTTP500' in gap.get('failure_reason','') else 'HISTORY_UNKNOWN',recovery_attempts=0))
            for item in self.schema:
                if item['type']=='index':
                    self.begin_operation('Build index '+item['name'])
                    self.checkpoint()
                    self.sql(self.dst,'execute',item['sql'].replace('CREATE INDEX ','CREATE INDEX IF NOT EXISTS ',1).replace('CREATE UNIQUE INDEX ','CREATE UNIQUE INDEX IF NOT EXISTS ',1))
                    self.dst.commit()
                    self.end_operation()
            self.begin_operation('Count copied history')
            copied_records=self.sql(self.dst,'execute','SELECT COUNT(*) FROM entity_history').fetchone()[0]
            self.end_operation()
            report.update(recovered_records=copied_records,attempts=self.attempts,
                recovery_wall_seconds=time.monotonic()-self.started,recovery_active_cpu_seconds=self.recovery_cpu,
                preparation_cpu_seconds=time.process_time()-self.cpu_started,throttle_seconds=self.throttle,
                sqlite_wait_seconds=max(0.,self.sqlite_elapsed-self.sqlite_cpu-self.lock_wait),lock_wait_seconds=self.lock_wait,
                generation_limit_bytes=self.disk, recovery_disk_limit_bytes=self.recovery_disk,
                copy_tables=self.state['tables'],resumed=self.resumed,availability='PARTIAL' if report['gaps'] else 'HISTORY_OK')
            self.begin_operation('Finalize recovery projection and manifest')
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
            self.end_operation()
            self.begin_operation('Validate generation (quick_check)')
            if self.sql(self.dst,'execute','PRAGMA quick_check').fetchone()[0]!='ok':
                raise RecoveryUnavailable('Recovered generation failed quick_check')
            self.end_operation()
            self.finish_scratch()
            self.emit('entity_history','complete',True)
        os.replace(self.working,self.destination)
        return report

    def finish_scratch(self):
        """Publish a standalone, durable DB, never rename a live WAL pair."""
        self.begin_operation('Checkpoint isolated generation WAL')
        self.dst.execute('PRAGMA synchronous=FULL')
        busy, _, _ = self.dst.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if busy:
            raise RecoveryUnavailable('Isolated generation checkpoint busy')
        mode = self.dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
        if mode.lower()!='delete':
            raise RecoveryUnavailable('Isolated generation WAL not sealed')
        self.end_operation()

    def copy_event_window(self, table, entity, lo, hi, suffix=''):
        """Required provenance/observation metadata: read-only recovery, no fake origin."""
        key=table+':'+entity+suffix
        predicate='entity_id=? AND event_time>=? AND event_time<=?'
        params=(entity,lo,hi)
        try:
            self.copy(table,predicate,params,query_key=key,key='event_time,rowid',strategy='entity_time_keyset')
            return
        except sqlite3.DatabaseError as exc:
            if not is_sqlite_corruption(exc):
                raise
        self.in_recovery=True
        self.recovery_started=time.process_time()
        self.recovery_start_bytes=scratch_bytes(self.working)
        recovery=dict(table=table,entity_id=entity,start_ts=lo,end_ts=hi,strategy='covering_index')
        try:
            self.recover_event_keys(table,entity,lo,hi)
        except sqlite3.DatabaseError as exc:
            if not is_sqlite_corruption(exc):
                raise
            # An index can be damaged while every payload row is healthy.
            # One rowid-ordered physical pass, not repeated OFFSET scans.
            recovery['strategy']='physical_rowid'
            try:
                self.copy(table,predicate,params,query_key=key+':physical',key='rowid',
                          strategy='entity_time_keyset',physical=True)
            except sqlite3.DatabaseError as physical_error:
                if not is_sqlite_corruption(physical_error):
                    raise
                raise RecoveryUnavailable(
                    f'Unmapped SQLite damage in {table}, entity={entity}, interval={lo}..{hi}; '
                    'cannot verify training provenance; previous model retained') from physical_error
        finally:
            self.recovery_cpu=self.active_recovery_cpu()
            self.recovery_started=None
            self.in_recovery=False
        self.state.setdefault('metadata_recoveries',[]).append(recovery)
        self.state['queries'][key]=dict(done=True,last=self.state['queries'].get(key,{}).get('last'))
        self.save()
        self.emit(table,'metadata_recovered',True)

    def recover_event_keys(self, table, entity, lo, hi):
        """Map damaged payload rows using independently readable time/rowid keys."""
        indexes=[]
        for row in self.src.execute('PRAGMA index_list('+quote(table)+')').fetchall():
            name=row[1]
            cols=[r[2] for r in self.src.execute('PRAGMA index_info('+quote(name)+')')]
            if cols[:2]==['entity_id','event_time']:
                indexes.append(name)
        if not indexes:
            raise sqlite3.DatabaseError('database disk image is malformed')
        upper=self.state['boundary']['tables'][table]['upper']
        def read_keys(keys,depth=0):
            self.checkpoint()
            self.attempts+=1
            if self.attempts>self.limit:
                raise RecoveryUnavailable('Metadata recovery attempt budget exhausted; previous model retained')
            marks=','.join('?' for _ in keys)
            try:
                with closing(self.sql(self.src,'execute',f'SELECT rowid AS __copy_rowid,* FROM {quote(table)} NOT INDEXED WHERE rowid IN ({marks}) ORDER BY rowid',[k[0] for k in keys])) as cursor:
                    rows,size,_=self.read_page(cursor,len(keys))
                if len(rows)!=len(keys):
                    if len(keys)>1:
                        mid=len(keys)//2
                        read_keys(keys[:mid],depth+1);read_keys(keys[mid:],depth+1)
                        return
                    raise RecoveryUnavailable('Metadata witness row missing; previous model retained')
                expected=dict(keys)
                for row in rows:
                    if row['entity_id']!=entity or float(row['event_time'])!=expected[row['__copy_rowid']]:
                        raise RecoveryUnavailable('Metadata witness mismatch; previous model retained')
            except sqlite3.DatabaseError as exc:
                if not is_sqlite_corruption(exc):
                    raise
                if len(keys)>1 and depth<self.depth:
                    mid=len(keys)//2
                    read_keys(keys[:mid],depth+1);read_keys(keys[mid:],depth+1)
                    return
                for _,stamp in keys:
                    # Missing provenance MUST NOT turn our own commands into
                    # independent demonstrations. Exclude the affected time.
                    gap=dict(entity_id=entity,start_ts=stamp,end_ts=stamp,source=table,
                             failure_reason='HISTORY_GAP_SQLITE_CORRUPT',recovery_attempts=self.attempts)
                    gaps=self.state.setdefault('metadata_gaps',[])
                    if not any(x['source']==table and x['entity_id']==entity and x['start_ts']==stamp for x in gaps):
                        gaps.append(gap)
                return
            columns=[c for c in rows[0].keys() if c!='__copy_rowid']
            before=self.dst.total_changes
            self.sql(self.dst,'executemany',f'INSERT OR IGNORE INTO {quote(table)} ({",".join(map(quote,columns))}) VALUES ({",".join("?" for _ in columns)})',
                     [tuple(r[c] for c in columns) for r in rows])
            metric=self.state['tables'][table]
            metric['source_rows_scanned']+=len(rows)
            metric['rows_copied']+=self.dst.total_changes-before
            metric['bytes_copied']+=size
            metric['pending_batches']=metric.get('pending_batches',0)+1
            self.pending_bytes+=size
            self.pending_batches+=1
        last_error=None
        for index in indexes:
            last=None
            try:
                while True:
                    self.checkpoint()
                    after=' AND (event_time,rowid)>(?,?)' if last else ''
                    args=(entity,lo,hi,upper)+tuple(last or ())
                    with closing(self.sql(self.src,'execute',f'SELECT rowid,event_time FROM {quote(table)} INDEXED BY {quote(index)} WHERE entity_id=? AND event_time>=? AND event_time<=? AND rowid<=?'+after+' ORDER BY event_time,rowid LIMIT 128',args)) as cursor:
                        batch=self.sql(cursor,'fetchall')
                    if not batch:
                        return
                    next_cursor=(float(batch[-1][1]),int(batch[-1][0]))
                    advancing_cursor(last,next_cursor,table=table,strategy='metadata_witness',rows=len(batch),job_id=self.destination.stem,query_key=entity)
                    read_keys([(int(r[0]),float(r[1])) for r in batch])
                    metric=self.state['tables'][table]
                    metric.update(query_key=table+':'+entity+':witness:'+index,
                                  previous_cursor=last,current_cursor=next_cursor,
                                  last_cursor_advance_at=time.time())
                    last=next_cursor
                    self.save(force=False)
                    self.emit(table,'metadata_witness')
            except sqlite3.DatabaseError as exc:
                if not is_sqlite_corruption(exc):
                    raise
                last_error=exc
        raise last_error

    def recover(self, failed, report):
        """Prefer selected entity keys from a healthy covering index, never all keys."""
        self.in_recovery = True
        self.recovery_start_bytes = scratch_bytes(self.working)
        self.recovery_started = time.process_time()
        cache_key=hashlib.sha256((str(self.source)+'|'+','.join(failed)).encode()).hexdigest()[:20]
        cache_path=self.destination.parent/('.quarantine-'+cache_key+'.json')
        try:
            cache=json.loads(cache_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            cache={}
        known={int(k):v for k,v in cache.get('missing',{}).items()
               if self.start<=v['start_ts']<=self.end} if cache.get('signature')==self.source_signature else {}
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
                        params=(entity,self.start,self.end,self.state['boundary']['tables']['entity_history']['upper'])+tuple((last[0],last[1]) if last else ())
                        with closing(self.sql(self.src,'execute',f'SELECT id,entity_id,ts FROM entity_history INDEXED BY {quote(index)} WHERE entity_id=? AND ts>=? AND ts<=? AND rowid<=?'+predicate+' ORDER BY ts,id LIMIT 128',params)) as cursor:
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
                    with closing(self.sql(self.src,'execute',f'SELECT * FROM entity_history NOT INDEXED WHERE id BETWEEN ? AND ? AND entity_id IN ({marks}) AND ts>=? AND ts<=? ORDER BY id',(lo,hi,*failed,self.start,self.end))) as cursor:
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
        self.checkpoint()
        self.recovery_cpu=self.active_recovery_cpu()
        self.recovery_started=None
        self.in_recovery=False


def prepare_generation(source,destination,entities,start,end,*,options=None,cancelled=lambda:False,
                       progress=lambda **kw:None,http_gaps=(),agent_id=None,rebuild=False):
    # Retry only native lock contention. Every attempt closes its connections,
    # and the next attempt validates the committed source fence/copy checkpoint.
    # Do not keep a reader or writer open while sleeping.
    entities=tuple(entities)
    deadline=None
    retries=0
    busy_wait_seconds=0.
    while True:
        preparation = Preparation(source,destination,entities,start,end,options,cancelled,progress,agent_id,rebuild)
        try:
            report=preparation.run(http_gaps)
            report['sqlite_busy_retries']=retries
            report['sqlite_busy_wait_seconds']=round(busy_wait_seconds,3)
            return report
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL:
                raise generation_pressure('sqlite_full', preparation.working,
                                          preparation.disk, preparation.reserve) from exc
            if not is_sqlite_busy(exc):
                raise
            retries+=1
            if deadline is None:
                deadline=time.monotonic()+90.
            if retries>=10 or time.monotonic()>=deadline:
                error=StoragePressure('SQLite busy retry budget exhausted during '+preparation.failure_stage+
                                      '; previous model and committed preparation retained')
                error.reason='sqlite_busy_exhausted'
                raise error from exc
            delay=min(4.,.25*2**min(retries-1,4),max(0.,deadline-time.monotonic()))
            details=dict(stage='Waiting for SQLite',operation=preparation.failure_stage,
                         sqlite_busy_retries=retries,sqlite_errorcode=getattr(exc,'sqlite_errorcode',None),
                         sqlite_errorname=getattr(exc,'sqlite_errorname',None),
                         retry_delay_seconds=delay,copy_strategy='sqlite_busy_wait',
                         generation_scratch_bytes=scratch_bytes(preparation.working))
            progress(message='Waiting for SQLite writer; training will retry',
                     phase_detail=preparation.failure_stage,progress=None,work_done=0,preparation=details)
            print('[training] SQLite busy: '+json.dumps(details),flush=True)
            began=time.monotonic()
            until=began+delay
            while time.monotonic()<until:
                if cancelled():
                    raise InterruptedError('SQLite preparation retry cancelled')
                time.sleep(min(.1,max(0.,until-time.monotonic())))
            busy_wait_seconds+=time.monotonic()-began
            if cancelled():
                raise InterruptedError('SQLite preparation retry cancelled')
