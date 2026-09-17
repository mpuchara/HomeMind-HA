import sqlite3
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

import agent_candidate_preference_metrics as preference
import performance_f22_order_guard as order_guard


class MemoryStore:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.events = []

    @contextmanager
    def conn(self):
        with self.db:
            yield self.db

    def event(self, *args):
        self.events.append(args)


class LateOutcomeOrderGuardTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        with self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE agent_candidate_generations (
                    generation_id TEXT PRIMARY KEY,
                    parent_generation_id TEXT,
                    agent_id TEXT,
                    created_ts REAL
                );
                CREATE TABLE candidate_generation_pairs (
                    parent_generation_id TEXT NOT NULL,
                    child_generation_id TEXT NOT NULL,
                    outcome_ts REAL NOT NULL
                );
                """
            )
            c.execute(
                "INSERT INTO agent_candidate_generations VALUES(?,?,?,?)",
                ("g1", "g0", "candidate", 1.0),
            )
        self.manager = SimpleNamespace(store=self.store, performance_f22_contract={})
        self.original_fast = preference._fast_metrics
        self.had_legacy = hasattr(preference, "_f22_legacy_fast_metrics")
        self.original_legacy = getattr(preference, "_f22_legacy_fast_metrics", None)
        self.had_flag = hasattr(preference, "_f22_order_guard_global_installed")
        self.original_flag = getattr(preference, "_f22_order_guard_global_installed", None)
        self.calls = {"optimized": 0, "legacy": 0}

        def optimized(*args, **kwargs):
            self.calls["optimized"] += 1
            return {"source": "optimized", "value": 1.0}

        def legacy(*args, **kwargs):
            self.calls["legacy"] += 1
            return {"source": "legacy", "value": 2.0}

        preference._fast_metrics = optimized
        preference._f22_legacy_fast_metrics = legacy
        if hasattr(preference, "_f22_order_guard_global_installed"):
            delattr(preference, "_f22_order_guard_global_installed")
        order_guard.install(self.manager)

    def tearDown(self):
        preference._fast_metrics = self.original_fast
        if self.had_legacy:
            preference._f22_legacy_fast_metrics = self.original_legacy
        elif hasattr(preference, "_f22_legacy_fast_metrics"):
            delattr(preference, "_f22_legacy_fast_metrics")
        if self.had_flag:
            preference._f22_order_guard_global_installed = self.original_flag
        elif hasattr(preference, "_f22_order_guard_global_installed"):
            delattr(preference, "_f22_order_guard_global_installed")

    def _metric(self):
        return preference._fast_metrics(
            self.manager,
            {"candidate_id": "candidate", "parent_agent_id": "live", "queued_ts": 1.0},
            {"id": "live"},
            {"id": "candidate"},
            {},
            {"teach_fit_total": 0, "teach_fit_after_count": 0},
        )

    def test_late_outcome_uses_exact_legacy_path_then_caches_repeated_status(self):
        with self.store.conn() as c:
            c.execute("INSERT INTO candidate_generation_pairs VALUES(?,?,?)", ("g0", "g1", 100.0))

        first = self._metric()
        self.assertEqual(first["source"], "optimized")
        self.assertEqual(self.calls, {"optimized": 1, "legacy": 0})

        # A later SQLite row carries an older semantic outcome timestamp.  Appending it
        # to an order-sensitive half-life statistic would differ from legacy ORDER BY outcome_ts.
        with self.store.conn() as c:
            c.execute("INSERT INTO candidate_generation_pairs VALUES(?,?,?)", ("g0", "g1", 90.0))

        repaired = self._metric()
        self.assertEqual(repaired["source"], "legacy")
        self.assertEqual(self.calls["legacy"], 1)

        # The exact result is durable/cacheable; a UI poll with unchanged evidence does
        # not repeat the full legacy calculation.
        repeated = self._metric()
        self.assertEqual(repeated, repaired)
        self.assertEqual(self.calls["legacy"], 1)
        guard = order_guard._row(self.store, "g0", "g1")
        self.assertTrue(bool(guard["fallback_legacy"]))
        self.assertIsNotNone(guard["metrics_json"])


if __name__ == "__main__":
    unittest.main()
