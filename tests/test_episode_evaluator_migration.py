import unittest

from support import *
from episode_evaluator import EpisodeEvaluator
from storage import Store


class EpisodeEvaluatorMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "migration.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_is_additive_and_preserves_existing_data(self):
        with self.store.lock, self.store.conn() as c:
            c.execute("CREATE TABLE audit_episode_sentinel(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("INSERT INTO audit_episode_sentinel(id,payload) VALUES(1,'keep-me')")
            before = {
                row[0] for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

        EpisodeEvaluator(self.store)

        with self.store.conn() as c:
            after = {
                row[0] for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            sentinel = c.execute(
                "SELECT payload FROM audit_episode_sentinel WHERE id=1"
            ).fetchone()[0]

        self.assertTrue(before.issubset(after))
        self.assertEqual(
            after - before,
            {"episode_evaluator_episodes", "episode_evaluator_policy_results"},
        )
        self.assertEqual(sentinel, "keep-me")


if __name__ == "__main__":
    unittest.main()
