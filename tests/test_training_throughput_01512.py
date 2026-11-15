"""Real scoped fence and private WAL regression tests."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import support
from training_snapshot import install,capture,validate,protect_training_inputs,retention_protected
from training_generation import Preparation


class TrainingThroughputTests(unittest.TestCase):
    def test_scope_ignores_other_entity_and_noop_but_detects_real_mutation(self):
        with closing(sqlite3.connect(':memory:')) as c:
            c.execute('CREATE TABLE entity_history(id INTEGER PRIMARY KEY AUTOINCREMENT,entity_id TEXT,ts REAL,state TEXT,mutation_revision INTEGER)')
            c.executemany('INSERT INTO entity_history(entity_id,ts,state,mutation_revision) VALUES(?,?,?,?)',
                          [('a',1.,'on',1),('b',1.,'on',1),('a',20.,'on',1)])
            install(c)
            fence=capture(c,10.,['a'])
            c.execute("UPDATE entity_history SET state='off' WHERE entity_id='b'")
            c.execute("UPDATE entity_history SET mutation_revision=2 WHERE id=1")
            c.execute("UPDATE entity_history SET state=state WHERE id=1")
            c.execute("UPDATE entity_history SET state='off' WHERE id=3")
            c.execute("INSERT INTO entity_history(entity_id,ts,state,mutation_revision) VALUES('a',2,'on',3)")
            validate(c,fence)
            c.execute("UPDATE entity_history SET ts=3 WHERE id=3")
            with self.assertRaisesRegex(RuntimeError,'entity=a'):
                validate(c,fence)

    def test_delete_and_entity_move_into_scope_are_detected(self):
        with closing(sqlite3.connect(':memory:')) as c:
            c.execute('CREATE TABLE entity_history(id INTEGER PRIMARY KEY AUTOINCREMENT,entity_id TEXT,ts REAL,state TEXT)')
            c.executemany('INSERT INTO entity_history(entity_id,ts,state) VALUES(?,?,?)',[('a',1.,'on'),('b',2.,'on')])
            install(c)
            fence=capture(c,10.,['a'])
            c.execute("UPDATE entity_history SET entity_id='a' WHERE id=2")
            with self.assertRaisesRegex(RuntimeError,'entity=a'):
                validate(c,fence)
            fence=capture(c,10.,['a'])
            c.execute("DELETE FROM entity_history WHERE id=1")
            with self.assertRaisesRegex(RuntimeError,'entity=a'):
                validate(c,fence)

    def test_nested_protection_releases_on_error(self):
        store=SimpleNamespace(lock=threading.RLock())
        with self.assertRaises(ValueError):
            with protect_training_inputs(store):
                with protect_training_inputs(store):
                    self.assertTrue(retention_protected(store,'entity_history'))
                    self.assertFalse(retention_protected(store,'events'))
                self.assertTrue(retention_protected(store,'entity_history'))
                raise ValueError('worker failed')
        self.assertFalse(retention_protected(store,'entity_history'))

    def test_sealed_scratch_has_all_rows_without_wal_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            p=Preparation(root/'source.db',root/'dest.db',[],0,1,{},lambda:False,lambda **kw:None,'a')
            with closing(sqlite3.connect(':memory:')) as src, closing(sqlite3.connect(p.working)) as dst:
                p.src,p.dst,p.schema=src,dst,[]
                dst.execute('PRAGMA journal_mode=WAL')
                dst.execute('PRAGMA synchronous=NORMAL')
                dst.execute('CREATE TABLE data(id INTEGER PRIMARY KEY,value TEXT)')
                dst.executemany('INSERT INTO data VALUES(?,?)',[(i,str(i)) for i in range(10000)])
                dst.commit()
                p.finish_scratch()
                self.assertEqual(dst.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            self.assertFalse(Path(str(p.working)+'-wal').exists())
            p.working.rename(p.destination)
            with closing(sqlite3.connect(p.destination)) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM data').fetchone()[0],10000)
                self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')

if __name__=='__main__':
    unittest.main()
