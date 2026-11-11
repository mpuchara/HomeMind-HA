import itertools
import json
import sqlite3
import unittest
from unittest.mock import patch

import support
from history_recovery import prepare_generation, RecoveryUnavailable, project_segment, safe_segments
import test_history_recovery_0156 as recovery_fixture


class SelectivePreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=recovery_fixture.RealRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store=self.fixture.store
        self.agent=self.store.create_agent(support.agent(target_entity='switch.target',input_entities=['binary_sensor.motion']))
        self.other=self.store.create_agent(support.agent(id='other',target_entity='switch.other',input_entities=['sensor.unrelated']))

    def prepare(self, **kwargs):
        return prepare_generation(self.store.path,self.fixture.destination,self.fixture.entities,900,77000,
                                  agent_id=self.agent['id'],**kwargs)

    def test_agent_models_experiences_correct_and_events_are_scoped(self):
        self.store.save_model(self.agent['id'],dict(marker='selected'))
        self.store.save_model(self.other['id'],dict(marker='other'))
        for agent in (self.agent,self.other):
            self.store.add_feedback(agent['id'],0,0.,-1.,'Correct',{0:1.},source='manual_feedback')
            self.store.add_historical_experience(agent['id'],1,0,0.,1.,30.,{0:1.})
        with self.store.conn() as conn:
            conn.execute("INSERT INTO events(created_at,level,kind,message) VALUES('fixture','info','unrelated','do not copy')")
        report=self.prepare()
        with sqlite3.connect(self.fixture.destination) as conn:
            self.assertEqual(conn.execute('SELECT agent_id FROM rl_models').fetchall(),[(self.agent['id'],)])
            self.assertEqual(conn.execute('SELECT agent_id FROM rl_feedback').fetchall(),[(self.agent['id'],)])
            self.assertEqual(conn.execute('SELECT agent_id FROM historical_experiences').fetchall(),[(self.agent['id'],)])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT type FROM sqlite_master WHERE name='recovery_rows'").fetchone()[0],'view')
        self.assertEqual(report['copy_tables']['events']['source_rows_scanned'],0)

    def test_normal_copy_does_not_consume_corruption_budget_or_fake_complete_progress(self):
        stages=[]
        with patch('training_generation.time.monotonic',side_effect=itertools.count(1.,10.)):
            report=self.prepare(options=dict(max_recovery_wall_seconds=1),progress=lambda **kw:stages.append(kw))
        self.assertGreater(report['recovery_wall_seconds'],120.)
        self.assertEqual(report['attempts'],0)
        self.assertEqual(report['recovery_active_cpu_seconds'],0.)
        self.assertTrue(all(0.<s['progress']<=.09 for s in stages))
        self.assertIn('table_name',stages[0]['preparation'])

    def test_completed_copy_tables_resume_without_recopying_after_cancellation(self):
        for i in range(300):
            self.store.add_feedback(self.agent['id'],0,0.,-1.,'Correct',{0:1.},source='manual_feedback')
        stopped=[]
        def progress(**fields):
            if fields.get('preparation',{}).get('table_name')=='rl_feedback' and fields['preparation'].get('rows_copied')==300:
                stopped.append(True)
        with self.assertRaises(InterruptedError):
            self.prepare(progress=progress,cancelled=lambda:bool(stopped))
        report=self.prepare()
        self.assertTrue(report['resumed'])
        self.assertEqual(report['copy_tables']['rl_feedback']['source_rows_scanned'],300)
        self.assertEqual(report['copy_tables']['rl_feedback']['rows_copied'],300)

    def test_real_corruption_attempt_exhaustion_retains_source_model(self):
        self.store.save_model(self.agent['id'],dict(marker='approved'))
        before=self.store.get_model(self.agent['id'])
        self.fixture.damage(self.fixture.pages[3][0])
        with self.assertRaisesRegex(RecoveryUnavailable,'attempt budget exhausted'):
            self.prepare(options=dict(max_recovery_attempts=1))
        self.assertEqual(self.store.get_model(self.agent['id']),before)
        self.assertFalse(self.fixture.destination.exists())

    def test_partial_generation_projects_views_without_copying_history_again(self):
        self.fixture.damage(self.fixture.pages[3][0])
        report=self.prepare()
        with sqlite3.connect(self.fixture.destination) as conn:
            before=conn.execute('SELECT COUNT(*) FROM recovery_rows').fetchone()[0]
            self.assertEqual(conn.execute("SELECT type FROM sqlite_master WHERE name='entity_history'").fetchone()[0],'view')
        for lo,hi in safe_segments(report,900,77000)[:2]:
            project_segment(self.fixture.destination,lo,hi)
        with sqlite3.connect(self.fixture.destination) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM recovery_rows').fetchone()[0],before)

    def test_correct_dependency_tables_keep_only_selected_family(self):
        with self.store.conn() as conn:
            conn.executescript('''CREATE TABLE tiny_mlp_correct_batches(batch_id TEXT PRIMARY KEY,parent_agent_id TEXT,root_agent_id TEXT);
                CREATE TABLE tiny_mlp_correct_samples(batch_id TEXT,label_id INTEGER);
                CREATE TABLE manual_feedback_journal(feedback_id TEXT PRIMARY KEY,agent_id TEXT,root_agent_id TEXT);
                CREATE TABLE manual_feedback_effects(feedback_id TEXT,payload TEXT);
                CREATE TABLE agent_correct_operations(operation_id TEXT,root_agent_id TEXT,parent_agent_id TEXT);''')
            for index,agent in enumerate((self.agent,self.other)):
                conn.execute('INSERT INTO tiny_mlp_correct_batches VALUES(?,?,?)',(str(index),agent['id'],agent['id']))
                conn.execute('INSERT INTO tiny_mlp_correct_samples VALUES(?,1)',(str(index),))
                conn.execute('INSERT INTO manual_feedback_journal VALUES(?,?,?)',(str(index),agent['id'],agent['id']))
                conn.execute('INSERT INTO manual_feedback_effects VALUES(?,?)',(str(index),'trusted'))
                conn.execute('INSERT INTO agent_correct_operations VALUES(?,?,?)',(str(index),agent['id'],agent['id']))
        self.prepare()
        with sqlite3.connect(self.fixture.destination) as conn:
            self.assertEqual(conn.execute('SELECT * FROM tiny_mlp_correct_samples').fetchall(),[('0',1)])
            self.assertEqual(conn.execute('SELECT * FROM manual_feedback_effects').fetchall(),[('0','trusted')])


if __name__=='__main__':
    unittest.main()
