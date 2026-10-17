"""Real SQLite recovery, protected roots and bounded background traversal."""
import json
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

import support
from observation_contract import FeatureJournal
from provenance import ProvenanceJournal
from settings import OPTIONS
from storage import Store
from storage_budget import StoragePressure
from storage_recovery import StorageRecovery
from storage_retention import StorageRetention


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.dict(OPTIONS, storage_budget_mode='manual'):
            self.store = Store(Path(folder.name) / 'recovery.db')
        self.addCleanup(self.store.stop_storage_retention)
        self.now = time.time()
        self.stop = threading.Event()
        self.gc = StorageRecovery(self.store, self.stop)
        self.gc.SLICE_SECONDS = 2.
        self.journal = FeatureJournal(self.store)
        self.provenance = ProvenanceJournal(self.store)
        patcher = patch.dict(OPTIONS, storage_diagnostic_retention_hours=24, storage_pressure_retention_hours=1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def events(self, count=256, *, age=7200., deadline=0., start=0, payload=8192):
        stamp = self.now - age
        with self.store.conn() as c:
            c.executemany('INSERT INTO feature_observation_events '
                '(event_key,contract_version,entity_id,event_time,received_time,attributes_json,source,quality,protected_until) '
                'VALUES(?,1,?,?,?,?,?,1,?)', [(f'event-{i}', 'sensor.room', stamp, stamp,
                    json.dumps({'trace': 'x' * payload}), 'ha', deadline) for i in range(start, start + count)])

    def window(self, name='protected', *, deadline=None, matching=True):
        with self.store.conn() as c:
            c.execute('INSERT INTO feature_windows VALUES(?,1,?,?,?,?,?,?,?)',
                (name, 'agent', self.now-7200, self.now-7300, self.now-7100,
                 'feedback', self.now-7400, self.now+86400 if deadline is None else deadline))
            c.execute('INSERT INTO feature_window_entities VALUES(?,?)',
                      (name, 'sensor.room' if matching else 'sensor.other'))

    def pressure(self, fraction=.91):
        b = self.store.storage_budget
        with self.store.conn() as c:
            b.observe_pages(c)
        used = b.snapshot()['database_used_bytes']
        b.db_limit = int(used / fraction)
        b.sample()
        return used

    def count(self, table):
        with self.store.conn() as c:
            return c.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def drain(self, passes=30):
        for _ in range(passes):
            self.gc.step(self.now)
            if not self.gc.active:
                break

    def test_real_page_pressure_recovers_without_increasing_quota(self):
        self.events()
        before = self.pressure()
        budget = self.store.storage_budget
        limit = budget.db_limit
        self.assertEqual(budget.snapshot()['reason'], 'database_budget')
        with self.assertRaises(StoragePressure), self.store.conn() as c:
            c.execute("INSERT INTO entity_history(entity_id,ts,state,source) VALUES('sensor.room',1,'on','live')")
        self.drain()
        self.assertEqual(budget.db_limit, limit)
        self.assertLess(budget.snapshot()['database_used_bytes'], before)
        self.assertLess(budget.snapshot()['database_used_bytes'], limit * .8)
        self.assertEqual(budget.sample()['level'], 'normal')
        with self.store.conn() as c:
            c.execute("INSERT INTO entity_history(entity_id,ts,state,source) VALUES('sensor.room',1,'on','live')")
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_preemptive_high_low_watermarks_do_not_flap_at_pause_threshold(self):
        self.events()
        self.pressure(.86)
        self.assertEqual(self.store.storage_budget.snapshot()['level'], 'normal')
        with patch.object(self.gc, '_batch', return_value=(0, 128, (1., 128))):
            self.gc.step(self.now)
        self.assertTrue(self.gc.active)
        self.store.storage_budget.db_limit = int(self.store.storage_budget.snapshot()['database_used_bytes'] / .83)
        with patch.object(self.gc, '_batch', return_value=(0, 128, (1., 128))):
            self.gc.step(self.now)
        self.assertTrue(self.gc.active)
        self.drain()
        self.assertFalse(self.gc.active)

    def test_live_feature_window_protects_matching_event_but_not_orphan_stamp(self):
        self.events(count=4, deadline=self.now+86400)
        self.window()
        self.gc.hours = lambda *args: 1.
        self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 4)
        with self.store.conn() as c:
            # The actual reference wins even if a stale cached event deadline
            # predates the currently protected window.
            c.execute('UPDATE feature_observation_events SET protected_until=0')
        self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 4)
        with self.store.conn() as c:
            c.execute('DELETE FROM feature_window_entities')
            c.execute('DELETE FROM feature_windows')
        self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 0)

    def test_foreign_entity_or_nonoverlapping_window_does_not_pin_an_event(self):
        self.events(count=4, deadline=self.now+86400)
        self.window(matching=False)
        self.gc.hours = lambda *args: 1.
        self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 0)
        spec = self.gc._spec('feature_observation_events', dict.fromkeys(
            ['received_time','protected_until','entity_id','event_time']),
            {'feature_windows','feature_window_entities'}, self.now)
        with self.store.conn() as c:
            plan = c.execute('EXPLAIN QUERY PLAN SELECT event_key FROM feature_observation_events WHERE '
                             + spec[1], spec[2]).fetchall()
        self.assertTrue(any('SEARCH win USING INDEX idx_storage_window_start' in row[3] for row in plan), plan)

    def test_normal_ttl_does_not_shorten_recent_journal_to_pressure_floor(self):
        self.events(count=2)
        self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 2)
        self.gc.step(self.now + 25 * 3600)
        self.assertEqual(self.count('feature_observation_events'), 0)

    def test_pressure_preserves_recent_diagnostics_and_fourteen_day_replay(self):
        self.events()
        self.events(count=2, start=1000, age=30.)
        with self.store.conn() as c:
            c.execute("INSERT INTO entity_history(entity_id,ts,state,source) VALUES('sensor.keep',?,'on','history')",
                      (self.now - 13 * 86400,))
            c.execute("INSERT INTO app_meta VALUES('durable-preference','keep')")
            c.execute('CREATE TABLE protected_candidate_weights(id TEXT,ts REAL)')
            c.execute("INSERT INTO protected_candidate_weights VALUES('model',?)", (self.now - 100 * 86400,))
        self.pressure()
        self.drain()
        self.assertEqual(self.count('entity_history'), 1)
        self.assertEqual(self.count('protected_candidate_weights'), 1)
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT value FROM app_meta WHERE key='durable-preference'").fetchone()[0], 'keep')
            self.assertEqual(c.execute("SELECT count(*) FROM feature_observation_events WHERE event_key IN ('event-1000','event-1001')").fetchone()[0], 2)

    def test_only_unreferenced_ordinary_shadow_or_rejected_decisions_expire(self):
        with self.store.conn() as c:
            for name, status, teaching in [('shadow','SHADOW',None),('reject','REJECTED',None),
                    ('command','ACCEPTED',None),('teach','SHADOW',1),('experience','SHADOW',None),('pending','SHADOW',None)]:
                c.execute('INSERT INTO provenance_decisions(decision_id,contract_version,created_time,agent_id,'
                    'feature_manifest_json,allowed_actions_json,dispatch_status,teaching_id) VALUES(?,1,?,?,?, ?,?,?)',
                    (name,self.now-7200,'agent','{}','[]',status,teaching))
            c.execute("INSERT INTO provenance_experiences(experience_key,contract_version,created_time,agent_id,decision_id,source) VALUES('exp',1,?,'agent','experience','manual')",(self.now,))
            c.execute("INSERT INTO provenance_commands(command_id,entity_id,target_property,desired_value,deadband,created_time,expires_time,status,decision_id) VALUES('cmd','light.room','power',1,.5,?,?,'pending','pending')",(self.now,self.now+10))
        self.gc.hours = lambda *args: 1.
        self.gc.step(self.now)
        with self.store.conn() as c:
            self.assertEqual({row[0] for row in c.execute('SELECT decision_id FROM provenance_decisions')},
                             {'command','teach','experience','pending'})

    def test_expired_window_children_are_bounded_and_deleted_before_parent(self):
        self.window(deadline=self.now-1)
        self.gc.hours = lambda *args: 1.
        self.gc.step(self.now)
        self.assertEqual(self.count('feature_windows'), 0)
        self.assertEqual(self.count('feature_window_entities'), 0)

    def test_pinned_rows_do_not_hide_collectable_rows_after_first_batch(self):
        self.events(count=130, deadline=self.now+86400)
        self.window()
        with self.store.conn() as c:
            c.execute("UPDATE feature_observation_events SET entity_id='sensor.other' WHERE event_key='event-129'")
        self.gc.hours = lambda *args: 1.
        self.gc.MAX_BATCHES = 1
        for _ in range(15):
            self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 129)

    def test_blocked_store_lock_never_waits_for_foreground_writer(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            with self.store.lock:
                entered.set()
                release.wait(3)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            with patch.object(self.gc, '_pressure', return_value=self.store.storage_budget.snapshot()):
                self.gc.step(self.now)
            self.assertEqual(self.gc.snapshot()['busy'], 1)
        finally:
            release.set(); thread.join(3)

    def test_stop_and_cooperative_slice_prevent_extra_batches(self):
        self.events()
        self.stop.set()
        with patch.object(self.gc, '_batch', side_effect=AssertionError('stopped batch')):
            self.gc.step(self.now)
        self.stop.clear()
        self.gc.SLICE_SECONDS = 0
        with patch.object(self.gc, '_batch', side_effect=AssertionError('expired slice')):
            self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 256)

    def test_slow_metadata_still_allows_one_bounded_batch_per_slice(self):
        self.events(count=2, age=100000)
        for _ in range(3):
            ticks = iter([0., 10.])
            with patch('storage_recovery.time.monotonic', side_effect=lambda: next(ticks, 10.)):
                self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 0)

    def test_failed_delete_rolls_back_and_does_not_advance_cursor_or_counts(self):
        self.events(count=2, age=100000)
        original = self.gc._batch
        def fail(*args):
            original(*args)
            raise ValueError('failed transaction')
        with patch.object(self.gc, '_batch', side_effect=fail):
            self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 2)
        self.assertEqual(self.gc.snapshot()['deleted'], 0)
        self.assertEqual(self.gc.cursors, {})
        self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 0)

    def test_pressure_wakes_retention_without_disk_io_in_snapshot(self):
        worker = StorageRetention(self.store)
        self.store.storage_budget.recovery_event = worker.wake_event
        self.events()
        self.pressure()
        self.assertTrue(worker.wake_event.is_set())
        with patch('storage_budget.shutil.disk_usage', side_effect=AssertionError('snapshot I/O')):
            self.assertIn('recovery', worker.snapshot())

    def test_live_debug_status_exposes_ram_cleanup_progress(self):
        from types import SimpleNamespace
        from runtime_debug_log import RuntimeDebugLogService
        self.store._retention_worker = StorageRetention(self.store)
        service = RuntimeDebugLogService(SimpleNamespace(STORE=self.store), SimpleNamespace())
        with patch('storage_budget.shutil.disk_usage', side_effect=AssertionError('panel disk I/O')):
            snapshot = service.status()['sqlite']['storage_retention']['recovery']
        self.assertEqual(snapshot['deleted'], 0)
        self.assertEqual(snapshot['diagnostic_hours'], 24)

    def test_background_worker_recovers_automatically_without_manual_gc(self):
        self.events()
        self.pressure()
        self.store.start_storage_retention()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.store.storage_budget.snapshot()['level'] == 'normal':
                break
            self.stop.wait(.01)
        self.assertEqual(self.store.storage_budget.snapshot()['level'], 'normal')
        self.assertGreater(self.store.sqlite_snapshot()['storage_retention']['recovery']['deleted'], 0)

    def test_wal_pressure_waits_for_checkpoint_instead_of_erasing_extra_data(self):
        self.events(count=2, age=100000)
        b = self.store.storage_budget
        def wal_sample():
            with b.lock:
                b.stats.update(level='paused', reason='wal_budget')
                return dict(b.stats)
        with patch.object(b, 'sample', side_effect=wal_sample):
            self.gc.step(self.now)
        self.assertEqual(self.count('feature_observation_events'), 2)
        self.assertEqual(self.gc.snapshot()['reason'], 'waiting_for_checkpoint')

    def test_malformed_ttl_options_are_finite_and_bounded(self):
        for value, expected in [('bad',24), (float('nan'),24), (float('inf'),24), (-1,1), (9999,336)]:
            with patch.dict(OPTIONS, storage_diagnostic_retention_hours=value):
                self.assertEqual(self.gc.snapshot()['diagnostic_hours'], expected)

    def test_warning_and_error_logs_keep_their_original_retention(self):
        stamp = datetime.fromtimestamp(self.now-7200, timezone.utc).isoformat()
        with self.store.conn() as c:
            c.executemany('INSERT INTO events(created_at,level,kind,message) VALUES(?,?,?,?)',
                         [(stamp,level,level,level) for level in ('info','debug','warning','error')])
        self.gc.hours = lambda *args: 1.
        self.gc.step(self.now)
        with self.store.conn() as c:
            self.assertEqual({row[0] for row in c.execute('SELECT level FROM events')}, {'warning','error'})


class ForegroundDecisionPriorityTests(unittest.TestCase):
    def setUp(self):
        from test_release_205_control_priority import ObserverIntegrationTests
        self.f = ObserverIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_due_optional_work_yields_to_another_active_decision(self):
        f = self.f
        f.run_agent()
        f.fixture.service._foreground_threads.add(-1)
        f.now = 11.
        with patch.object(f.fixture.service, 'observe_shadow', wraps=f.fixture.service.observe_shadow) as observe:
            self.assertEqual(f.run_agent({f.fixture.active}), 'production-result')
            observe.assert_not_called()
            f.fixture.service._foreground_threads.clear()
            self.assertEqual(f.run_agent({f.fixture.active}), 'production-result')
            observe.assert_called_once()

    def test_target_edges_still_run_and_realtime_deferral_cannot_starve_observer(self):
        f = self.f
        f.run_agent()
        f.fixture.service._foreground_threads.add(-1)
        f.now = 15.
        with patch.object(f.fixture.service, 'observe_shadow', wraps=f.fixture.service.observe_shadow) as observe:
            f.run_agent({f.fixture.active})
            self.assertEqual(observe.call_count, 1)
            f.fixture.states[f.fixture.target]['state'] = 'on'
            f.now += .01
            f.run_agent({f.fixture.target})
            self.assertEqual(observe.call_count, 2)

    def test_real_second_thread_in_foreground_defers_optional_observer(self):
        import test_context_tournament_shadow as fixture
        entered, release = threading.Event(), threading.Event()
        original = fixture.FakeEngine.process_agent
        def held(engine, agent, states, changed_entities=None):
            if agent['id'] == 'held':
                entered.set()
                release.wait(3)
            return original(engine, agent, states, changed_entities)
        with patch.object(fixture.FakeEngine, 'process_agent', new=held):
            f = fixture.ContextTournamentShadowTests()
            f.setUp()
        self.addCleanup(f.tearDown)
        f.engine._inference_tls = threading.local()
        f.engine._inference_tls.state_revision = 1
        clock = [10.]
        f.service._observation_scheduler.clock = lambda: clock[0]
        f.engine.process_agent(f.agent, f.states, {f.active})
        errors = []
        def run_held():
            try:
                f.engine._inference_tls.state_revision = 1
                f.engine.process_agent(f.agent | {'id': 'held'}, f.states, {f.active})
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run_held)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            clock[0] = 11.
            with patch.object(f.service, 'observe_shadow', wraps=f.service.observe_shadow) as observe:
                self.assertEqual(f.engine.process_agent(f.agent, f.states, {f.active}), 'production-result')
                observe.assert_not_called()
        finally:
            release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(f.service._foreground_threads, set())


if __name__ == '__main__':
    unittest.main()
