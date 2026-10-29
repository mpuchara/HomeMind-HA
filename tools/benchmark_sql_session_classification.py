"""Before-change reference for the measured pure SQLite session classifier."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time


def reference(sql):
    clean = re.sub(r'\A\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*', '', str(sql), flags=re.S).strip()
    pragma = re.match(r'PRAGMA\s+(?:\w+\.)?(\w+)\s*(.*)', clean, re.I | re.S)
    return bool(re.match(r'(ATTACH|DETACH|VACUUM|SCRIPT)\b', clean, re.I)
                or re.search(r'\bTEMP(?:ORARY)?\b', clean, re.I)
                or (pragma and pragma[2].strip(' ;') and pragma[1].lower()
                    not in ('busy_timeout', 'wal_autocheckpoint')))


STATEMENTS = ['SELECT * FROM agents WHERE id=?', 'PRAGMA busy_timeout',
              'PRAGMA wal_autocheckpoint=0', 'SELECT * FROM agent_candidate_generations WHERE agent_id=?',
              'INSERT INTO feature_windows(window_id) VALUES(?)', 'BEGIN IMMEDIATE',
              'PRAGMA synchronous=NORMAL', 'CREATE TEMP TABLE demo(x)', 'ATTACH DATABASE ? AS x',
              '-- header\n/* comment */ SELECT 1', 'PRAGMA main.table_info(agents)']


def run(source, passes):
    sys.path.insert(0, str(source/'adaptive_ai/src'))
    import runtime_sqlite_cache as module
    candidate = getattr(module, 'session_sql_taints', reference)
    rows = []
    for repeat in range(5):
        pair = {}
        for label in (('before', 'after') if repeat % 2 == 0 else ('after', 'before')):
            fn = reference if label == 'before' else candidate
            start, cpu = time.perf_counter_ns(), time.thread_time_ns()
            result = [fn(STATEMENTS[i % len(STATEMENTS)]) for i in range(passes)]
            pair[label] = dict(wall_ms=(time.perf_counter_ns()-start)/1e6,
                               thread_cpu_ms=(time.thread_time_ns()-cpu)/1e6,
                               sha256=hashlib.sha256(bytes(result)).hexdigest())
        assert pair['before']['sha256'] == pair['after']['sha256']
        rows.append(pair)
    return dict(passes=passes, series=rows, source_has_optimization=hasattr(module, 'session_sql_taints'))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--passes', type=int, default=100000)
    a = p.parse_args()
    print(json.dumps(run(a.source_root, a.passes)))
