"""Source-paired localhost tile delivery and existing Candidate worker page caches.

Publication is deterministic to isolate UI transport from model inference. This
does not measure HA ingress, browser painting, Ingress proxy or a physical ACK.
"""
import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def one(source, events=6):
    sys.path[:0] = [str(source/'adaptive_ai/src'), str(source/'tests')]
    with tempfile.TemporaryDirectory() as scratch, ExitStack() as stack:
        os.environ['ADAPTIVE_AI_DATA'] = scratch
        import support
        import agent_live_card_refresh as live
        import test_candidate_shadow_runtime as fixture
        import agent_candidate_shadow_context as context
        from runtime_sqlite_cache import RuntimeSQLiteCache
        engine = SimpleNamespace(lock=threading.RLock(), all_agent_configs={'a':support.agent(id='a',target_entity='light.a')},
            state_map={'light.a':support.state('light.a','off')}, entity_revisions={'light.a':0},
            runtime={'a':{'last_prediction':0.,'last_confidence':.9}}, _refresh_agent_index=lambda: None)
        reactive = (source/'adaptive_ai/src/live_card_updates.py').exists()
        if reactive:
            from live_card_updates import LiveCardUpdates
            engine.live_card_updates = LiveCardUpdates()
        core = SimpleNamespace(ENGINE=engine)
        core.live_agent_payload = lambda include_configs=False: live.live_agent_payload(core,include_configs=include_configs)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                payload = live.live_request_payload(core,query) if reactive else core.live_agent_payload()
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self,*args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever,daemon=True)
        serving.start()
        def request(path='/api/live'):
            with urlopen(f'http://127.0.0.1:{server.server_port}'+path,timeout=3) as response:
                return json.load(response)
        rows, times = [], []
        try:
            for i in range(events):
                first = request()
                started = time.perf_counter()
                published = []
                def publish():
                    time.sleep(.05)  # Decision becomes ready between legacy polls.
                    value = float((i+1)%2)
                    with engine.lock:
                        engine.runtime['a'] = dict(last_prediction=value,last_confidence=.9,
                                                  last_inference_ts=100.+i)
                        if reactive:
                            engine.runtime['a']['_live_decision'] = dict(engine.runtime['a'],decision_ready_ts=100.+i)
                    published.append(time.perf_counter())
                    if reactive:
                        engine.live_card_updates.publish()
                publisher = threading.Thread(target=publish)
                publisher.start()
                if reactive:
                    payload = request('/api/live?after='+first['revision']+'&wait_ms=1500')
                else:
                    time.sleep(max(0.,.5-(time.perf_counter()-started)))
                    payload = request()
                received = time.perf_counter()
                publisher.join(3)
                assert published and not publisher.is_alive()
                value = payload['agents'][0]['last_prediction']
                assert value == float((i+1)%2)
                rows.append(value)
                times.append((received-published[0])*1000)
        finally:
            server.shutdown();server.server_close();serving.join(3)
        f = fixture.CandidateShadowRuntimeTests(methodName='runTest')
        f.setUp()
        stack.callback(f.tearDown)
        f._g1()
        policy = f.engine.policy(f.root)
        f.engine.policy = lambda agent: policy
        with patch.object(context,'_generation',wraps=context._generation) as reads:
            context.install(f.manager)
            for _ in range(24):
                f.engine.policy(f.root)
            generation_reads = reads.call_count
        pool = RuntimeSQLiteCache(256,memory=lambda:(8192,4096))
        f.store._runtime_sqlite_cache = pool
        stack.callback(pool.stop)
        turns = []
        def maintenance():
            with f.store.conn() as c:
                turns.append(c.execute('SELECT count(*) FROM agent_candidates').fetchone()[0])
            if len(turns)>=24:
                f.manager.stop()
            else:
                f.manager.wake_event.set()
        f.manager._maintenance = maintenance
        # Real lifecycle rows are stable comparing rows, so this performs actual
        # _all_rows plus the bounded read maintenance without starting training.
        f.manager._worker_loop()
        assert len(turns)==24 and len(set(turns))==1
        f.executor.service.assert_not_called()
        return dict(events=events, publication_to_http_median_ms=statistics.median(times),
            publication_to_http_p95_ms=sorted(times)[int((len(times)-1)*.95)],
            decision_values_sha256=hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            root_policy_calls=24,generation_reads=generation_reads,worker_passes=24,
            sqlite_page_cache=pool.snapshot(),ha_calls=0,
            limit='Localhost API response after deterministic ready-decision publication at a fixed 50ms phase. '
                  'Excludes model computation, browser/Ingress/HA network and physical ACK. Existing bounded cache only.')


def paired(baseline,events):
    series=[]
    for i in range(3):
        pair={}
        for name in (('before','after') if i%2==0 else ('after','before')):
            output=subprocess.check_output([sys.executable,str(Path(__file__).resolve()),'--one',
                '--source-root',str(baseline if name=='before' else ROOT),'--events',str(events)],text=True)
            pair[name]=json.loads(output.strip().splitlines()[-1])
        assert pair['before']['decision_values_sha256']==pair['after']['decision_values_sha256']
        series.append(pair)
    return dict(synthetic=True,independent_sources=True,series=series,
                median_before_ms=statistics.median(x['before']['publication_to_http_median_ms'] for x in series),
                median_after_ms=statistics.median(x['after']['publication_to_http_median_ms'] for x in series),
                limit=series[0]['after']['limit'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--one',action='store_true')
    parser.add_argument('--source-root',type=Path,default=ROOT)
    parser.add_argument('--baseline-root',type=Path)
    parser.add_argument('--events',type=int,default=6)
    args=parser.parse_args()
    if args.events<2 or (not args.one and args.baseline_root is None):
        parser.error('events >=2 and baseline-root required except --one')
    print(json.dumps(one(args.source_root.resolve(),args.events) if args.one else
                     paired(args.baseline_root.resolve(),args.events),indent=None if args.one else 2))
