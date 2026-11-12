"""Real SQLite pagination regressions; bounded callbacks fail old SQL promptly."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import support
from training_generation import Preparation, TrainingPreparationNoProgress, CONTRACT


class PaginationTests(unittest.TestCase):
    def make(self, table, rows, upper=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = sqlite3.connect(':memory:')
        dst = sqlite3.connect(':memory:')
        self.addCleanup(src.close)
        self.addCleanup(dst.close)
        src.row_factory = sqlite3.Row
        schema = 'CREATE TABLE '+table+'(ts REAL PRIMARY KEY,payload TEXT)'
        src.execute(schema)
        dst.execute(schema)
        dst.execute('CREATE TABLE __training_copy_state(id INTEGER PRIMARY KEY,payload TEXT)')
        src.executemany('INSERT INTO '+table+' VALUES(?,?)', rows)
        p = Preparation(Path(tmp.name)/'source.db', Path(tmp.name)/'job.db', [], 2000000, 2001000, {}, lambda:False, lambda **kw:None, 'agent')
        p.src, p.dst, p.schema = src, dst, []
        p.state['boundary'] = {'tables':{table:{'upper':len(rows) if upper is None else upper}}}
        calls = []
        def bounded():
            calls.append(True)
            if len(calls)>20:
                raise AssertionError('pagination did not terminate within 20 pages')
        p.checkpoint = bounded
        return p

    def test_window_seed_multibatch_and_boundary_both_tables(self):
        for table in ('home_checkpoints','adaptive_presence_checkpoints'):
            for count in (5,385):
                for seed in (False,True):
                    with self.subTest(table=table,count=count,seed=seed):
                        rows = ([(10.,'old'),(20.,'seed')] if seed else [])+[(2000000.+i,str(i)) for i in range(count)]
                        upper = len(rows)
                        # A late append has a timestamp inside the allowed window.
                        p = self.make(table, rows+[(2000999.,'late')],upper)
                        plan = p.table_plan({'name':table})
                        p.copy(table,plan[0],plan[1],strategy=plan[2])
                        expected = rows[1:] if seed else rows
                        self.assertEqual(p.dst.execute('SELECT * FROM '+table+' ORDER BY ts').fetchall(),expected)
                        metric = p.state['tables'][table]
                        self.assertEqual(metric['source_rows_scanned'],len(expected))
                        self.assertEqual(metric['rows_copied'],len(expected))
                        self.assertEqual(metric['duplicate_rows'],0)
                        self.assertTrue(p.state['queries'][table]['done'])

    def test_no_progress_precedes_any_write_or_checkpoint(self):
        for key, previous in (('rowid',5),('ts,id',[2000000.,1]),('event_time,rowid',[2000000.,1])):
            with self.subTest(key=key):
                p=self.make('home_checkpoints',[(2000000.,'one')])
                p.src.execute('ALTER TABLE home_checkpoints ADD COLUMN id INTEGER DEFAULT 1')
                p.src.execute('ALTER TABLE home_checkpoints ADD COLUMN event_time REAL DEFAULT 2000000')
                p.state['queries']['home_checkpoints']={'last':previous}
                original=p.sql
                def broken(conn,method,*args):
                    if conn is p.src and method=='execute':
                        return p.src.execute('SELECT rowid AS __copy_rowid,* FROM home_checkpoints')
                    return original(conn,method,*args)
                p.sql=broken
                before=p.dst.total_changes
                with self.assertRaisesRegex(TrainingPreparationNoProgress,'previous_cursor'):
                    p.copy('home_checkpoints',key=key)
                self.assertEqual(p.dst.total_changes,before)
                self.assertEqual(p.state['queries']['home_checkpoints'],{'last':previous})

    def test_legal_deduplication_advances(self):
        p=self.make('home_checkpoints',[(2000000.+i,str(i)) for i in range(300)])
        p.dst.executemany('INSERT INTO home_checkpoints VALUES(?,?)',[(2000000.+i,str(i)) for i in range(300)])
        p.dst.commit()
        p.copy('home_checkpoints')
        self.assertEqual(p.state['tables']['home_checkpoints']['rows_copied'],0)
        self.assertEqual(p.state['tables']['home_checkpoints']['duplicate_rows'],300)
        self.assertTrue(p.state['queries']['home_checkpoints']['done'])

    def test_checkpoint_rows_and_cursor_rollback_together(self):
        p=self.make('home_checkpoints',[(2000000.+i,str(i)) for i in range(1200)])
        p.save()
        calls=[]
        def cancel():
            calls.append(True)
            if len(calls)==4:
                raise InterruptedError('cancel')
        p.checkpoint=cancel
        with self.assertRaises(InterruptedError):
            p.copy('home_checkpoints')
        p.dst.rollback()
        state=json.loads(p.dst.execute('SELECT payload FROM __training_copy_state').fetchone()[0])
        self.assertEqual(p.dst.execute('SELECT COUNT(*) FROM home_checkpoints').fetchone()[0],0)
        self.assertEqual(state['queries'],{})

    def test_seed_subquery_ignores_late_append_before_window(self):
        p=self.make('home_checkpoints',[(10.,'seed'),(800000.,'late')],upper=1)
        plan=p.table_plan({'name':'home_checkpoints'})
        p.copy('home_checkpoints',plan[0],plan[1])
        self.assertEqual(p.dst.execute('SELECT * FROM home_checkpoints').fetchall(),[(10.,'seed')])

    def test_upgrade_rebuilds_scratch_preserves_archive_cache_model_and_correct(self):
        import training_generation
        from test_training_generation_0157 import SelectivePreparationTests
        fixture=SelectivePreparationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.store.save_model(fixture.agent['id'],{'marker':'approved'})
        fixture.store.add_feedback(fixture.agent['id'],0,0.,-1.,'Correct',{0:1.},source='manual_feedback')
        fixture.store.meta_set('training_recorder_coverage_v1','{"contract":"training_recorder_coverage_v1","rows":[]}')
        def frozen():
            with fixture.store.conn() as c:
                return {name:[tuple(r) for r in c.execute('SELECT * FROM '+name)]
                        for name in ('entity_history','rl_models','rl_feedback','app_meta','agents')}
        before=frozen()
        stopped=[]
        def progress(**kw):
            if kw.get('preparation',{}).get('table_name')=='entity_history':
                stopped.append(True)
        with patch.object(training_generation,'CONTRACT','training-history-generation-v5'):
            with self.assertRaises(InterruptedError):
                fixture.prepare(progress=progress,cancelled=lambda:bool(stopped))
        with patch('ha.HA.history',side_effect=AssertionError('upgrade must not fetch Recorder')):
            report=fixture.prepare()
        self.assertEqual(report['contract'],'training-history-generation-v6')
        self.assertFalse(report['resumed'])
        self.assertEqual(frozen(),before)

    def test_deferred_cleanup_requires_verified_dead_owner(self):
        from training_process import _retire_legacy_preparation, _generation_path
        import hashlib
        p=self.make('home_checkpoints',[])
        job={'agent_id':'agent','result_path':str(p.destination.parent/'result.json'),
             'status_path':str(p.destination.parent/'status.json')}
        Path(job['status_path']).write_text(json.dumps({'worker_pid':99999999}),encoding='utf-8')
        key=hashlib.sha256((str(p.source)+'|agent|training-history-generation-v5').encode()).hexdigest()[:24]
        path=_generation_path(job).parent/('.preparing-'+key+'.db')
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE __training_copy_state(id INTEGER PRIMARY KEY,payload TEXT)')
            c.execute('INSERT INTO __training_copy_state VALUES(1,?)',(json.dumps({'identity':dict(source=str(p.source),agent_id='agent',contract='training-history-generation-v5')}),))
        c.close()
        item={'source':str(p.source),'job':job}
        with patch('training_process._same_process_alive',return_value=True):
            self.assertFalse(_retire_legacy_preparation(item))
            self.assertTrue(path.exists())
        with patch('training_process._same_process_alive',return_value=False):
            self.assertTrue(_retire_legacy_preparation(item))
            self.assertFalse(path.exists())

    def test_contract_invalidates_0158_preparation_only(self):
        self.assertEqual(CONTRACT,'training-history-generation-v6')
        p=self.make('home_checkpoints',[])
        import hashlib
        legacy=hashlib.sha256((str(p.source)+'|agent|training-history-generation-v5').encode()).hexdigest()[:24]
        self.assertNotEqual(p.working.name,'.preparing-'+legacy+'.db')


if __name__=='__main__':
    unittest.main()
