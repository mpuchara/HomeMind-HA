"""A long index build is not a stalled history keyset."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
import support
from training_generation import Preparation
from training_process import preparation_progress_token


class FinalizationTests(unittest.TestCase):
    def test_watchdog_token_only_accepts_vm_progress_for_finalization(self):
        copy=dict(query_key='history:weather.dom',current_cursor=[1.,16],copy_strategy='entity_time_keyset')
        self.assertEqual(preparation_progress_token(copy),
                         preparation_progress_token(dict(copy,operation_vm_steps=10000)))
        operation=dict(copy_strategy='generation_finalization',operation='Build index x',operation_vm_steps=10000)
        self.assertNotEqual(preparation_progress_token(operation),
                            preparation_progress_token(dict(operation,operation_vm_steps=20000)))
        self.assertNotEqual(preparation_progress_token(operation),
                            preparation_progress_token(dict(operation,operation='Validate generation')))

    def test_real_sqlite_index_and_integrity_progress_has_own_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            reports=[]
            p=Preparation(root/'source.db',root/'dest.db',[],0,1,{},lambda:False,
                          lambda **kw: reports.append(kw['preparation']), 'a')
            with sqlite3.connect(':memory:') as src, sqlite3.connect(':memory:') as dst:
                p.src,p.dst,p.schema=src,dst,[]
                p.state['tables']['entity_history']=dict(query_key='history:weather.dom',
                    current_cursor=[1.,16],rows_copied=20000)
                dst.execute('CREATE TABLE history(id INTEGER PRIMARY KEY,ts REAL)')
                dst.executemany('INSERT INTO history VALUES(?,?)',((i,float(i%123)) for i in range(20000)))
                dst.commit()
                p.begin_operation('Build index history_ts')
                def progress():
                    p.operation_step(100)
                    return 0
                dst.set_progress_handler(progress,100)
                dst.execute('CREATE INDEX history_ts ON history(ts)')
                p.emit('entity_history','generation_finalization',True)
                self.assertGreater(p.operation_vm_steps,0)
                self.assertIsNone(reports[-1]['current_cursor'])
                self.assertEqual(reports[-1]['stage'],'Finalizing generation')
                self.assertNotEqual(preparation_progress_token(reports[0]),preparation_progress_token(reports[-1]))
                p.end_operation()
                p.begin_operation('Validate generation (quick_check)')
                self.assertEqual(dst.execute('PRAGMA quick_check').fetchone()[0],'ok')
                p.emit('entity_history','generation_finalization',True)
                self.assertGreater(reports[-1]['operation_vm_steps'],0)
                self.assertEqual(reports[-1]['operation'],'Validate generation (quick_check)')

if __name__=='__main__':
    unittest.main()
