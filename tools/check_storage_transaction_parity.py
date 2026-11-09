"""Replay native commit, failure, external-writer and reopen cases on a frozen tree."""
import argparse
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def replay(path, budget, connection_type):
    def connect():
        return sqlite3.connect(path, timeout=0, factory=connection_type)

    def failure(operation, code):
        try:
            operation()
        except sqlite3.DatabaseError as error:
            if error.sqlite_errorcode != code:
                raise
            return dict(code=code, message=str(error))
        raise AssertionError(f'expected SQLite error {code}')

    with closing(connect()) as connection, closing(sqlite3.connect(path, timeout=0)) as external:
        connection.execute('PRAGMA journal_mode=WAL').fetchone()
        connection.execute('PRAGMA foreign_keys=ON')
        budget.install(connection)
        connection.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY, payload BLOB)')
        connection.execute('CREATE TABLE child(parent REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)')
        with connection:
            connection.execute('INSERT INTO parent VALUES(1, zeroblob(1))')
            connection.execute('INSERT INTO child VALUES(1)')

        def invalid_commit():
            with connection:
                connection.execute('INSERT INTO child VALUES(999)')

        commit_error = failure(invalid_commit, sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY)
        assert not connection.in_transaction
        assert connection.execute('SELECT parent FROM child').fetchall() == [(1,)]
        connection.execute('INSERT INTO parent VALUES(2, zeroblob(4096))')
        connection.rollback()
        assert connection.execute('SELECT id FROM parent').fetchall() == [(1,)]
        budget.observe_pages(connection)
        original_used = budget.snapshot()['database_used_bytes']
        external.execute('BEGIN IMMEDIATE')
        external.execute('INSERT INTO parent VALUES(3, zeroblob(65536))')
        busy = failure(lambda: connection.execute('BEGIN IMMEDIATE'), sqlite3.SQLITE_BUSY)
        external.commit()
        budget.observe_pages(connection)
        grown_used = budget.snapshot()['database_used_bytes']
        assert grown_used > original_used
        checkpoint = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        assert checkpoint == (0, 0, 0)
    with closing(connect()) as reopened:
        budget.install(reopened)
        persisted = reopened.execute('SELECT id, length(payload) FROM parent ORDER BY id').fetchall()
        assert persisted == [(1, 1), (3, 65536)]
        assert reopened.execute('PRAGMA foreign_key_check').fetchall() == []
        return dict(commit_error=commit_error, busy=busy, rollback_preserved=True,
                    checkpoint=checkpoint, original_used=original_used, grown_used=grown_used,
                    reopened_used=budget.snapshot()['database_used_bytes'], persisted=persisted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source/'adaptive_ai/src'))
    from storage_budget import BudgetConnection, StorageBudget

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'transactions.db'
        budget = StorageBudget(path, {'storage_budget_mode': 'manual'})
        result = replay(path, budget, BudgetConnection)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
