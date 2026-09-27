"""Stage 5 tests: 7-day recent window plus bounded sparse long-term memory."""
import json
from pathlib import Path
import unittest

from long_memory import (
    CONTRACT,
    SparseLongMemorySelector,
    collect_sparse_dwells,
    count_completed_dwells,
    filter_unseen_candidates,
    sparse_stratum,
)


DAY = 86400.0


class FakeStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def archive_iter(self, start_ts, end_ts, entity_ids, chunk_size=256):
        ids = tuple(sorted(str(value) for value in entity_ids))
        self.calls.append((float(start_ts), float(end_ts), ids, int(chunk_size)))
        wanted = set(ids)
        for row in sorted(self.rows, key=lambda item: (item["ts"], item["id"])):
            if (
                row["entity_id"] in wanted
                and float(start_ts) <= float(row["ts"]) <= float(end_ts)
            ):
                yield dict(row)


def target_row(row_id, ts, on):
    return {
        "id": int(row_id),
        "entity_id": "light.kitchen",
        "ts": float(ts),
        "state": "on" if on else "off",
        "attributes_json": "{}",
        "context_user_id": None,
        "source": "test",
    }


class SparseLongMemoryTests(unittest.TestCase):
    def agent(self):
        return {
            "id": "kitchen-light",
            "target_entity": "light.kitchen",
            "target_property": "power",
            "deadband": 0.5,
        }

    def test_older_weekend_pattern_is_available_without_recent_fanout(self):
        # Monday 2026-09-28 00:00 UTC. Older rows include a weekend transition pattern.
        reference = 1790553600.0
        old_start = reference - 35 * DAY
        recent_start = reference - 7 * DAY
        saturday = reference - 29 * DAY + 9 * 3600
        rows = [
            target_row(1, saturday, False),
            target_row(2, saturday + 900, True),
            target_row(3, saturday + 1800, False),
            target_row(4, recent_start + 3600, True),
            target_row(5, recent_start + 7200, False),
        ]
        store = FakeStore(rows)
        selected, diag = collect_sparse_dwells(
            store, self.agent(), [0.0, 1.0],
            old_start, recent_start - 0.001, reference, 16,
        )
        self.assertEqual(diag["contract"], CONTRACT)
        self.assertTrue(selected)
        self.assertTrue(
            any(row["stratum_meta"]["day_kind"] == "weekend" for row in selected)
        )
        # Selection reads only target transitions, never all context sensors.
        self.assertTrue(all(call[2] == ("light.kitchen",) for call in store.calls))

    def test_max_samples_and_selector_memory_are_bounded_and_deterministic(self):
        reference = 1790553600.0
        selector_a = SparseLongMemorySelector(7, reference, candidate_cap=32)
        selector_b = SparseLongMemorySelector(7, reference, candidate_cap=32)
        for idx in range(400):
            candidate = {
                "agent_id": "a",
                "history_id": idx + 1,
                "start_ts": reference - (8 + (idx % 28)) * DAY + (idx % 24) * 3600,
                "dwell_seconds": (60, 600, 7200)[idx % 3],
                "action_idx": idx % 2,
                "action_value": float(idx % 2),
            }
            selector_a.consider(candidate)
            selector_b.consider(candidate)
        first = selector_a.selected()
        second = selector_b.selected()
        self.assertLessEqual(len(first), 7)
        self.assertLessEqual(len(selector_a.best), 32)
        self.assertEqual(
            [row["history_id"] for row in first],
            [row["history_id"] for row in second],
        )
        self.assertLessEqual(selector_a.diagnostics(first)["retained_candidate_strata"], 32)

    def test_action_round_robin_prevents_dominant_state_from_consuming_budget(self):
        reference = 1790553600.0
        selector = SparseLongMemorySelector(4, reference)
        for idx in range(24):
            selector.consider({
                "agent_id": "a",
                "history_id": idx + 1,
                "start_ts": reference - (8 + idx % 12) * DAY + (idx % 20) * 3600,
                "dwell_seconds": 600 + idx,
                "action_idx": 0,
                "action_value": 0.0,
            })
        for idx in range(4):
            selector.consider({
                "agent_id": "a",
                "history_id": 100 + idx,
                "start_ts": reference - (9 + idx) * DAY + 18 * 3600,
                "dwell_seconds": 120,
                "action_idx": 1,
                "action_value": 1.0,
            })
        chosen = selector.selected()
        self.assertIn(0, {row["action_idx"] for row in chosen})
        self.assertIn(1, {row["action_idx"] for row in chosen})

    def test_recent_count_and_sparse_selection_scan_target_only(self):
        reference = 1790553600.0
        rows = []
        row_id = 1
        for day in range(35):
            ts = reference - (35 - day) * DAY + 8 * 3600
            rows.append(target_row(row_id, ts, bool(day % 2)))
            row_id += 1
        store = FakeStore(rows)
        recent_start = reference - 7 * DAY
        old, diag = collect_sparse_dwells(
            store, self.agent(), [0.0, 1.0],
            reference - 35 * DAY, recent_start, reference, 8,
        )
        recent = count_completed_dwells(
            store, self.agent(), [0.0, 1.0], recent_start, reference
        )
        self.assertLessEqual(len(old), 8)
        self.assertGreater(diag["target_rows_scanned"], 0)
        self.assertGreaterEqual(recent["completed_dwells"], 1)
        self.assertTrue(all(call[2] == ("light.kitchen",) for call in store.calls))

    def test_restart_filter_is_idempotent_for_already_learned_sparse_dwells(self):
        candidates = [
            {"history_id": 10}, {"history_id": 20}, {"history_id": 30}
        ]
        self.assertEqual(
            [row["history_id"] for row in filter_unseen_candidates(candidates, {10})],
            [20, 30],
        )
        self.assertEqual(
            filter_unseen_candidates(candidates, {10, 20, 30}), []
        )

    def test_strata_include_action_day_kind_daypart_age_and_dwell(self):
        reference = 1790553600.0
        meta = sparse_stratum(1, reference - 20 * DAY + 19 * 3600, 7200, reference)
        self.assertEqual(meta["action"], "1")
        self.assertIn(meta["day_kind"], {"weekday", "weekend"})
        self.assertEqual(meta["daypart"], "evening")
        self.assertEqual(meta["age_bucket"], "15-21d")
        self.assertEqual(meta["dwell_bucket"], "long")
        self.assertEqual(len(meta["stratum"].split("|")), 5)

    def test_source_contract_runs_long_memory_only_on_first_recent_chunk(self):
        root = Path(__file__).resolve().parents[1]
        history = (root / "adaptive_ai" / "src" / "history.py").read_text()
        process = (root / "adaptive_ai" / "src" / "training_process.py").read_text()
        self.assertIn("include_long_memory=(boundary_ts <= start_ts + 0.5)", history)
        self.assertIn("long_memory_recent_start_ts=start_ts", history)
        self.assertIn("long_memory_reference_end_ts=target_end", history)
        self.assertIn('"include_long_memory": bool(kwargs.get("include_long_memory", False))', process)
        self.assertIn('"long_memory_recent_start_ts": kwargs.get("long_memory_recent_start_ts")', process)
        self.assertIn('"long_memory_reference_end_ts": kwargs.get("long_memory_reference_end_ts")', process)


if __name__ == "__main__":
    unittest.main()
