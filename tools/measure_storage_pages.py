"""Compare fresh SQLite page reads without instrumenting the timed loops."""
import json
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path


COMBINED = ('SELECT page_count, freelist_count, page_size FROM '
            'pragma_page_count(), pragma_freelist_count(), pragma_page_size()')


def separate(connection):
    execute = sqlite3.Connection.execute
    return (execute(connection, 'PRAGMA page_count').fetchone()[0],
            execute(connection, 'PRAGMA freelist_count').fetchone()[0],
            execute(connection, 'PRAGMA page_size').fetchone()[0])


def combined(connection):
    return sqlite3.Connection.execute(connection, COMBINED).fetchone()


def run(connection, count=20000):
    methods = {'separate': separate, 'combined': combined}
    reference = separate(connection)
    rows = []
    for pair in range(6):
        for name in (methods if pair % 2 == 0 else reversed(methods)):
            function = methods[name]
            wall, cpu = time.perf_counter_ns(), time.thread_time_ns()
            for _ in range(count):
                value = function(connection)
            result = dict(pair=pair, method=name, wall_ns=time.perf_counter_ns()-wall,
                          cpu_ns=time.thread_time_ns()-cpu, iterations=count,
                          python_execute_calls=count*(3 if name == 'separate' else 1))
            assert value == reference
            rows.append(result)
    steps = {}
    for name, function in methods.items():
        calls = []
        connection.set_progress_handler(lambda: calls.append(None) or 0, 1)
        try:
            assert function(connection) == reference
        finally:
            connection.set_progress_handler(None, 0)
        steps[name] = len(calls)
    return dict(page_values=reference, timings=rows, progress_callbacks=steps)


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'pages.db'
        with closing(sqlite3.connect(path)) as connection, closing(sqlite3.connect(path)) as external:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE payload(body BLOB)')
            external.executemany('INSERT INTO payload VALUES(zeroblob(8192))', [()]*128)
            external.commit()
            assert separate(connection) == combined(connection)
            external.execute('DELETE FROM payload')
            external.commit()
            assert separate(connection) == combined(connection)
            results = {'autocommit': run(connection)}
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT INTO payload VALUES(zeroblob(65536))')
            results['write_transaction'] = run(connection)
            connection.rollback()
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            assert separate(connection) == combined(connection)
            # A caller may authorize native page PRAGMAs while denying SELECT.
            connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY
                                      if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
            separate(connection)
            try:
                combined(connection)
            except sqlite3.DatabaseError as exc:
                authorizer = dict(separate='allowed', combined=str(exc),
                                  error_code=exc.sqlite_errorcode)
            else:
                raise AssertionError('expected SELECT denial')
    print(json.dumps(dict(sqlite=sqlite3.sqlite_version, results=results,
                          authorizer=authorizer), indent=2))


if __name__ == '__main__':
    main()
