"""Generation quota and archive-page regressions (no HA connection)."""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import support
from training_generation import MIB, generation_capacity, generation_pressure, Preparation


class TrainingCapacityTests(unittest.TestCase):
    def test_normal_capacity_is_not_512_mib(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'archive.db'
            working = Path(tmp)/'scratch.db'
            guard = SimpleNamespace(reserve=2*1024*MIB, db_limit=8*1024*MIB)
            with patch('training_generation.scratch_bytes', side_effect=lambda p: 900*MIB if p==source else 0), \
                 patch('training_generation.shutil.disk_usage', return_value=SimpleNamespace(free=10*1024*MIB)):
                self.assertEqual(generation_capacity(source,working,guard),1864*MIB)

    def test_reserve_and_existing_scratch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'archive.db'
            working = Path(tmp)/'scratch.db'
            guard = SimpleNamespace(reserve=2048*MIB, db_limit=8192*MIB)
            with patch('training_generation.scratch_bytes', side_effect=lambda p: 900*MIB if p==source else 300*MIB), \
                 patch('training_generation.shutil.disk_usage', return_value=SimpleNamespace(free=guard.reserve+200*MIB)):
                self.assertEqual(generation_capacity(source,working,guard),400*MIB)
            error = generation_pressure('sqlite_full',working,512*MIB,guard.reserve)
            self.assertEqual(error.reason,'sqlite_full')
            self.assertIn('generation_limit=536870912',str(error))

    def test_history_uses_larger_pages_preserving_keyset_and_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sqlite3.connect(':memory:') as src, sqlite3.connect(':memory:') as dst:
                src.row_factory = sqlite3.Row
                for conn in (src,dst):
                    conn.execute('CREATE TABLE entity_history(id INTEGER PRIMARY KEY,ts REAL,attributes_json TEXT)')
                dst.execute('CREATE TABLE __training_copy_state(id INTEGER PRIMARY KEY,payload TEXT)')
                rows=[(i,float(i//2),'{}') for i in range(1,1201)]
                src.executemany('INSERT INTO entity_history VALUES(?,?,?)',rows)
                p=Preparation(Path(tmp)/'source.db',Path(tmp)/'dest.db',[],0,1000,{},lambda:False,lambda **kw:None,'a')
                p.src,p.dst,p.schema=src,dst,[]
                p.checkpoint=lambda:None
                calls=[]
                original=p.sql
                def tracked(conn, method, *args):
                    if conn is src and method=='execute':
                        calls.append(args[0])
                    return original(conn,method,*args)
                p.sql=tracked
                p.copy('entity_history',history=True,key='ts,id')
                self.assertEqual(dst.execute('SELECT * FROM entity_history ORDER BY id').fetchall(),rows)
                self.assertEqual(len(calls),4)
                self.assertIn('LIMIT 512',calls[0])
                self.assertTrue(p.state['queries']['entity_history']['done'])

    def test_large_payload_reduces_page_without_skipping_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sqlite3.connect(':memory:') as src, sqlite3.connect(':memory:') as dst:
                src.row_factory=sqlite3.Row
                for conn in (src,dst):
                    conn.execute('CREATE TABLE entity_history(id INTEGER PRIMARY KEY,ts REAL,attributes_json TEXT)')
                dst.execute('CREATE TABLE __training_copy_state(id INTEGER PRIMARY KEY,payload TEXT)')
                rows=[(i,float(i),'{"v":"'+'x'*5000+'"}') for i in range(1,301)]
                src.executemany('INSERT INTO entity_history VALUES(?,?,?)',rows)
                p=Preparation(Path(tmp)/'source.db',Path(tmp)/'dest.db',[],0,1000,{'max_recovery_memory_mb':1},lambda:False,lambda **kw:None,'a')
                p.src,p.dst,p.schema=src,dst,[]
                p.checkpoint=lambda:None
                p.copy('entity_history',history=True,key='ts,id')
                self.assertEqual(dst.execute('SELECT * FROM entity_history ORDER BY id').fetchall(),rows)
                self.assertEqual(p.state['tables']['entity_history']['source_rows_scanned'],300)

if __name__=='__main__':
    unittest.main()
