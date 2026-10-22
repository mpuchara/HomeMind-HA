"""Real final-route HTTP reads while Store.lock and SQLite writer are occupied."""
import json,sys,threading,time,sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tests'))
import support,trial_queue_main
from runtime_http import ExplicitRouteRegistry,install_dispatch
from agent_workflow_actions import register_read_routes
from agent_correct_generation_history import build_correct_history,build_correct_point
from test_correct_multigeneration_history import CorrectGenerationHistoryTests


def main():
    fixture=CorrectGenerationHistoryTests(methodName='runTest');fixture.setUp()
    store,manager=fixture.store,fixture.manager
    manager.workflow_correct_history=lambda ref,start,end,compact=False:build_correct_history(manager,ref,start,end,None,compact)
    manager.workflow_correct_point=lambda ref,ts:build_correct_point(manager,ref,ts,None)
    manager.workflow_subject=Mock(side_effect=AssertionError('History must not depend on full workflow status'))
    core=trial_queue_main.core;core.STORE=store;core.ENGINE=fixture.engine;core.AUTOMATION_KNOWLEDGE=object()
    core.set_startup('ready',7,'fixture',ready=True)
    registry=ExplicitRouteRegistry();register_read_routes(registry,manager)
    server=ThreadingHTTPServer(('127.0.0.1',0),core.Handler);core.HTTP_SERVER=server;install_dispatch(core,registry)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    start,end=fixture.ts-10,fixture.ts+2
    def read(ref):
        before=time.perf_counter()
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/agent-workflow/{ref}/correct-history?start={start}&end={end}&compact=1',timeout=2) as response:body=json.load(response)
        return body,(time.perf_counter()-before)*1000
    connection=sqlite3.connect(store.path,timeout=0,isolation_level=None)
    try:
        connection.execute('BEGIN IMMEDIATE')
        with store.lock,patch.object(store,'get_model',side_effect=AssertionError('Chart must not deserialize model')):
            snapshots=[];maximum=0.
            for ref in (fixture.root['id'],fixture.g1['generation_id'],fixture.g2['generation_id']):
                body,elapsed=read(ref);maximum=max(maximum,elapsed)
                assert elapsed<1000,elapsed
                assert body['series']['current']['pairs'] and body['policy_replay_used'] is False,body
                snapshots.append(body)
            assert snapshots[-1]['parent_generation_id']==fixture.g1['generation_id']
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/agent-workflow/{fixture.root["id"]}/correct-point?ts={fixture.ts}',timeout=2) as response:
                point=json.load(response);assert point['desired']==0. and point['current']==0.,point
        manager.workflow_subject.assert_not_called()
        print(json.dumps(dict(real_http=True,shipped_entrypoint=True,store_lock_held=True,sqlite_writer_held=True,live_and_two_generations=True,direct_parent_preserved=True,no_model_read=True,no_full_status=True,point_read=True,max_host_read_ms=round(maximum,3))))
    finally:
        connection.rollback();connection.close();server.shutdown();server.server_close();worker.join(2);fixture.tearDown()


if __name__=='__main__':main()
