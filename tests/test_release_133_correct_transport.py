"""0.14.133 compact Correct transport regressions."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import storage
from agent_candidate_lineage import _ensure_root, ensure_lineage_tables
from agent_candidate_shadow_runtime import ensure_shadow_tables
from agent_correct_generation_history import build_correct_history


class FakeTeaching:
    def labels(self, agent_id):
        return []


class CompactCorrectTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "compact-correct.db")
        ensure_lineage_tables(self.store)
        ensure_shadow_tables(self.store)
        self.agent = self.store.create_agent({
            "name": "Compact light", "target_entity": "light.compact",
            "target_property": "power", "min_value": 0, "max_value": 1,
            "deadband": 0.5, "action_interval": 0.25,
            "exploration_step": 1, "input_entities": [],
        })
        self.store.save_model(self.agent["id"], {
            "version": 10, "model_revision": "compact", "schema": {"version": 1},
            "selection_meta": {"schema_revision": 1},
        })
        self.generation = _ensure_root(self.store, self.agent["id"], 0)
        self.engine = SimpleNamespace(
            rl_teaching=FakeTeaching(),
            teaching=SimpleNamespace(buffer=[]),
            policy=Mock(side_effect=AssertionError("Correct history must not replay policy")),
        )
        self.manager = SimpleNamespace(store=self.store, engine=self.engine)
        self.manager._generation = lambda agent_id: 0
        self.ts = time.time() - 600
        self.store.archive_batch([
            ("light.compact", self.ts + i, "on" if i % 2 else "off", {}, None, "test")
            for i in range(400)
        ])
        with self.store.lock, self.store.conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS decision_history (
                  agent_id TEXT NOT NULL, ts REAL NOT NULL, current REAL, desired REAL,
                  PRIMARY KEY(agent_id,ts)
                );
            """)
            c.executemany(
                "INSERT INTO decision_history(agent_id,ts,current,desired) VALUES(?,?,?,?)",
                [(self.agent["id"], self.ts + i, float(i % 2), float((i // 3) % 2)) for i in range(400)],
            )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def decode(payload, name):
        item = payload["series"][name]
        if "points" in item:
            return list(item["points"])
        return [{"ts": float(row[0]), "value": float(row[1])} for row in item.get("pairs") or []]

    def history(self, compact):
        return build_correct_history(
            self.manager, self.generation["generation_id"], self.ts, self.ts + 399,
            Mock(side_effect=AssertionError("legacy history forbidden")), compact=compact,
        )

    def test_compact_and_legacy_series_are_semantically_identical(self):
        legacy, compact = self.history(False), self.history(True)
        self.assertTrue(compact["compact"])
        self.assertEqual(compact["series_encoding"], "pairs_v1")
        self.assertNotIn("points", compact)
        for name in ("current", "live_desired"):
            self.assertEqual(self.decode(compact, name), self.decode(legacy, name))
        self.assertEqual(compact["gaps"], legacy["gaps"])
        self.assertEqual(compact["labels"], legacy["labels"])
        self.engine.policy.assert_not_called()

    def test_compact_wire_payload_is_less_than_45_percent_of_legacy(self):
        legacy, compact = self.history(False), self.history(True)
        legacy_bytes = len(json.dumps(legacy, separators=(",", ":")).encode("utf-8"))
        compact_bytes = len(json.dumps(compact, separators=(",", ":")).encode("utf-8"))
        self.assertLess(compact_bytes / legacy_bytes, 0.45)


class CompactCorrectUiContractTests(unittest.TestCase):
    def test_ui_requests_and_normalizes_pairs_v1(self):
        source = (Path(__file__).resolve().parents[1] / "adaptive_ai/src/static/agent_workflow_ui.js").read_text(encoding="utf-8")
        self.assertIn("&compact=1", source)
        self.assertIn("normalizeCompactSeries", source)
        self.assertIn("Array.isArray(item?.pairs)", source)
        self.assertIn("delete item.pairs", source)


if __name__ == "__main__":
    unittest.main()
