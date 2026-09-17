import tempfile
import unittest
from pathlib import Path

from manual_feedback_contract import ManualFeedbackJournal
from storage import Store


class ManualFeedbackMigrationTests(unittest.TestCase):
    def test_migration_is_additive_and_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "feedback-migration.db"
            store = Store(path)
            with store.lock, store.conn() as c:
                c.execute("CREATE TABLE legacy_feedback_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
                c.execute("INSERT INTO legacy_feedback_sentinel VALUES(1,'keep-me')")
                before = {
                    row["name"]
                    for row in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()
                }

            ManualFeedbackJournal(store)

            with store.conn() as c:
                after = {
                    row["name"]
                    for row in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()
                }
                sentinel = c.execute(
                    "SELECT id,value FROM legacy_feedback_sentinel WHERE id=1"
                ).fetchone()

            self.assertEqual((sentinel["id"], sentinel["value"]), (1, "keep-me"))
            self.assertTrue(before.issubset(after))
            self.assertEqual(
                after - before,
                {"manual_feedback_journal", "manual_feedback_effects"},
            )


if __name__ == "__main__":
    unittest.main()
