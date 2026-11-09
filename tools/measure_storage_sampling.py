"""Compare filesystem sampling on frozen runtime trees with identical inputs."""
import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source/'adaptive_ai/src'))
    from storage_budget import StorageBudget, StoragePressure

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'test.db'
        budget = StorageBudget(path, {'storage_budget_mode': 'manual'})
        wal = path.with_name('test.db-wal')
        rows = []
        for case, db_size, wal_size, free, used in (
                ('missing', None, None, budget.reserve, 0),
                ('normal', 100, 50, budget.reserve, 0),
                ('soft_disk', 200, 100, budget.hard_reserve, 0),
                ('hard_disk', 300, 150, budget.hard_reserve-1, 0),
                ('soft_wal', 300, budget.wal_limit//2, budget.reserve, 0),
                ('hard_wal', 300, budget.wal_limit, budget.reserve, 0),
                ('soft_pages', 300, 0, budget.reserve, budget.db_limit),
                ('stat_error', 300, 0, budget.reserve, 0),
                ('free_error', 300, 0, budget.reserve, 0)):
            calls = []
            sizes = {path: db_size, wal: wal_size}

            def stat(target):
                if target not in sizes:
                    raise AssertionError(target)
                calls.append(target.name)
                if case == 'stat_error' and target == wal:
                    raise OSError('injected stat failure')
                if sizes[target] is None:
                    raise FileNotFoundError(target)
                return type('Stat', (), {'st_size': sizes[target]})()

            def disk_usage(target):
                assert target == path.parent
                calls.append('free')
                if case == 'free_error':
                    raise OSError('injected disk usage failure')
                return type('Usage', (), {'free': free})()

            budget.stats['database_used_bytes'] = used
            with patch.object(type(path), 'stat', stat), patch.object(shutil, 'disk_usage', disk_usage):
                result = budget.sample()
                try:
                    check = budget.check()
                except StoragePressure as exc:
                    check = str(exc)
            rows.append(dict(case=case, sample=result, check=check, reads=calls))
        timings = []
        path.write_bytes(b'db')
        wal.write_bytes(b'wal')
        budget.stats['database_used_bytes'] = 0
        for index in range(6):
            started, cpu = time.perf_counter_ns(), time.thread_time_ns()
            for _ in range(10000):
                budget.sample()
            timings.append(dict(round=index, iterations=10000,
                                cpu_ns=time.thread_time_ns()-cpu, wall_ns=time.perf_counter_ns()-started))
    print(json.dumps(dict(safety=rows, timings=timings), indent=2))


if __name__ == '__main__':
    main()
