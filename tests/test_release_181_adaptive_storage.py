"""Adaptive capacity, restart invariance and startup under a soft database quota."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
import storage_files
from automatic_correct_rewards import AutomaticRewardJournal
from storage import Store
from storage_budget import StorageBudget, StoragePressure, MIB
import test_packaged_startup as packaged_startup

GIB = 1024 * MIB


class AdaptiveBudget181Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'app.db'

    def simulated_budget(self, free, db=0, wal=0, **options):
        self.path.touch()
        wal_path = Path(str(self.path) + '-wal')
        wal_path.touch()
        original_stat = Path.stat
        def stat(path, *args, **kwargs):
            if path == self.path:
                return SimpleNamespace(st_size=db)
            if path == wal_path:
                return SimpleNamespace(st_size=wal)
            return original_stat(path, *args, **kwargs)
        with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=free)), \
             patch.object(Path, 'stat', stat):
            return StorageBudget(self.path, options)

    def test_defaults_scale_for_different_installations(self):
        small = self.simulated_budget(4 * GIB)
        large = self.simulated_budget(44 * GIB)
        self.assertAlmostEqual(large.db_limit, small.db_limit * 11, delta=11)
        self.assertAlmostEqual(large.wal_limit, small.wal_limit * 11, delta=11)
        self.assertEqual(large.reserve, 22 * GIB)
        self.assertEqual(large.db_limit + large.wal_limit, 22 * GIB)

    def test_screenshot_database_fits_automatic_budget(self):
        budget = self.simulated_budget(21934698496, db=1932783616)
        budget.stats['database_used_bytes'] = 1932783616
        with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=21934698496)):
            self.assertEqual(budget.check(), 'normal')
        self.assertGreater(budget.db_limit * .9, 1932783616)

    def test_restarts_cannot_ratchet_budget_up_with_app_growth(self):
        before = self.simulated_budget(44 * GIB, db=2 * GIB)
        after = self.simulated_budget(34 * GIB, db=11 * GIB, wal=GIB)
        self.assertEqual((before.db_limit, before.wal_limit, before.reserve),
                         (after.db_limit, after.wal_limit, after.reserve))

    def test_other_addons_space_is_not_assigned_to_this_app(self):
        before = self.simulated_budget(44 * GIB, db=2 * GIB)
        after = self.simulated_budget(24 * GIB, db=2 * GIB)
        self.assertLess(after.db_limit, before.db_limit)
        self.assertEqual(after.auto_pool_bytes, 26 * GIB)

    def test_percentage_is_configurable_without_fixed_gib_ceiling(self):
        budget = self.simulated_budget(200 * GIB, storage_auto_use_percent=70)
        self.assertEqual(budget.db_limit + budget.wal_limit, 140 * GIB)
        self.assertEqual(budget.reserve, 60 * GIB)

    def test_legacy_saved_defaults_do_not_reinstate_fixed_quota(self):
        budget = self.simulated_budget(44 * GIB, storage_database_max_mb=2048,
            storage_wal_max_mb=128, storage_min_free_mb=2048)
        self.assertEqual(budget.mode, 'auto')
        self.assertGreater(budget.db_limit, 2 * GIB)

    def test_manual_mode_keeps_user_limits(self):
        budget = self.simulated_budget(44 * GIB, storage_budget_mode='manual',
            storage_database_max_mb=10240, storage_wal_max_mb=512, storage_min_free_mb=4096)
        self.assertEqual((budget.db_limit, budget.wal_limit, budget.reserve),
                         (10 * GIB, 512 * MIB, 4 * GIB))
        self.assertIsNone(budget.auto_pool_bytes)

    def test_unknown_capacity_blocks_until_successful_sample(self):
        with patch('storage_budget.shutil.disk_usage', side_effect=OSError('unavailable')):
            budget = StorageBudget(self.path, {})
            with self.assertRaises(StoragePressure):
                budget.check()
        with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=44 * GIB)):
            self.assertEqual(budget.check(), 'normal')
        self.assertEqual(budget.auto_pool_bytes, 44 * GIB)

    def test_other_addons_growth_cannot_lower_running_reserve(self):
        budget = self.simulated_budget(44 * GIB)
        with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=21 * GIB)):
            self.assertEqual(budget.check(), 'paused')
        self.assertEqual(budget.reserve, 22 * GIB)

    def test_later_worker_inherits_parent_reserve_and_cannot_recalculate_it(self):
        for mode in ('auto', 'manual'):
            parent = self.simulated_budget(44 * GIB, storage_budget_mode=mode)
            with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=44 * GIB)):
                env = parent.worker_environment()
            with patch.dict(os.environ, {**env, 'ADAPTIVE_AI_TRAINING_WORKER': '1'}), \
                 patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=parent.reserve - MIB)):
                child = StorageBudget(self.path, {})
                self.assertEqual((child.mode, child.db_limit, child.wal_limit, child.reserve),
                                 (parent.mode, parent.db_limit, parent.wal_limit, parent.reserve))
                with self.assertRaises(StoragePressure):
                    child.check()

    def test_worker_budget_cannot_be_applied_to_another_database(self):
        budget = self.simulated_budget(44 * GIB)
        with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=44 * GIB)):
            env = budget.worker_environment()
        with patch.dict(os.environ, env), self.assertRaises(StoragePressure):
            StorageBudget(self.path.with_name('another.db'), {})

    def test_automatic_mode_bulk_import_keeps_rows_with_bounded_sampling(self):
        store = Store(self.path)
        budget = store.storage_budget
        before = budget.snapshot().get('disk_samples', 0)
        with patch('storage_budget.time.monotonic', return_value=100.0), store.conn() as c:
            c.executemany('INSERT INTO app_meta VALUES(?,?)', ((str(i), 'value') for i in range(2000)))
        self.assertLess(budget.snapshot()['disk_samples'] - before, 20)
        self.assertEqual(budget.mode, 'auto')
        with store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 2000)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


class RestartMaintenance181Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'app.db')
        self.budget = self.store.storage_budget
        self.journal = AutomaticRewardJournal(self.store, clock=lambda: 100)

    def pending(self, key):
        return self.journal.start(dict(resolution_key=key, agent_id='saved-agent',
            action_ts=1, observation_start=1, observation_end=60,
            target_entity='switch.test', target_property='power'))

    def soft(self):
        return patch('storage_budget.shutil.disk_usage',
                     return_value=SimpleNamespace(free=self.budget.reserve - MIB))

    def test_empty_restart_journal_does_not_issue_update_under_pressure(self):
        with self.soft():
            journal = AutomaticRewardJournal(self.store)
        self.assertEqual(journal.recent('saved-agent'), [])
        self.assertEqual(self.budget.snapshot()['blocked_writes'], 0)

    def test_stale_pending_are_invalidated_in_small_commits_under_soft_pressure(self):
        for i in range(130):
            self.pending(str(i))
        commits = []
        original = self.store.conn
        from contextlib import contextmanager
        @contextmanager
        def conn():
            with original() as c:
                before = c.total_changes
                yield c
                commits.append(c.total_changes - before)
        with self.soft(), patch.object(self.store, 'conn', conn):
            journal = AutomaticRewardJournal(self.store, clock=lambda: 200)
        self.assertEqual(sorted(n for n in commits if n), [2, 128])
        self.assertEqual(journal.get('0')['status'], 'unknown')
        self.assertIsNone(journal.get('0')['trusted_reward'])
        self.assertEqual(journal.get('129')['attribution_reason'], 'restart_interrupted')
        with self.soft(), self.assertRaises(StoragePressure):
            self.pending('new')

    def test_maintenance_permission_does_not_escape_context_or_allow_insert(self):
        self.pending('old')
        sql = "UPDATE automatic_reward_experiences SET status='unknown' WHERE resolution_key='old'"
        with self.soft(), self.store.conn() as c:
            with self.budget.maintenance(update_tables=('automatic_reward_experiences',)):
                c.execute(sql)
                with self.assertRaises(StoragePressure):
                    c.execute("INSERT INTO automatic_reward_experiences SELECT * FROM automatic_reward_experiences WHERE 0")
            with self.assertRaises(StoragePressure):
                c.execute(sql)  # Same cached statement must be reauthorized.

    def test_maintenance_cannot_bypass_hard_free_space_guard(self):
        self.pending('old')
        with patch('storage_budget.shutil.disk_usage',
                   return_value=SimpleNamespace(free=self.budget.hard_reserve - 1)):
            with self.assertRaises(StoragePressure):
                self.journal._interrupt_stale_pending()
        self.assertEqual(self.journal.get('old')['status'], 'pending')

    def test_worker_files_share_automatic_reserve(self):
        root = Path(self.store.path).parent
        with patch.object(storage_files, 'worker_storage_budget', return_value=self.budget), self.soft():
            with self.assertRaises(StoragePressure):
                storage_files.atomic_text(root / 'result.json', 'new')
        self.assertFalse((root / 'result.json').exists())


class AdaptiveStartup181Tests(unittest.TestCase):
    run_isolated = packaged_startup.PackagedStartupTests.run_isolated

    def test_final_entrypoint_reaches_ready_with_database_quota_paused(self):
        self.run_isolated('''
import http.server, runpy, threading
from unittest.mock import Mock, patch
from storage_budget import StorageBudget, StoragePressure, MIB
server = Mock()
server.serve_forever.side_effect = KeyboardInterrupt
original_start = threading.Thread.start
def start(thread):
    import main
    if thread.name == 'adaptive-ai-runtime-init':
        thread._target()
    elif thread.name == 'adaptive-ai-sqlite-checkpoint':
        def pages(budget, connection):
            budget.stats['database_used_bytes'] = 1932783616
        StorageBudget.observe_pages = pages
        main.STORE.storage_budget.db_limit = 2048 * MIB
        main.STORE.storage_budget.stats['database_used_bytes'] = 1932783616
        main.STORE.meta_set('saved-model-proof', 'preserved')
        original_start(thread)
with patch.object(http.server, 'ThreadingHTTPServer', return_value=server), \\
     patch.object(threading.Thread, 'start', start):
    runpy.run_path('adaptive_ai/src/trial_queue_main.py', run_name='__main__')
import main
assert main.startup_snapshot()['ready'], main.startup_snapshot()
assert main.STORE.meta_get('saved-model-proof') == 'preserved'
assert main.STORE.storage_budget.sample()['level'] == 'paused'
assert main.ENGINE.inference_enabled.is_set()
try:
    with main.STORE.conn() as db:
        db.execute("INSERT INTO entity_history(entity_id,ts,state,source) VALUES('sensor.test',1,'on','live')")
except StoragePressure:
    pass
else:
    raise AssertionError('new history was admitted past the quota')
''')
