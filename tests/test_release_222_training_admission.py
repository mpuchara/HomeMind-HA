"""Training admission in the shipped wrapper stack must not run replay/preflight."""
import os
import subprocess
import sys
import unittest
from support import ROOT


class TrainingAdmission222Tests(unittest.TestCase):
    def isolated(self, script):
        env = dict(os.environ, PYTHONPATH=str(ROOT/'tests'))
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                env=env, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_final_entrypoint_http_returns_before_slow_history_admission(self):
        self.isolated(r'''
import threading,time
import support
import trial_queue_main,queue_main
from training_queue import TrainingQueue
from runtime_http import ExplicitRouteRegistry
from test_training_queue import FakeStore,FakeEngine,FakeHistory

class Store(FakeStore):
    def get_agent_config(self,aid):return dict(self.agents[aid])
    def get_agent(self,aid):raise AssertionError('aggregate query on queue path')
class Http:
    def __init__(self,path):self.path=path;self.result=None
    def require_trusted_client(self):return True
    def require_runtime(self):return True
    def send_json(self,status,payload):self.result=(status,payload)

for rebuild in (False,True):
    store=Store();trace=[];history=FakeHistory(store,trace)
    entered=threading.Event();release=threading.Event();returned=threading.Event()
    original=history._start
    def slow_start(aid,rebuild):
        entered.set();assert release.wait(5)
        return original(aid,rebuild)
    history._start=slow_start
    queue=TrainingQueue(history,store,FakeEngine(trace),poll_seconds=.01)
    queue_main.core.STORE=store;queue_main.core.HISTORY=history;queue_main.TRAINING_QUEUE=queue
    routes=ExplicitRouteRegistry();queue_main.register_mutation_routes(routes)
    http=Http('/api/agents/a/'+('learning' if rebuild else 'resume'))
    def call():
        try:routes.dispatch('DELETE' if rebuild else 'POST',http)
        finally:returned.set()
    request=threading.Thread(target=call);request.start()
    try:
        assert returned.wait(.5),'HTTP waits for HistoryManager preparation'
        assert http.result[0]==202,http.result
        assert not entered.is_set(),'HTTP started heavy preparation'
        queue.start();assert entered.wait(2),'worker was not woken'
        assert queue.status_for('a')['state']=='queued'
    finally:
        release.set();request.join(5)
    deadline=time.monotonic()+2
    while not history.started and time.monotonic()<deadline:time.sleep(.01)
    queue.stop();queue.join(2)
    assert history.started==[('a',rebuild)],history.started
print('shipped entrypoint: Resume/Rebuild HTTP admission independent of blocked worker')
''')

    def test_real_history_registers_cancellable_session_before_sensor_selection(self):
        self.isolated(r'''
import tempfile,threading,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import support
import trial_queue_main,history
from storage import Store
from telemetry import HEAVY_JOBS
from training_queue import TrainingQueue
from test_training_queue import FakeEngine

with tempfile.TemporaryDirectory() as folder:
    store=Store(Path(folder)/'agent.db');aid=store.create_agent(support.agent(mode='paused'))['id']
    saved={'version':11,'schema':{'version':12,'feature_contract_version':4},'saved_weights':[1,2,3]}
    store.save_model(aid,saved);store.set_training_state(aid,'paused',score=.91,samples=1259)
    store.set_training_progress(aid,100,160,200)
    before=store.get_agent_config(aid);model=store.get_model(aid)
    engine=FakeEngine([]);engine.lock=threading.RLock();engine.state_map={};engine.entity_registry={}
    engine.executor.target_lock=lambda target:threading.RLock()
    entered=threading.Event();release=threading.Event();returned=threading.Event();indexed=[];result=[]
    def select(self,agent,**kwargs):
        entered.set();assert release.wait(5);return {}
    with patch.object(history,'STORE',store),patch.dict(history.OPTIONS,manual_agent_training=True),patch.object(history.HistoryManager,'_remember_training_schema',select),patch.object(history.HistoryManager,'_false_activation_schema_rescreen',return_value={}),patch.object(history.HistoryManager,'_run_agent_indexing',side_effect=lambda *a,**k:indexed.append(a)):
        manager=history.HistoryManager(engine)
        def start():
            try:result.append(manager.request_agent_rebuild(aid))
            finally:returned.set()
        starter=threading.Thread(target=start);starter.start()
        try:
            assert entered.wait(2),'sensor selection never started'
            assert returned.wait(.5),'job admission waits for sensor selection'
            assert result==[True],result
            session=manager.training_session_for(aid);assert session is not None
            assert session.phase_detail=='Selecting sensors for rebuild'
            queue=TrainingQueue(manager,store,engine)
            assert queue.cancel_active(aid),'preparation is not cancellable'
            assert session.job_cancel_event.is_set()
            assert store.get_model(aid)==model
        finally:release.set();starter.join(5)
        deadline=time.monotonic()+5
        while manager.agent_jobs and time.monotonic()<deadline:time.sleep(.01)
        assert not manager.agent_jobs,manager.agent_jobs
        assert not indexed,'cancelled preflight reached destructive rebuild'
        assert store.get_model(aid)==model
        after=store.get_agent_config(aid)
        for key in ('training_cursor_ts','training_progress','benchmark_score','benchmark_samples'):
            assert after[key]==before[key],(key,after[key],before[key])
        assert HEAVY_JOBS.owner is None,HEAVY_JOBS.owner
print('real HistoryManager: cancellable sensor preparation preserves model and checkpoint')
''')


if __name__ == '__main__':
    unittest.main()
