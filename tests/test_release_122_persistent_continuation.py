"""0.14.122 persistent continuation-row cache regressions."""
import unittest
from pathlib import Path

import history as history_module
from history import stateful_continuation_seed_rows_from_rows
from training_process import aggregate_training_sequence_profile


def row(row_id, ts, state, entity_id="light.test"):
    return {
        "id": int(row_id),
        "entity_id": entity_id,
        "ts": float(ts),
        "received_ts": float(ts),
        "state": state,
        "attributes_json": "{}",
        "context_user_id": None,
        "source": "test",
    }


def agent(agent_id="agent-a", entity_id="light.test"):
    return {
        "id": agent_id,
        "target_entity": entity_id,
        "target_property": "power",
        "deadband": 0.5,
    }


class ContinuationRowReducerTests(unittest.TestCase):
    def test_cached_rows_reduce_to_same_open_dwell_seed_contract(self):
        a = agent()
        target_map = {"light.test": [a]}
        rows = [
            row(1, 1.0, "off"),
            row(2, 2.0, "on"),
            row(3, 3.0, "on"),
            row(4, 4.0, "off"),
            row(5, 5.0, "on"),
        ]

        seeds, scanned = stateful_continuation_seed_rows_from_rows(
            rows, [a], target_map, 2.0, 5.0
        )

        self.assertEqual(scanned, 3)
        self.assertEqual(seeds["agent-a"][0]["id"], 4)
        self.assertEqual(seeds["agent-a"][1], 0.0)

    def test_boundary_row_is_never_visible_to_previous_open_dwell(self):
        a = agent()
        target_map = {"light.test": [a]}
        seeds, scanned = stateful_continuation_seed_rows_from_rows(
            [row(1, 10.0, "off"), row(2, 20.0, "on")],
            [a],
            target_map,
            10.0,
            20.0,
        )
        self.assertEqual(scanned, 1)
        self.assertEqual(seeds["agent-a"][0]["id"], 1)


class PersistentContinuationCacheTests(unittest.TestCase):
    def manager(self):
        manager = history_module.HistoryManager.__new__(
            history_module.HistoryManager
        )
        manager._persistent_continuation_seed_state = None
        return manager

    def test_exact_covered_window_uses_cached_target_rows(self):
        manager = self.manager()
        a = agent()
        rows = [
            row(1, 100.0, "off"),
            row(2, 110.0, "on"),
            row(3, 120.0, "on"),
        ]
        seeds, scanned = stateful_continuation_seed_rows_from_rows(
            rows, [a], {"light.test": [a]}, 100.0, 130.0
        )
        manager._remember_persistent_continuation_seed(
            [a], seeds, scanned, 100.0, 130.0
        )

        result = manager._persistent_continuation_seed_rows_for(
            [a], {"light.test": [a]}, 100.0, 130.0
        )

        self.assertIsNotNone(result)
        seeds, scanned = result
        self.assertEqual(scanned, 3)
        self.assertEqual(seeds["agent-a"][0]["id"], 2)
        self.assertEqual(seeds["agent-a"][1], 1.0)

    def test_empty_cached_overlap_is_a_valid_cache_hit(self):
        manager = self.manager()
        a = agent()
        manager._remember_persistent_continuation_seed(
            [a], {}, 0, 100.0, 130.0
        )

        result = manager._persistent_continuation_seed_rows_for(
            [a], {"light.test": [a]}, 100.0, 130.0
        )

        self.assertEqual(result, ({}, 0))

    def test_uncovered_or_changed_scope_falls_back(self):
        manager = self.manager()
        a = agent()
        manager._remember_persistent_continuation_seed(
            [a],
            {"agent-a": (row(1, 100.0, "off"), 0.0)},
            1,
            100.0,
            130.0,
        )

        self.assertIsNone(
            manager._persistent_continuation_seed_rows_for(
                [a], {"light.test": [a]}, 90.0, 130.0
            )
        )
        changed = agent(entity_id="light.other")
        self.assertIsNone(
            manager._persistent_continuation_seed_rows_for(
                [changed], {"light.other": [changed]}, 100.0, 130.0
            )
        )

    def test_close_releases_continuation_rows(self):
        manager = self.manager()
        manager._persistent_replay_sqlite_connection = None
        manager._persistent_replay_query_cache = None
        manager._persistent_home_context_cache = None
        manager._persistent_ram_replay_index = None
        manager._persistent_transition_edge_index = None
        manager._persistent_feature_snapshot_cache = None
        manager._persistent_experience_ids = {}
        manager._persistent_replay_provenance_state = None
        manager._persistent_continuation_seed_state = {
            "rows": 1,
            "seeds": {"agent-a": (row(1, 1, "off"), 0.0)},
        }

        manager.close_persistent_training_resources()

        self.assertIsNone(manager._persistent_continuation_seed_state)


class ContinuationProfileTests(unittest.TestCase):
    def test_session_profile_aggregates_cache_hits_and_avoided_db_rows(self):
        reports = [
            {
                "elapsed_seconds": 1.0,
                "training_phase_timings": {
                    "continuation_seed_load_seconds": 0.02,
                    "continuation_seed_cache_hits": 0,
                    "continuation_seed_db_loads": 1,
                    "continuation_seed_rows": 20,
                    "continuation_seed_avoided_db_rows": 0,
                    "continuation_cached_rows": 20,
                },
            },
            {
                "elapsed_seconds": 0.5,
                "training_phase_timings": {
                    "continuation_seed_load_seconds": 0.001,
                    "continuation_seed_cache_hits": 1,
                    "continuation_seed_db_loads": 0,
                    "continuation_seed_rows": 20,
                    "continuation_seed_avoided_db_rows": 20,
                    "continuation_cached_rows": 18,
                },
            },
        ]

        profile = aggregate_training_sequence_profile(reports)

        self.assertEqual(profile["counters"]["continuation_seed_cache_hits"], 1)
        self.assertEqual(profile["counters"]["continuation_seed_db_loads"], 1)
        self.assertEqual(profile["counters"]["continuation_seed_avoided_db_rows"], 20)
        self.assertEqual(profile["counters"]["continuation_cached_rows"], 38)
        self.assertAlmostEqual(
            profile["phase_seconds"]["continuation_seed_load_seconds"], 0.021
        )


class Release122SourceContractTests(unittest.TestCase):
    def test_release_keeps_sqlite_fallback_and_surfaces_cache_metrics(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "adaptive_ai" / "src" / "history.py").read_text(
            encoding="utf-8"
        )
        for marker in (
            "_persistent_continuation_seed_state",
            "_persistent_continuation_seed_rows_for",
            "_remember_persistent_continuation_seed",
            "_apply_stateful_continuation_seed_row",
            "stateful_continuation_seed_rows(",
            "continuation_seed_cache_hits",
            "continuation_seed_db_loads",
            "continuation_seed_rows",
            "continuation_seed_avoided_db_rows",
            "continuation_cached_rows",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
