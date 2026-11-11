import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import support
from archive_reads import archive_query


SQL = "SELECT id,entity_id,ts FROM entity_history INDEXED BY test_history_index ORDER BY ts,id"


class FailingCursor(sqlite3.Cursor):
    def fetchmany(self, size=256):
        self.calls = getattr(self, "calls", 0) + 1
        if self.calls == 2:
            if self.connection.before_error:
                self.connection.before_error()
            error = sqlite3.DatabaseError("database disk image is malformed")
            error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
            raise error
        return super().fetchmany(size)


class FailingConnection(sqlite3.Connection):
    before_error = None

    def execute(self, sql, parameters=()):
        if sql == SQL:
            return self.cursor(factory=FailingCursor).execute(sql, parameters)
        return super().execute(sql, parameters)


class ArchiveReadCorruptionTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.path = Path(root) / "history.db"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(
                "CREATE TABLE entity_history(id INTEGER PRIMARY KEY,entity_id TEXT,ts REAL);"
                "CREATE INDEX test_history_index ON entity_history(ts,id);"
            )
            conn.executemany(
                "INSERT INTO entity_history VALUES(?,?,?)",
                [(i, "sensor.test", float(i // 2)) for i in range(1, 1001)],
            )
            self.expected = conn.execute(SQL).fetchall()
            self.page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            self.index_page = conn.execute(
                "SELECT rootpage FROM sqlite_master WHERE name='test_history_index'"
            ).fetchone()[0]
            self.table_page = conn.execute(
                "SELECT rootpage FROM sqlite_master WHERE name='entity_history'"
            ).fetchone()[0]

    def corrupt_page(self, page):
        with self.path.open("r+b") as stream:
            stream.seek((page - 1) * self.page_size)
            stream.write(b"\xff")

    def test_real_corrupt_index_returns_every_table_row_in_order(self):
        self.corrupt_page(self.index_page)
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute(SQL).fetchall()
        cursor = archive_query(conn, SQL)
        self.addCleanup(cursor.close)
        self.assertEqual(cursor.fetchall(), self.expected)
        self.assertTrue(cursor.retried)
        self.assertEqual(conn.total_changes, 0)

    def test_real_corrupt_table_still_reports_error(self):
        self.corrupt_page(self.table_page)
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.DatabaseError):
            archive_query(conn, SQL).fetchall()

    def test_corruption_after_delivered_batch_neither_drops_nor_repeats_rows(self):
        conn = sqlite3.connect(self.path, factory=FailingConnection)
        self.addCleanup(conn.close)
        cursor = archive_query(conn, SQL)
        self.addCleanup(cursor.close)
        actual = []
        while batch := cursor.fetchmany(100):
            actual.extend(batch)
        self.assertEqual(actual, self.expected)
        self.assertTrue(cursor.retried)

    def test_archive_mutation_prevents_resuming_with_a_changed_row_set(self):
        conn = sqlite3.connect(self.path, factory=FailingConnection)
        self.addCleanup(conn.close)
        conn.before_error = lambda: conn.execute(
            "DELETE FROM entity_history WHERE id=1"
        )
        cursor = archive_query(conn, SQL)
        self.addCleanup(cursor.close)
        self.assertEqual(cursor.fetchmany(100), self.expected[:100])
        with self.assertRaises(sqlite3.DatabaseError):
            cursor.fetchmany(100)

    def test_external_commit_prevents_resuming_across_archive_revisions(self):
        conn = sqlite3.connect(self.path, factory=FailingConnection)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)

        def change_archive():
            writer.execute("DELETE FROM entity_history WHERE id=1")
            writer.commit()

        conn.before_error = change_archive
        cursor = archive_query(conn, SQL)
        self.addCleanup(cursor.close)
        self.assertEqual(cursor.fetchmany(100), self.expected[:100])
        with self.assertRaises(sqlite3.DatabaseError):
            cursor.fetchmany(100)

    def test_healthy_query_and_unrelated_sql_error_keep_their_behavior(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(archive_query(conn, SQL).fetchall(), self.expected)
        with self.assertRaises(sqlite3.OperationalError):
            archive_query(conn, "SELECT absent FROM entity_history").fetchall()
