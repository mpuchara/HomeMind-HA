"""Compare statement classification against the frozen storage-budget rules."""
import argparse
import json
import re
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source/'adaptive_ai/src'))
    from storage_budget import _READONLY_PAGE_PRAGMAS, _clean_sql, _statement_flags

    def retained(sql):
        text = _clean_sql(sql)
        write = bool(text.strip()) and text.split(None, 1)[0].upper() not in (
            'SELECT', 'EXPLAIN', 'PRAGMA', 'BEGIN', 'ROLLBACK', 'SAVEPOINT')
        pragma = re.match(r'PRAGMA\s+(?:\w+\.)?(\w+)', _clean_sql(sql), re.I)
        return write, bool(write or pragma and pragma[1].lower() not in _READONLY_PAGE_PRAGMAS)

    workload = (
        'SELECT revision FROM universal_arbiter_revision WHERE id=1',
        'BEGIN IMMEDIATE', 'PRAGMA main.page_count', 'PRAGMA schema_version',
        'INSERT OR IGNORE INTO universal_collection_decisions VALUES (?,?,?,?,?,?)',
        'PRAGMA wal_checkpoint(PASSIVE)', 'PRAGMA foreign_keys=ON', 'BEGIN',
        'ROLLBACK TO predecision_evaluation', 'SAVEPOINT prepare', 'EXPLAIN SELECT 1',
        'WITH x AS (SELECT 1) SELECT * FROM x', 'COMMIT', 'CREATE TABLE t(a)',
        'DELETE FROM t', 'UPDATE t SET a=1', 'RELEASE prepare', 'pragma', '', '   ',
        'PRAGMA "page_count"', 'PRAGMAx schema_version', ';SELECT 1',
        '\ufeffSELECT 1', 'PRAGMA\u2003page_count')
    cases = [prefix+sql for prefix in ('', ' \t', '-- header\n', '/* x\ny */ ', '--x\n/* y */')
             for sql in workload]
    cases += [sql.swapcase() for sql in cases]
    for sql in cases:
        if retained(sql) != _statement_flags(sql):
            raise AssertionError(sql)
    rows = []
    methods = (('retained', retained), ('candidate', _statement_flags))
    for pair in range(6):
        for name, classify in (methods if pair%2 == 0 else reversed(methods)):
            started, cpu = time.perf_counter_ns(), time.thread_time_ns()
            for _ in range(2000):
                for sql in workload:
                    classify(sql)
            rows.append(dict(pair=pair, method=name, calls=2000*len(workload),
                             cpu_ns=time.thread_time_ns()-cpu, wall_ns=time.perf_counter_ns()-started))
    print(json.dumps(dict(parity_cases=len(cases), timings=rows), indent=2))


if __name__ == '__main__':
    main()
