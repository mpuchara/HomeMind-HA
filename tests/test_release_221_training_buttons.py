"""Embedded-panel actions and paused durable training after process loss."""
import os
import subprocess
import sys
import unittest

from support import ROOT


class TrainingButtons221Tests(unittest.TestCase):
    def test_actions_work_without_native_confirmation_or_alert(self):
        r=subprocess.run(['node','tools/verify_training_actions_ui.js'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertIn('"destructive_confirmation":true',r.stdout)

    def test_interrupted_database_reopens_and_accepts_resume_and_rebuild(self):
        script=r'''
import json,tempfile,threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import support,history,queue_main
from storage import Store
from training_queue import TrainingQueue
from runtime_http import ExplicitRouteRegistry
from test_training_queue import FakeHistory,FakeEngine

class DurableHistoryStub(FakeHistory):
    def _start(self,aid,rebuild):
        with self.agent_jobs_lock:
            if self.agent_jobs:return False
            self.agent_jobs.add(aid)
        self.started.append((aid,rebuild))
        current=self.store.get_agent_config(aid)
        self.store.set_training_state(aid,'training',score=current['benchmark_score'],samples=current['benchmark_samples'])
        return True
    def complete(self,aid):
        self.agent_jobs.discard(aid)
        self.store.set_training_state(aid,'qualified')

class Http:
    def __init__(self,path):self.path=path;self.result=None
    def require_trusted_client(self):return True
    def require_runtime(self):return True
    def send_json(self,status,payload):self.result=(status,payload);return self.result

with tempfile.TemporaryDirectory() as folder:
    for rebuild in (False,True):
        path=Path(folder)/('rebuild.db' if rebuild else 'resume.db')
        first=Store(path)
        aid=first.create_agent(support.agent(mode='paused'))['id']
        model={'version':11,'schema':{'version':12,'feature_contract_version':4,'entities':['binary_sensor.room']},'saved_weights':[1,2,3]}
        first.save_model(aid,model)
        first.set_training_state(aid,'training',score=.91,samples=1259,source='fixture')
        first.set_training_progress(aid,100,160,200)
        before=first.get_agent_config(aid)
        saved=first.get_model(aid)
        # Fresh connection/manager simulates restarting after the last durable checkpoint.
        store=Store(path)
        engine=SimpleNamespace(lock=threading.RLock(),state_map={},entity_registry={})
        with patch.object(history,'STORE',store),patch.dict(history.OPTIONS,manual_agent_training=True):
            manager=history.HistoryManager(engine)
        resumed=store.get_agent_config(aid)
        assert resumed['training_state']=='paused' and resumed['mode']=='paused',resumed
        for key in ('training_cursor_ts','training_window_start_ts','training_window_end_ts','training_progress','benchmark_score','benchmark_samples'):
            assert resumed[key]==before[key],key
        assert store.get_model(aid)==saved
        trace=[];worker=DurableHistoryStub(store,trace);queue=TrainingQueue(worker,store,FakeEngine(trace))
        queue_main.core.STORE=store;queue_main.core.HISTORY=manager;queue_main.TRAINING_QUEUE=queue
        registry=ExplicitRouteRegistry();queue_main.register_mutation_routes(registry)
        http=Http('/api/agents/'+aid+('/learning' if rebuild else '/resume'))
        assert registry.dispatch('DELETE' if rebuild else 'POST',http)
        assert http.result[0]==202,http.result
        job=queue.status_for(aid)
        assert job['state']=='queued' and job['rebuild'] is rebuild,job
        assert store.get_model(aid)==saved
        assert store.get_agent_config(aid)['training_cursor_ts']==160
        assert queue._try_start_head()
        assert worker.started==[(aid,rebuild)],worker.started
        worker.complete(aid);queue._finish_active_if_done()
        assert queue.snapshot()['queued_count']==0
print('paused checkpoint preserved; Resume and explicit Rebuild admitted after restart')
'''
        env=dict(os.environ,PYTHONPATH=str(ROOT/'tests'))
        r=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)


if __name__=='__main__':unittest.main()
