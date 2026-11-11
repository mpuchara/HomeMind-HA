"""Read the same archive rows through a table scan if an index is corrupt."""
import re
import sqlite3

from sqlite_background import is_sqlite_corruption


_TABLE = re.compile(
    r"\bFROM\s+entity_history\b(?:\s+INDEXED\s+BY\s+\w+)?",
    re.IGNORECASE,
)
_fallback_reported = False


class ArchiveReadCursor:
    def __init__(self, connection, sql, parameters, *, snapshot=False):
        self.connection = connection
        self.sql = sql
        self.parameters = tuple(parameters)
        self.delivered = 0
        self.retried = False
        self.cursor = None
        self.revision = None
        self.owns_snapshot = snapshot and not connection.in_transaction
        try:
            if self.owns_snapshot:
                connection.execute("BEGIN")
                connection.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()
            self.revision = self._revision()
            self.cursor = connection.execute(sql, self.parameters)
        except sqlite3.DatabaseError as exc:
            if self.revision is None:
                self.close()
                raise
            try:
                self._retry(exc)
            except BaseException:
                self.close()
                raise

    def _revision(self):
        return (
            self.connection.execute("PRAGMA data_version").fetchone()[0],
            self.connection.total_changes,
        )

    def _retry(self, error):
        global _fallback_reported
        if self.retried or not is_sqlite_corruption(error):
            raise error
        if self.cursor is not None:
            self.cursor.close()
        # A resumed query must see the same archive as the rows already returned.
        # Otherwise its offset could omit a newly inserted row or repeat a row.
        if self._revision() != self.revision:
            raise error
        sql = _TABLE.sub("FROM entity_history NOT INDEXED", self.sql)
        if sql == self.sql or "NOT INDEXED NOT INDEXED" in sql.upper():
            raise error
        if self.delivered and not re.search(r"\bORDER\s+BY\b", self.sql, re.IGNORECASE):
            raise error
        self.retried = True
        if not _fallback_reported:
            print("[history] SQLite archive read failed; retrying the same query without indexes", flush=True)
            _fallback_reported = True
        self.cursor = self.connection.execute(sql, self.parameters)
        if self._revision() != self.revision:
            self.cursor.close()
            raise error
        remaining = self.delivered
        while remaining:
            batch = self.cursor.fetchmany(min(256, remaining))
            if not batch:
                raise error
            remaining -= len(batch)

    def _fetch(self, method, *args):
        try:
            try:
                result = getattr(self.cursor, method)(*args)
            except sqlite3.DatabaseError as exc:
                self._retry(exc)
                result = getattr(self.cursor, method)(*args)
        except BaseException:
            self.close()
            raise
        self.delivered += (int(result is not None) if method == "fetchone" else len(result))
        if self.owns_snapshot and (method == "fetchall" or not result):
            self.close()
        return result

    def fetchone(self):
        return self._fetch("fetchone")

    def fetchmany(self, size=256):
        return self._fetch("fetchmany", size)

    def fetchall(self):
        return self._fetch("fetchall")

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self):
        if self.cursor is not None:
            self.cursor.close()
        if self.owns_snapshot:
            self.connection.rollback()
            self.owns_snapshot = False


def archive_query(connection, sql, parameters=(), *, snapshot=False):
    if not sql.lstrip().upper().startswith("SELECT") or not _TABLE.search(sql):
        return connection.execute(sql, parameters)
    return ArchiveReadCursor(connection, sql, parameters, snapshot=snapshot)
