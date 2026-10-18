"""Real Store reopen + HistoryManager construction on a populated indexed archive."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def one(source, rows):
    with tempfile.TemporaryDirectory() as directory:
        os.environ['ADAPTIVE_AI_DATA'] = directory
        sys.path[:0] = [str(source/'adaptive_ai/src'), str(source/'tests')]
        import storage
        import history
        import sqlite3
        import support
        store = storage.STORE
        agent = store.create_agent(support.agent(id='legacy'))
        with store.conn() as c:
            c.execute("UPDATE agents SET training_state='qualified' WHERE id=?", (agent['id'],))
            c.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<?) '
                      'INSERT INTO entity_history(entity_id,ts,state,attributes_json,source,mutation_revision) '
                      "SELECT 'sensor.'||(x%778),1792200000.+x,CAST(x%101 AS TEXT),'{}','fixture',1 FROM n", (rows,))
        scans = []
        real = sqlite3.connect
        def connect(*args, **kwargs):
            connection = real(*args, **kwargs)
            def trace(sql):
                if 'FROM entity_history' in sql and ('COUNT(' in sql or 'MIN(ts), MAX(ts)' in sql):
                    scans.append(sql)
            connection.set_trace_callback(trace)
            return connection
        started = time.perf_counter()
        with patch.object(sqlite3,'connect',connect):
            reopened = storage.Store(store.path)
            storage_ms=(time.perf_counter()-started)*1000
            with patch.object(history,'STORE',reopened):
                manager=history.HistoryManager(SimpleNamespace())
        total_ms=(time.perf_counter()-started)*1000
        record=reopened.get_agent_config(agent['id'])
        assert record['training_cursor_ts']==1792200000.+rows
        return dict(rows=rows,store_reopen_ms=storage_ms,store_and_history_ms=total_ms,
                    full_archive_aggregate_queries=scans, seeded_cursor=record['training_cursor_ts'],
                    overview_rows_at_ready=manager.archive_cache['n'],
                    overview_stale=manager.archive_cache.get('stale',False))


def paired(baseline,rows):
    series=[]
    for i in range(3):
        pair={}
        for name in (('before','after') if i%2==0 else ('after','before')):
            source=baseline if name=='before' else ROOT
            result=subprocess.check_output([sys.executable,str(Path(__file__).resolve()),'--one',
                '--source-root',str(source),'--rows',str(rows)],text=True)
            pair[name]=json.loads(result)
        assert pair['before']['seeded_cursor']==pair['after']['seeded_cursor']
        pair['speedup']=pair['before']['store_and_history_ms']/pair['after']['store_and_history_ms']
        series.append(pair)
    return dict(series=series, median_speedup=statistics.median(p['speedup'] for p in series),
                scope='Indexed host archive, warm OS cache, Store reopen + HistoryManager only; not full deployed startup. '
                      'New overview is deliberately pending at readiness, training bounds/cursor remain exact.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--one',action='store_true')
    parser.add_argument('--source-root',type=Path,default=ROOT)
    parser.add_argument('--baseline-root',type=Path)
    parser.add_argument('--rows',type=int,default=200000)
    args=parser.parse_args()
    print(json.dumps(one(args.source_root,args.rows) if args.one else paired(args.baseline_root,args.rows)))
