"""0.14.48 regressions for Correct Current and RAM-first training reuse."""
import inspect
import unittest
from types import SimpleNamespace

from history import HistoryManager
from replay import ReplayQueryCache


class Release048CorrectTrainingCacheTests(unittest.TestCase):
    def test_schema_seed_preserves_selection_but_resets_heads(self):
        manager = object.__new__(HistoryManager)
        manager.training_schema_cache = {}
        manager.training_schema_cache_hits = 0
        manager.training_schema_cache_misses = 0
        manager.engine = SimpleNamespace(models={})
        agent = {"id": "a1", "input_entities": ["*"]}
        raw = {
            "version": 10,
            "dims": 128,
            "actions": [0.0, 1.0],
            "horizons": [1],
            "schema": {"version": 1, "dims": 128, "entities": ["binary_sensor.p"]},
            "selection_meta": {"selection_reasons": {"binary_sensor.p": "causal"}},
            "heads": {"1": {"dims": 128, "actions": [0.0, 1.0], "a": [[1]*128,[1]*128], "b": [[0]*128,[0]*128]}},
        }
        seed = manager._remember_training_schema(agent, raw)
        self.assertEqual(seed["schema"]["entities"], ["binary_sensor.p"])
        self.assertEqual(seed["selection_meta"]["selection_reasons"]["binary_sensor.p"], "causal")
        self.assertEqual(seed["heads"], {})
        self.assertIs(manager.training_schema_seed("a1", agent), seed)
        changed = {"id": "a1", "input_entities": ["binary_sensor.other"]}
        self.assertIsNone(manager.training_schema_seed("a1", changed))

    def test_rebuild_history_refresh_prefers_schema_seed(self):
        source = inspect.getsource(HistoryManager._refresh_agent_history)
        self.assertIn("seed = self.training_schema_seed", source)
        self.assertIn("(seed.get(\"schema\") or {}).get(\"entities\")", source)
        self.assertIn("_eligible_rebuild_context()", source)

    def test_training_skips_whole_home_screen_when_schema_seed_exists(self):
        source = inspect.getsource(HistoryManager._train_from_archive)
        self.assertIn("STORE.get_model(a[\"id\"]) or self.training_schema_seed", source)
        self.assertIn("entity_ids=screening_entities", source)
        self.assertIn("context_candidates", source)
        self.assertIn("set(context_candidates) | set(screen_target_map)", source)

    def test_replay_query_cache_is_bounded_and_reuses_rows(self):
        cache = ReplayQueryCache(max_rows=3, max_entry_rows=2)
        cache.put("q1", [1], [{"id": 1}, {"id": 2}])
        first = cache.get("q1", [1])
        self.assertEqual([x["id"] for x in first], [1, 2])
        self.assertEqual(cache.status()["hits"], 1)
        cache.put("q2", [2], [{"id": 3}, {"id": 4}])
        status = cache.status()
        self.assertLessEqual(status["rows"], 3)
        self.assertGreaterEqual(status["evictions"], 1)

    def test_two_replay_trackers_share_one_job_cache(self):
        source = inspect.getsource(HistoryManager._train_from_archive)
        self.assertIn("replay_query_cache = ReplayQueryCache", source)
        self.assertEqual(source.count("query_cache=replay_query_cache"), 2)
        self.assertIn("self.training_replay_cache_status = replay_query_cache.status()", source)


if __name__ == "__main__":
    unittest.main()
