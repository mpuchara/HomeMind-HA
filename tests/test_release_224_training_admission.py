import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from support import ROOT
from storage import Store
from training_admission import TrainingAdmission, AdmissionConflict
from runtime_http import ExplicitRouteRegistry
from context_row_buffer import ContextRowBuffer


class Admission224Tests(unittest.TestCase):
    def test_deduplicated_alias_survives_lost_acknowledgement_and_completion(self):
        release=threading.Event();entered=threading.Event();calls=[]
        registry=ExplicitRouteRegistry()
        def resume(http,params):
            entered.set();release.wait(3);calls.append(params['agent_id']);http.send_json(202,{'message':'Queued'})
        registry.register('POST','fixture',r'^/api/agents/(?P<agent_id>[^/]+)/resume$',resume)
        service=TrainingAdmission(SimpleNamespace(store=object(),_admission_local=threading.local()),registry,lambda:True)
        try:
            service.submit('original-request','a','resume');self.assertTrue(entered.wait(1))
            duplicate=service.submit('duplicate-request','a','resume')
            self.assertEqual(duplicate['request_id'],'original-request')
            release.set();deadline=time.monotonic()+2
            while time.monotonic()<deadline and service.get('duplicate-request')['state']=='pending':time.sleep(.01)
            self.assertEqual(service.get('duplicate-request')['state'],'accepted')
            self.assertEqual(service.submit('duplicate-request','a','resume')['state'],'accepted')
            self.assertEqual(calls,['a'])
            with self.assertRaises(AdmissionConflict):service.submit('duplicate-request','b','resume')
        finally:release.set();service.stop();service.thread.join(2)

    def test_failure_after_queue_registration_recovers_acceptance(self):
        registry=ExplicitRouteRegistry()
        def resume(http,params):raise RuntimeError('diagnostic failed after queue append')
        registry.register('POST','fixture',r'^/api/agents/(?P<agent_id>[^/]+)/resume$',resume)
        queue=SimpleNamespace(store=object(),_admission_local=threading.local(),
                              status_for=lambda aid:{'state':'queued','position':1,'rebuild':False})
        service=TrainingAdmission(queue,registry,lambda:True)
        try:
            service.submit('request-recovered','a','resume');deadline=time.monotonic()+2
            while time.monotonic()<deadline and service.get('request-recovered')['state']=='pending':time.sleep(.01)
            self.assertEqual(service.get('request-recovered')['state'],'accepted')
            self.assertEqual(service.get('request-recovered')['response_status'],202)
        finally:service.stop();service.thread.join(2)

    def test_shipped_http_two_agent_restart_under_store_contention(self):
        result=subprocess.run([sys.executable,'tools/verify_training_admission_restart.py'],cwd=ROOT,capture_output=True,text=True,timeout=45)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report['two_workers_started'])
        self.assertTrue(report['candidate_guard_preserved'])

    def test_explicit_admission_defers_retryable_batches_without_discarding_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'priority.db')
            with store.conn() as conn:conn.execute('CREATE TABLE test_admission (a TEXT,b TEXT)')
            buffer=ContextRowBuffer(store,'fixture','INSERT INTO test_admission VALUES (?,?)')
            buffer.submit([('a','b')])
            with store.training_admission_priority():
                with store.background_sqlite(timeout_ms=0):
                    with self.assertRaises(sqlite3.OperationalError):buffer.flush()
                    self.assertEqual(buffer.snapshot()['pending'],1)
                    # Explicit durability remains allowed, inside the same scope.
                    with store.explicit_sqlite(),store.conn() as conn:conn.execute("INSERT INTO test_admission VALUES ('explicit','safe')")
            self.assertFalse(store._training_admission_pending.is_set())
            buffer.flush()
            with store.conn() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM test_admission').fetchone()[0],2)

    def test_request_ledger_is_bounded_and_never_rebinds_an_id(self):
        release=threading.Event();entered=threading.Event();calls=[]
        registry=ExplicitRouteRegistry()
        def resume(http,params):
            entered.set();release.wait(3);calls.append(params['agent_id']);http.send_json(202,{'state':'queued','message':'Queued'})
        registry.register('POST','fixture',r'^/api/agents/(?P<agent_id>[^/]+)/resume$',resume)
        queue=SimpleNamespace(store=object(),_admission_local=threading.local())
        service=TrainingAdmission(queue,registry,lambda:True,capacity=2)
        try:
            first=service.submit('request-a','a','resume');self.assertTrue(entered.wait(1))
            self.assertEqual(service.submit('request-a','a','resume')['request_id'],first['request_id'])
            with self.assertRaises(AdmissionConflict):service.submit('request-a','b','resume')
            with self.assertRaises(AdmissionConflict):service.submit('request-b','a','rebuild')
            service.submit('request-b','b','resume')
            with self.assertRaises(RuntimeError):service.submit('request-c','c','resume')
            release.set();deadline=time.monotonic()+2
            while time.monotonic()<deadline and service.get('request-b')['state']=='pending':time.sleep(.01)
            self.assertEqual(calls,['a','b'])
            # A repeated terminal id returns its saved result, never another call.
            service.records['request-a']['_monotonic']=time.monotonic()-10000
            self.assertEqual(service.submit('request-a','a','resume')['state'],'accepted')
            self.assertEqual(calls,['a','b'])
            with self.assertRaises(RuntimeError):service.submit('request-c','c','resume')
        finally:release.set();service.stop();service.thread.join(2)


if __name__=='__main__':unittest.main()
