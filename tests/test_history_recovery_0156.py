"""Real on-disk damage fixtures; no synthetic DatabaseError in salvage tests."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

import support
from storage import Store
from history_recovery import prepare_generation, safe_segments, project_segment, publish_generation, RecoveryUnavailable
from replay import RAMReplayIndex, SQLiteTemporalTracker
from context_engine import ContextEngine
from settings import DEFAULT_OPTIONS
from training_process import agent_config_fingerprint, training_history_scope


class RealRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.source = self.root/'source.db'
        self.store = Store(self.source)
        self.entities = ['switch.target', 'binary_sensor.motion']
        rows = []
        for i in range(2500):
            # Physical row order deliberately differs from timestamp order.
            t = 1000.0 + ((i*47) % 2500)*30
            rows.extend([(self.entities[0], t, 'on' if i%2 else 'off', {}, None, 'ha_history_full'),
                         (self.entities[1], t-2, 'on' if i%2 else 'off', {'device_class':'motion'}, None, 'ha_history_full')])
        self.store.archive_batch(rows)
        with self.store.conn() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.page_size = c.execute('PRAGMA page_size').fetchone()[0]
            root = c.execute("SELECT rootpage FROM sqlite_master WHERE name='entity_history'").fetchone()[0]
            self.expected = [tuple(r) for r in c.execute('SELECT * FROM entity_history ORDER BY id')]
        raw = self.source.read_bytes()
        def leaves(page):
            offset = (page-1)*self.page_size
            header = offset + (100 if page == 1 else 0)
            if raw[header] == 13:
                return [(page,)]
            self.assertEqual(raw[header], 5)
            count = int.from_bytes(raw[header+3:header+5], 'big')
            children = [int.from_bytes(raw[header+8:header+12], 'big')]
            for i in range(count):
                pos = int.from_bytes(raw[header+12+2*i:header+14+2*i], 'big') + offset
                children.append(int.from_bytes(raw[pos:pos+4], 'big'))
            return [leaf for child in children for leaf in leaves(child)]
        self.pages = sorted(leaves(root))
        self.destination = self.root/'generation.db'

    def damage(self, page):
        with self.source.open('r+b') as stream:
            stream.seek((int(page)-1)*self.page_size)
            stream.write(b'\xff')

    def recover(self, **kw):
        return prepare_generation(self.source, self.destination, self.entities, 900, 77000, **kw)

    def test_A_corrupt_index_exact_row_parity(self):
        with self.store.conn() as conn:
            page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='idx_entity_history_ts'").fetchone()[0]
        self.damage(page)
        report = self.recover()
        with closing(sqlite3.connect(self.destination)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM entity_history ORDER BY id').fetchall(), self.expected)
        self.assertEqual(report['gaps'], [])

    def test_B_table_page_salvage_and_both_replay_paths(self):
        self.damage(self.pages[len(self.pages)//2][0])
        self._source_before=self.source.read_bytes()
        before = hashlib.sha256(self._source_before).hexdigest()
        with self.assertRaises(sqlite3.DatabaseError):
            with closing(sqlite3.connect(self.source)) as conn:
                conn.execute('SELECT * FROM entity_history NOT INDEXED').fetchall()
        report = self.recover()
        self.assertEqual(report['archive_status'], 'ARCHIVE_CORRUPTED')
        self.assertGreater(report['recovered_records'], 4500)
        self.assertLess(report['recovered_records'], 5000)
        self.assertTrue(report['gaps'])
        self.assertTrue(all(g['failure_reason']=='HISTORY_GAP_SQLITE_CORRUPT' for g in report['gaps']))
        # 0.15.8 adds/updates a bounded prefix ledger; payload leaf pages stay
        # untouched. Source-wide bytes include this intentional metadata write.
        after=self.source.read_bytes()
        for page, in self.pages:
            offset=(page-1)*self.page_size
            self.assertEqual(self._source_before[offset:offset+self.page_size],after[offset:offset+self.page_size])
        with closing(sqlite3.connect(self.destination)) as conn:
            conn.row_factory = sqlite3.Row
            index = RAMReplayIndex.build(conn, self.entities, 900, 77000, max_bytes=8*1048576)
            self.assertGreater(index.status()['rows'], 4000)
        segments = safe_segments(report, 900, 77000)
        self.assertGreater(len(segments), 0)
        lo, hi = max(segments, key=lambda x:x[1]-x[0])
        project_segment(self.destination, lo, hi)
        context = ContextEngine(DEFAULT_OPTIONS)
        context.configure({e:support.state(e, 'off') for e in self.entities}, entities={})
        tracker = SQLiteTemporalTracker(type('S', (), dict(path=str(self.destination)))(), self.entities, context, lo, hi)
        try:
            from datetime import datetime, timezone
            with patch('context.iso_from_ts', side_effect=lambda ts: datetime.fromtimestamp(float(ts),timezone.utc).isoformat()):
                tracker.advance(hi)
            self.assertTrue(tracker.state_map)
        finally:
            tracker.close()

    def test_C_three_fragments_and_no_seed_across_gap(self):
        gaps = [dict(entity_id=self.entities[1], start=10000., end=12000.),
                dict(entity_id=self.entities[1], start=30000., end=32000.)]
        report = self.recover(http_gaps=gaps)
        segments = safe_segments(report, 900., 77000.)
        self.assertEqual(len(segments), 3)
        lo, hi = segments[1]
        project_segment(self.destination, lo, hi)
        with closing(sqlite3.connect(self.destination)) as conn:
            self.assertIsNone(conn.execute('SELECT id FROM entity_history WHERE ts<?', (lo,)).fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM entity_history').fetchone()[0],
                             conn.execute('SELECT COUNT(DISTINCT id) FROM entity_history').fetchone()[0])

    def test_E_combined_http_and_physical_damage(self):
        self.damage(self.pages[3][0])
        report = self.recover(http_gaps=[dict(entity_id=self.entities[0], start=20000., end=21000.)])
        self.assertEqual({g['failure_reason'] for g in report['gaps']}, {'HISTORY_GAP_SQLITE_CORRUPT','HISTORY_GAP_HTTP500'})

    def test_F_cancellation_does_not_modify_source_and_retry_uses_new_generation(self):
        before = self.source.read_bytes()
        with self.assertRaises(InterruptedError):
            self.recover(cancelled=lambda: True)
        self.assertEqual(before, self.source.read_bytes())
        self.destination = self.root/'retry.db'
        self.assertEqual(self.recover()['status'], 'READY')

    def test_G_unknown_time_loss_excludes_entire_window(self):
        segments = safe_segments(dict(gaps=[dict(start_ts=900, end_ts=77000, failure_reason='HISTORY_UNKNOWN')]),900,77000)
        self.assertEqual(segments, [])

    def test_H_wal_snapshot_does_not_leak_concurrent_commits(self):
        inserted = []
        def progress(**fields):
            if not inserted and fields.get('preparation',{}).get('table_name')=='entity_history':
                inserted.append(True)
                self.store.archive_batch([('switch.target', 76500.,'on',{},None,'ha_history_full')])
        report = self.recover(progress=progress)
        self.assertEqual(report['status'], 'READY')
        # The callback runs after the durable prefix boundary was captured.
        self.store.archive_batch([('switch.target', 76501.,'off',{},None,'ha_history_full')])
        with closing(sqlite3.connect(self.destination)) as conn:
            self.assertIsNone(conn.execute('SELECT id FROM entity_history WHERE ts=76500').fetchone())
            self.assertIsNone(conn.execute('SELECT id FROM entity_history WHERE ts=76501').fetchone())

    def test_I_correct_metadata_is_preserved_exactly(self):
        self.store.add_feedback('test',1,1.,-1.,'Correct',{0:1.},user_id='resident',source='manual_feedback')
        with self.store.conn() as conn:
            original_correct = [tuple(row) for row in conn.execute('SELECT * FROM rl_feedback')]
        with self.store.conn() as conn:
            conn.execute("CREATE TABLE correct_fixture(id INTEGER PRIMARY KEY, payload TEXT)")
            conn.execute("INSERT INTO correct_fixture VALUES(1,?)", ('{"label":"user-correct","received_time":1234}',))
        self.recover()
        with closing(sqlite3.connect(self.destination)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM rl_feedback').fetchall(), original_correct)
            # Unknown extension tables retain schema; real Correct dependencies
            # above retain exact rows. The original extension is never modified.
            self.assertEqual(conn.execute('SELECT * FROM correct_fixture').fetchall(), [])
        with self.store.conn() as conn:
            self.assertEqual(conn.execute('SELECT payload FROM correct_fixture').fetchone()[0],'{"label":"user-correct","received_time":1234}')

    def _trained_generation(self):
        agent = self.store.create_agent(support.agent(target_entity=self.entities[0]))
        self.store.add_historical_experience(agent['id'],1,0,0.,1.,30.,{0:1.})
        self.store.save_model(agent['id'],dict(version=11,marker='previous-approved'))
        before = self.store.get_model(agent['id'])
        self.recover()
        with patch.dict('os.environ', ADAPTIVE_AI_TRAINING_WORKER='1'):
            staged = Store(self.destination)
        staged.add_historical_experience(agent['id'],4000,1,1.,1.,30.,{0:1.})
        staged.save_model(agent['id'],dict(version=11,marker='trained'))
        staged.set_training_state(agent['id'],'paused',samples=20,shadow_after_completion=True)
        with staged.conn() as conn:
            manifest = json.loads(conn.execute('SELECT payload FROM recovery_manifest').fetchone()[0])
            manifest['status'] = 'TRAINED'
            conn.execute('UPDATE recovery_manifest SET payload=?',(json.dumps(manifest),))
        return agent,before

    def test_publication_is_atomic_when_lifecycle_update_fails(self):
        agent,before = self._trained_generation()
        with self.store.conn() as conn:
            conn.execute("CREATE TRIGGER fail_publication BEFORE UPDATE ON agents BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            publish_generation(self.store,self.destination,agent['id'],agent_config_fingerprint(agent),agent_config_fingerprint)
        self.assertEqual(self.store.get_model(agent['id']),before)
        self.assertEqual([r['target_history_id'] for r in self.store.list_historical_experiences(agent['id'])],[1])
        with self.store.conn() as conn:
            conn.execute('DROP TRIGGER fail_publication')
        publish_generation(self.store,self.destination,agent['id'],agent_config_fingerprint(agent),agent_config_fingerprint)
        self.assertEqual(self.store.get_model(agent['id'])['marker'],'trained')
        self.assertEqual(self.store.get_agent_config(agent['id'])['mode'],'shadow')
        with self.store.conn() as conn:
            self.assertEqual(json.loads(conn.execute('SELECT model_json FROM training_generation_backup').fetchone()[0]),before)

    def test_new_correct_during_training_rejects_publication_without_losing_label(self):
        agent,before = self._trained_generation()
        self.store.add_feedback(agent['id'],1,1.,-1.,'Correct',{0:1.},user_id='resident',source='manual_feedback')
        with self.assertRaisesRegex(RecoveryUnavailable,'Correct changed'):
            publish_generation(self.store,self.destination,agent['id'],agent_config_fingerprint(agent),agent_config_fingerprint)
        self.assertEqual(self.store.get_model(agent['id']),before)
        self.assertEqual(len(self.store.list_feedback(agent['id'])),1)

    def test_quarantine_cache_reuses_diagnosis_and_invalidates_after_source_change(self):
        self.damage(self.pages[3][0])
        first = self.recover()
        self.destination = self.root/'second.db'
        second = self.recover()
        self.assertTrue(second['quarantine_cache_hit'])
        self.assertLess(second['attempts'],first['attempts'])
        with self.store.conn() as conn:
            conn.execute("INSERT INTO app_meta VALUES('cache_invalidation','changed')")
        self.destination = self.root/'third.db'
        self.assertFalse(self.recover()['quarantine_cache_hit'])

    def test_explicit_agent_does_not_require_unrelated_diagnostic_history(self):
        agent = self.store.create_agent(support.agent(target_entity=self.entities[0],input_entities=[self.entities[1]]))
        states = {e:support.state(e,'off') for e in self.entities}
        states['sensor.unrelated_diagnostic'] = support.state('sensor.unrelated_diagnostic','123')
        scope = training_history_scope(dict(agent_id=agent['id'],state_map=states,entity_registry={}),self.store)
        self.assertEqual(scope,set(self.entities))


if __name__ == '__main__':
    unittest.main()
