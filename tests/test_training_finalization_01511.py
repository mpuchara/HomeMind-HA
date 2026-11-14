"""A long index build is not a stalled history keyset."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import json
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

    def test_legacy_descriptor_is_adopted_only_with_matching_frozen_prefix(self):
        import hashlib
        import training_process as process
        from training_generation import CONTRACT
        from training_snapshot import install, capture
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'archive.db'
            with sqlite3.connect(source) as conn:
                conn.execute('CREATE TABLE rl_models(agent_id TEXT PRIMARY KEY,model_json TEXT)')
                conn.execute('CREATE TABLE entity_history(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL)')
                install(conn)
                boundary=capture(conn,100.)
                conn.commit()
            agent={'id':'a','target_entity':'light.test'}
            store=SimpleNamespace(path=str(source),get_agent_config=lambda aid:agent)
            job_id='aee31b36c0a74b89'
            job=dict(job_id=job_id,agent_id='a',app_version='0.15.10',
                     training_revision=process.TRAINING_REVISION,training_generation=True,
                     generation_rebuild=True,sequence_chunks=[{}],sequence_start_ts=0.,
                     sequence_target_end_ts=100.,agent_fingerprint=process.agent_config_fingerprint(agent),
                     options_fingerprint=process.training_options_fingerprint(dict(process.OPTIONS)),
                     status_path=str(root/(job_id+'.status.json')),result_path=str(root/(job_id+'.result.json')),
                     log_path=str(root/(job_id+'.log')))
            job['checksum']=process.descriptor_checksum(job)
            (root/(job_id+'.job.json')).write_text(json.dumps(job))
            (root/(job_id+'.status.json')).write_text(json.dumps(dict(worker_pid=99999999,training_preparation_active=True)))
            key=hashlib.sha256((str(source.resolve())+'|a|'+CONTRACT).encode()).hexdigest()[:24]
            scratch=root/'history_generations'/('.preparing-'+key+'.db')
            scratch.parent.mkdir()
            identity=dict(contract=CONTRACT,source=str(source.resolve()),entities=['light.test'],start=0.,end=100.,agent_id='a',rebuild=True)
            with sqlite3.connect(scratch) as conn:
                conn.execute('CREATE TABLE __training_copy_state(id INTEGER PRIMARY KEY,payload TEXT)')
                conn.execute('INSERT INTO __training_copy_state VALUES(1,?)',(json.dumps(dict(identity=identity,bootstrap_complete=True,boundary=boundary,model_fingerprint=None)),))
            history=SimpleNamespace(training_generation_rebuild=True)
            with patch.object(process,'_job_dir',return_value=root),patch.object(process,'training_history_scope',return_value={'light.test'}),patch.object(process,'_same_process_alive',return_value=False):
                adopted=process._recover_01510_preparation(history,'a',store)
                self.assertIsNotNone(adopted)
                self.assertEqual(adopted['job']['app_version'],process.APP_VERSION)
                self.assertEqual(adopted['job']['sequence_target_end_ts'],100.)
                with sqlite3.connect(source) as conn:
                    conn.execute("UPDATE __training_prefix_versions SET epoch=epoch+1 WHERE name='entity_history'")
                self.assertIsNone(process._recover_01510_preparation(history,'a',store))

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
