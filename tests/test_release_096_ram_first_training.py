"""0.14.96 RAM-first historical training regressions."""
import sys
import unittest

from support import ROOT

SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from replay import ReplayQueryCache
from settings import APP_VERSION, DEFAULT_OPTIONS
from training_budget import CooperativeTrainingBudget
from training_process import (
    RESOURCE_PROFILE_CONTRACT,
    resolve_training_resource_profile,
)


class Release096ResourceProfileTests(unittest.TestCase):
    def test_release_defaults_are_ram_first_but_keep_single_worker_and_nice(self):
        self.assertEqual(APP_VERSION, "0.14.96")
        self.assertEqual(DEFAULT_OPTIONS["training_cpu_duty_cycle"], 0.85)
        self.assertEqual(DEFAULT_OPTIONS["training_worker_memory_limit_mb"], 1024)
        self.assertEqual(DEFAULT_OPTIONS["training_replay_ram_cache_rows"], 65536)
        self.assertEqual(DEFAULT_OPTIONS["training_home_context_cache_entries"], 64)
        self.assertEqual(DEFAULT_OPTIONS["training_sqlite_cache_mb"], 32)
        self.assertEqual(DEFAULT_OPTIONS["training_worker_nice"], 10)
        self.assertEqual(DEFAULT_OPTIONS["max_concurrent_training_jobs"], 1)

    def test_large_pi_profile_uses_full_bounded_cache_budget(self):
        profile = resolve_training_resource_profile(
            DEFAULT_OPTIONS,
            {"total_mb": 3900.0, "available_mb": 2400.0},
        )
        self.assertEqual(profile["contract"], RESOURCE_PROFILE_CONTRACT)
        self.assertEqual(profile["tier"], "large")
        self.assertEqual(profile["effective_memory_limit_mb"], 1024)
        worker = profile["worker_options"]
        self.assertEqual(worker["training_worker_effective_replay_cache_rows"], 65536)
        self.assertEqual(worker["training_worker_effective_replay_cache_entry_rows"], 2048)
        self.assertEqual(worker["training_worker_effective_home_context_cache_entries"], 64)
        self.assertEqual(worker["training_worker_effective_home_context_cache_units"], 32768)
        self.assertEqual(worker["training_worker_effective_sqlite_cache_mb"], 32)

    def test_medium_host_keeps_headroom_for_parent(self):
        profile = resolve_training_resource_profile(
            DEFAULT_OPTIONS,
            {"total_mb": 2048.0, "available_mb": 1200.0},
        )
        self.assertEqual(profile["effective_memory_limit_mb"], 600)
        self.assertEqual(profile["tier"], "medium")
        worker = profile["worker_options"]
        self.assertEqual(worker["training_worker_effective_replay_cache_rows"], 16384)
        self.assertEqual(worker["training_worker_effective_home_context_cache_entries"], 16)
        self.assertEqual(worker["training_worker_effective_sqlite_cache_mb"], 16)

    def test_low_memory_host_falls_back_to_small_profile(self):
        profile = resolve_training_resource_profile(
            DEFAULT_OPTIONS,
            {"total_mb": 1024.0, "available_mb": 600.0},
        )
        self.assertEqual(profile["effective_memory_limit_mb"], 256)
        self.assertEqual(profile["tier"], "small")
        self.assertEqual(
            profile["worker_options"]["training_worker_effective_replay_cache_rows"],
            8192,
        )

    def test_user_cache_caps_remain_authoritative(self):
        options = dict(DEFAULT_OPTIONS)
        options.update({
            "training_worker_memory_limit_mb": 1500,
            "training_replay_ram_cache_rows": 12000,
            "training_replay_ram_cache_entry_rows": 700,
            "training_home_context_cache_entries": 12,
            "training_home_context_cache_units": 6000,
            "training_sqlite_cache_mb": 12,
        })
        profile = resolve_training_resource_profile(
            options,
            {"total_mb": 8192.0, "available_mb": 6000.0},
        )
        worker = profile["worker_options"]
        self.assertEqual(profile["effective_memory_limit_mb"], 1500)
        self.assertEqual(worker["training_worker_effective_replay_cache_rows"], 12000)
        self.assertEqual(worker["training_worker_effective_replay_cache_entry_rows"], 700)
        self.assertEqual(worker["training_worker_effective_home_context_cache_entries"], 12)
        self.assertEqual(worker["training_worker_effective_home_context_cache_units"], 6000)
        self.assertEqual(worker["training_worker_effective_sqlite_cache_mb"], 12)


class Release096CacheAndBudgetTests(unittest.TestCase):
    def test_replay_cache_reports_hit_rate_and_entry_bound(self):
        cache = ReplayQueryCache(max_rows=8, max_entry_rows=4)
        cache.put("SELECT ?", (1,), [{"id": 1}, {"id": 2}])
        self.assertEqual(len(cache.get("SELECT ?", (1,))), 2)
        self.assertIsNone(cache.get("SELECT ?", (2,)))
        status = cache.status()
        self.assertEqual(status["max_entry_rows"], 4)
        self.assertEqual(status["hits"], 1)
        self.assertEqual(status["misses"], 1)
        self.assertEqual(status["hit_rate"], 0.5)

    def test_training_budget_accepts_85_percent_target(self):
        budget = CooperativeTrainingBudget()
        snapshot = budget.configure(duty_cycle=0.85)
        self.assertEqual(snapshot["training_cpu_duty_cycle"], 0.85)
        self.assertEqual(snapshot["training_wall_duty_cycle_target"], 0.85)

    def test_worker_uses_effective_profile_and_sqlite_cache(self):
        history = (SRC / "history.py").read_text(encoding="utf-8")
        replay = (SRC / "replay.py").read_text(encoding="utf-8")
        process = (SRC / "training_process.py").read_text(encoding="utf-8")
        self.assertIn("training_worker_effective_replay_cache_rows", history)
        self.assertIn("training_worker_effective_home_context_cache_entries", history)
        self.assertIn("training_worker_effective_sqlite_cache_mb", replay)
        self.assertIn("PRAGMA cache_size=-{self.sqlite_cache_kib}", replay)
        self.assertIn('job["resource_profile"] = resource_profile', process)
        self.assertIn(
            'OPTIONS.update(dict(resource_profile.get("worker_options") or {}))',
            process,
        )


if __name__ == "__main__":
    unittest.main()
