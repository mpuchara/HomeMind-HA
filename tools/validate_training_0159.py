"""Pinned real-SQLite control and rich isolated-worker acceptance, host only."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
REFERENCE='bc60087a2085165ecd6d273a31c1ee25f907d8b6'
sys.path.insert(0,str(ROOT/'tests'))
import support
import training_generation
from test_release_080_training_process import ActualWorkerSmoke


def io_bytes():
    try:
        return int(dict(line.split(':',1) for line in Path('/proc/self/io').read_text().splitlines())['write_bytes'])
    except (OSError,KeyError):
        return None


def copy_bench(module, window, count):
    with tempfile.TemporaryDirectory(prefix='hm-pagination-') as root:
        root=Path(root)
        source=root/'source.db'
        with sqlite3.connect(source) as conn:
            conn.execute('CREATE TABLE adaptive_presence_checkpoints(ts REAL PRIMARY KEY,model_json TEXT)')
            conn.executemany('INSERT INTO adaptive_presence_checkpoints VALUES(?,?)',((2000000.+i,'x'*900) for i in range(count)))
        conn.close()
        dst=sqlite3.connect(root/'copy.db')
        src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
        src.row_factory=sqlite3.Row
        try:
            dst.execute('CREATE TABLE adaptive_presence_checkpoints(ts REAL PRIMARY KEY,model_json TEXT)')
            dst.execute('CREATE TABLE __training_copy_state(id INTEGER PRIMARY KEY,payload TEXT)')
            dst.commit()
            p=module.Preparation(source,root/'result.db',[],2000000.,2100000.,{},lambda:False,lambda **kw:None,'agent')
            p.src,p.dst,p.schema=src,dst,[]
            p.state['boundary']={'tables':{'adaptive_presence_checkpoints':{'upper':count}}}
            ticks=[]
            def bounded():
                ticks.append(True)
                if len(ticks)>100:
                    raise TimeoutError('bounded regression control: repeated keyset page')
            p.checkpoint=bounded
            commits=[]
            dst.set_trace_callback(lambda sql: commits.append(sql) if sql=='COMMIT' else None)
            start,cpu,io=time.perf_counter(),time.process_time(),io_bytes()
            outcome='completed'
            predicate,params,strategy=p.table_plan({'name':'adaptive_presence_checkpoints'}) if window else ('ts>=?',(2000000.,),'normal_keyset_cost')
            try:
                p.copy('adaptive_presence_checkpoints',predicate,params,strategy=strategy)
            except TimeoutError:
                outcome='bounded_infinite_loop'
            after=io_bytes()
            result=dict(outcome=outcome,wall_seconds=time.perf_counter()-start,cpu_seconds=time.process_time()-cpu,
                process_write_bytes=None if io is None else after-io,commits=len(commits),
                metrics=p.state['tables']['adaptive_presence_checkpoints'],queries=p.state['queries'])
        finally:
            src.close();dst.close()
        return result


def main():
    try:
        raw=subprocess.check_output(['git','show',REFERENCE+':adaptive_ai/src/training_generation.py'],cwd=ROOT,stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        subprocess.run(['git','fetch','--depth=1','origin',REFERENCE],cwd=ROOT,check=True)
        raw=subprocess.check_output(['git','show',REFERENCE+':adaptive_ai/src/training_generation.py'],cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix='hm-reference-0158-') as directory:
        path=Path(directory)/'training_generation_0158.py'
        path.write_bytes(raw)
        spec=importlib.util.spec_from_file_location('training_generation_0158',path)
        reference=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
        result=dict(environment='Host SQLite on local filesystem; NOT Pi/SD-card acceptance',reference_commit=REFERENCE)
        for name,window,count in (('five_checkpoint_regression',True,5),('normal_commit_cost',False,8192)):
            result[name]={'reference':copy_bench(reference,window,count),'candidate':copy_bench(training_generation,window,count)}
        assert result['five_checkpoint_regression']['reference']['outcome']=='bounded_infinite_loop'
        assert result['five_checkpoint_regression']['candidate']['metrics']['source_rows_scanned']==5
        normal=result['normal_commit_cost']
        assert normal['reference']['metrics']['rows_copied']==normal['candidate']['metrics']['rows_copied']==8192
        assert normal['candidate']['commits']<normal['reference']['commits']
    out=ROOT/'docs/benchmarks/training-0159-comparison.json'
    out.write_text(json.dumps(result,indent=2),encoding='utf-8')
    rich=ActualWorkerSmoke().run_worker_fixture(samples=96,qualify=True,corruption_recovery=True,
        two_agents=True,concurrent_writes=True,rich_checkpoints=True,inherit_storage_budget=True)
    assert rich['agent_mode']=='shadow' and rich['agent_progress']==1.
    assert rich['fixture_agents']>=26 and rich['fixture_state_entities']>=245
    assert rich['sequence_chunks_completed']==2
    assert rich['worker_phases']['feature_snapshot_session_reused']
    assert rich['worker_phases']['continuation_cached_rows']>0
    for table in ('home_checkpoints','adaptive_presence_checkpoints'):
        assert rich['history_recovery']['copy_tables'][table]['rows_copied']==5
        assert rich['history_recovery']['copy_tables'][table]['source_rows_scanned']==5
    (ROOT/'docs/benchmarks/training-0159-rich-worker.json').write_text(json.dumps(rich,indent=2),encoding='utf-8')
    from test_recorder_costs_0159 import RecorderCostsTests
    case=RecorderCostsTests('test_real_http_sqlite_import_and_second_agent_reuses_coverage')
    case.debug()
    recorder=dict(environment='Host loopback HTTP + real SQLite, 200 rows; NOT HA/Pi Recorder acceptance',
        http_requests_across_two_agents_and_restart=1,metrics=RecorderCostsTests.last_metrics,
        installation_11066_seconds_dominant_cost='UNKNOWN; collect recorder_costs on installation')
    (ROOT/'docs/benchmarks/training-0159-recorder-costs.json').write_text(json.dumps(recorder,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
    print('Rich isolated worker: Shadow, progress 100%, checkpoints exactly 5 each')


if __name__=='__main__':
    main()
