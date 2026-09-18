import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import storage
from support import ROOT


class HistoricalExperienceBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "batch.db")

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, start=1, count=4):
        return [
            {
                "agent_id": "a",
                "target_history_id": start + i,
                "action_index": i % 2,
                "action_value": float(i % 2),
                "reward": 1.0,
                "dwell_seconds": 10.0 + i,
                "features": {0: 1.0, 3: float(i)},
                "user_id": None,
            }
            for i in range(count)
        ]

    def test_batch_persists_many_experiences_in_one_connection(self):
        original = self.store.conn
        calls = {"n": 0}

        @contextmanager
        def counted():
            calls["n"] += 1
            with original() as c:
                yield c

        self.store.conn = counted
        inserted = self.store.add_historical_experiences_batch(self.rows(count=64))
        self.assertEqual(inserted, 64)
        self.assertEqual(calls["n"], 1)

    def test_batch_keeps_unique_agent_target_history_contract(self):
        rows = self.rows(count=4)
        self.assertEqual(self.store.add_historical_experiences_batch(rows), 4)
        self.assertEqual(self.store.add_historical_experiences_batch(rows), 0)
        self.assertEqual(self.store.historical_experience_target_ids("a"), {1, 2, 3, 4})

    def test_single_insert_compatibility_path_uses_same_payload_contract(self):
        self.assertTrue(self.store.add_historical_experience(
            "a", 7, 1, 1.0, .8, 12.0, {1: .25}, "user"
        ))
        self.assertFalse(self.store.add_historical_experience(
            "a", 7, 1, 1.0, .8, 12.0, {1: .25}, "user"
        ))
        rows = self.store.list_historical_experiences("a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_history_id"], 7)
        self.assertEqual(rows[0]["features"], {1: .25})


class Release024SourceContractTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / "adaptive_ai" / "src" / name).read_text(encoding="utf-8")

    def test_replay_uses_bounded_batch_writer_not_one_transaction_per_dwell(self):
        source = self.source("history.py")
        start = source.index("    def _train_from_archive")
        end = source.index("    def request_agent_rebuild", start)
        body = source[start:end]
        self.assertIn("historical_experience_target_ids", body)
        self.assertIn("experience_batch_rows", body)
        self.assertIn("add_historical_experiences_batch", body)
        self.assertIn('TRAINING_BUDGET.checkpoint("historical_experience_batch_flush", force=True)', body)
        self.assertNotIn("STORE.add_historical_experience(", body)

    def test_feature_screening_is_schema_aware_and_does_not_reopen_archive_when_skipped(self):
        source = self.source("history.py")
        self.assertIn("saved_models =", source)
        self.assertIn("screen_agents = [", source)
        self.assertIn('"*" in set(a.get("input_entities") or ["*"])', source)
        self.assertIn("if screening_required else ()", source)
        self.assertIn("persisted feature schema reused", source)
        self.assertIn('TRAINING_BUDGET.checkpoint("context_screen_target_edge")', source)

    def test_new_batch_size_is_bounded_and_configurable(self):
        config = (ROOT / "adaptive_ai" / "config.yaml").read_text(encoding="utf-8")
        settings = self.source("settings.py")
        self.assertIn("training_experience_batch_rows: 64", config)
        self.assertIn('training_experience_batch_rows: "int(8,512)"', config)
        self.assertIn('"training_experience_batch_rows": 64', settings)


if __name__ == "__main__":
    unittest.main()
