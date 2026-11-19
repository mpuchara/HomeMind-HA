"""Train three agents through the shipped HTTP runtime on disposable SQLite.

Use --rich for a seven-day, 25-agent archive with 240 entities and >2M observations.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'adaptive_ai' / 'src'
RADAR = 'binary_sensor.kitchen_presence_moving_target'
TARGETS = [f'switch.training_fixture_{index}' for index in range(3)]


def state(entity, value):
    return {'entity_id': entity, 'state': value,
            'attributes': {'device_class': 'motion'} if entity == RADAR else {}}


class RecorderStub(BaseHTTPRequestHandler):
    def do_GET(self):
        stamp = datetime.now(timezone.utc).isoformat()
        if self.path == '/api/states':
            body = [dict(row, last_updated=stamp, last_changed=stamp)
                    for row in self.server.states]
        elif self.path.startswith('/api/history/period/'):
            self.server.recorder_requests += 1
            # The test starts with a prepared archive. No network history is missing.
            body = []
        else:
            self.send_error(404)
            return
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.server.control_requests += 1
        self.send_error(403)

    def log_message(self, *args):
        pass


def serve(port_file):
    # Only substitute the listener address. Startup, HTTP, queue, Engine,
    # HistoryManager and process supervision are the shipped composition.
    import trial_queue_main
    core = trial_queue_main.core
    default_server = core.ThreadingHTTPServer

    def bind(_address, handler):
        listener = default_server(('127.0.0.1', 0), handler)
        port_file.write_text(str(listener.server_port), encoding='ascii')
        return listener

    core.ThreadingHTTPServer = bind
    core.main()


def until(predicate, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError('Timed out waiting for the production runtime')


@contextmanager
def runtime(folder, env, label):
    port_file = folder / f'{label}.port'
    log = folder / f'{label}.log'
    with log.open('wb') as output:
        parent = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--serve', str(port_file)],
            cwd=SRC, env=env, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            def ready():
                if parent.poll() is not None:
                    raise RuntimeError(f'Runtime exited with {parent.returncode}')
                try:
                    port = int(port_file.read_text(encoding='ascii'))
                    if call(port, '/health').get('ready'):
                        return port
                except (OSError, ValueError):
                    return None
                return None
            yield until(ready, timeout=60)
        except Exception:
            print(log.read_text(encoding='utf-8', errors='replace')[-16000:], file=sys.stderr)
            raise
        finally:
            parent.terminate()
            parent.wait(timeout=15)


def call(port, path, body=None):
    request = Request(f'http://127.0.0.1:{port}{path}',
                      data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def enqueue(port, aid, sequence):
    request_id = f'training-e2e-rebuild-{sequence}'
    receipt = call(port, f'/api/agents/{aid}/training-requests',
                   {'request_id': request_id, 'action': 'rebuild'})
    assert receipt['state'] == 'pending', receipt

    def admitted():
        status = call(port, f'/api/training-requests/{request_id}')
        if status['state'] == 'failed':
            raise AssertionError(status)
        return status['state'] == 'accepted'

    until(admitted, timeout=20)


def durable_shadow(store, aid):
    agent = store.get_agent_config(aid)
    if agent['mode'] != 'shadow' or agent['training_progress'] != 1.0:
        return None
    model = store.get_model(aid)
    assert model and RADAR in model['schema']['entities'], model
    with store.conn() as conn:
        count, watermark = conn.execute(
            'SELECT COUNT(*),MAX(id) FROM historical_experiences WHERE agent_id=?', (aid,)).fetchone()
        assert count > 0 and model['_history_watermark'] == watermark
        assert conn.execute('SELECT 1 FROM training_generation_backup WHERE agent_id=?', (aid,)).fetchone()
    assert agent['benchmark_samples'] >= 12, agent
    return {'agent_id': aid, 'experiences': count, 'benchmark_samples': agent['benchmark_samples'],
            'features': model['schema']['entities'], 'watermark': watermark}


def prepare_fixture(store, journal, rich):
    targets = TARGETS if not rich else TARGETS + [f'switch.training_fixture_{i}' for i in range(3, 25)]
    context = {}
    if rich:
        for kind, domain, count in (('occupancy', 'binary_sensor', 24), ('illuminance', 'sensor', 40),
                                    ('temperature', 'sensor', 75), ('humidity', 'sensor', 75)):
            for number in range(count):
                context[f'{domain}.fixture_{kind}_{number}'] = kind
    states = [state(entity, 'off') for entity in [RADAR, *targets]]
    for entity, kind in context.items():
        states.append({'entity_id': entity, 'state': 'off' if kind == 'occupancy' else '20',
                       'attributes': {'device_class': kind}})
    ids = [store.create_agent(dict(
        name=target, target_entity=target, target_property='power', min_value=0, max_value=1,
        deadband=.5, confidence_threshold=.78, action_interval=30, exploration_step=1,
        input_entities=[RADAR, *context] if index < 3 else [RADAR]
    ))['id'] for index, target in enumerate(targets)]
    duration = (7 * 86400 - 300) if rich else 480 * 30
    base = time.time() - duration - 30
    step = duration / 480
    store.archive_batch([(entity, base + index * step + (2 if entity != RADAR else 0),
                           'on' if index % 2 else 'off', state(entity, 'off')['attributes'], None, 'test')
                         for index in range(480) for entity in [RADAR, *targets]])
    if rich:
        for entity, kind in context.items():
            attributes = {'device_class': kind}
            for start in range(0, 10000, 500):
                store.archive_batch([
                    (entity, base + index * duration / 10000,
                     ('on' if index % 2 else 'off') if kind == 'occupancy' else str(20 + index % 9),
                     attributes, None, 'test') for index in range(start, start + 500)])
        for aid in ids[:3]:
            store.add_feedback(aid, 0, 0., -1., 'Correct fixture', {0: 1.}, source='manual_feedback')
    frozen_stamp = max(base+30, time.time()-6*3600)
    journal.record(RADAR, state(RADAR, 'on'), event_time=frozen_stamp, received_time=frozen_stamp,
                   source='ha_event', event_key='fixture-frozen')
    return ids[:3], states, frozen_stamp, {
        'agents': len(ids), 'entities': len(states), 'seeded_observations': 480*(len(targets)+1)+10000*len(context),
        'archive_window_hours': duration / 3600.,
    }


def main(folder, recorder, rich=False):
    from observation_contract import FeatureJournal
    from storage import STORE as store

    journal = FeatureJournal(store)
    agent_ids, recorder.states, frozen_stamp, fixture = prepare_fixture(store, journal, rich)
    training_timeout = 1800 if rich else 90
    env = dict(os.environ, HA_BASE_URL=f'http://127.0.0.1:{recorder.server_port}/api',
               SUPERVISOR_TOKEN='', HA_TOKEN='', PYTHONHASHSEED='0', OPENBLAS_NUM_THREADS='1')
    successes = []
    with runtime(folder, env, 'first') as port:
        enqueue(port, agent_ids[0], 0)
        enqueue(port, agent_ids[1], 1)
        queue = until(lambda: (data if (data := call(port, '/api/status')['training_queue'])['active'] else None),
                      timeout=20)
        assert queue['active']['agent_id'] == agent_ids[0], queue
        assert queue['queued'][0]['agent_id'] == agent_ids[1], queue
        successes.append(until(lambda: durable_shadow(store, agent_ids[0]), timeout=training_timeout))
        successes.append(until(lambda: durable_shadow(store, agent_ids[1]), timeout=training_timeout))
    prior_models = {aid: store.get_model(aid) for aid in agent_ids[:2]}
    http_latency = []
    with runtime(folder, env, 'restarted') as port:
        assert all(durable_shadow(store, aid) for aid in agent_ids[:2])
        aid = agent_ids[2]
        enqueue(port, aid, 2)

        def frozen():
            with store.conn() as conn:
                return conn.execute('SELECT 1 FROM __training_feature_windows WHERE owner=?', (aid,)).fetchone()

        until(frozen, timeout=training_timeout)
        # Actual mutation that caused the 0.15.15 prefix failure, while the
        # third production job holds its boundary. Append live observations too.
        journal.open_window('fixture-window', aid, [RADAR], frozen_stamp, 'decision', before=1, after=1)
        with store.conn() as conn:
            assert conn.execute("SELECT protected_until FROM feature_observation_events "
                                "WHERE event_key='fixture-frozen'").fetchone()[0] > time.time()
        for index in range(12):
            stamp = time.time()
            value = 'on' if index % 2 else 'off'
            store.archive_batch([(RADAR, stamp, value, {'device_class': 'motion'}, None, 'live')])
            journal.record(RADAR, state(RADAR, value),
                           event_time=stamp, received_time=stamp, source='ha_event')
            began = time.perf_counter()
            call(port, '/api/agents')
            http_latency.append((time.perf_counter()-began)*1000)
        successes.append(until(lambda: durable_shadow(store, aid), timeout=training_timeout))
        path = f'/api/agents/{aid}/training-diagnostics'
        diagnostics = until(lambda: (report if (report := call(port, path))['overall'].get('completed') else None))
        assert diagnostics['preparation']['payload_rows_fetched_total'] > 0, diagnostics
        assert diagnostics['phases']['replay_seconds'] > 0, diagnostics
        for private in (aid, RADAR, TARGETS[2], 'current_cursor', 'received_time', 'trace', 'token'):
            assert private not in json.dumps(diagnostics), diagnostics
        assert all(store.get_model(aid) == model for aid, model in prior_models.items())
    assert recorder.recorder_requests > 0
    assert recorder.control_requests == 0
    assert len({item['watermark'] for item in successes}) == 3
    ordered = sorted(http_latency)
    percentiles = {f'p{pct}': ordered[math.ceil(pct / 100 * len(ordered)) - 1] for pct in (50, 95, 99)}
    print(json.dumps({'fixture': fixture, 'completed': successes, 'recorder_requests': recorder.recorder_requests,
                      'control_requests': recorder.control_requests, 'http_ms': http_latency,
                      'http_percentiles_ms': percentiles, 'diagnostics': diagnostics}, indent=2))


if __name__ == '__main__':
    sys.path.insert(0, str(SRC))
    if len(sys.argv) > 1 and sys.argv[1] == '--serve':
        serve(Path(sys.argv[2]))
    else:
        with tempfile.TemporaryDirectory(prefix='hm-training-queue-e2e-') as temp:
            folder = Path(temp)
            os.environ['ADAPTIVE_AI_DATA'] = temp
            (folder/'options.json').write_text(json.dumps({
                'manual_agent_training': True, 'training_parallel_agent_workers': 1,
                'process_nice': 0, 'agent_training_history_days': 7,
            }), encoding='utf-8')
            with ThreadingHTTPServer(('127.0.0.1', 0), RecorderStub) as recorder:
                recorder.recorder_requests = 0
                recorder.control_requests = 0
                thread = threading.Thread(target=recorder.serve_forever, daemon=True)
                thread.start()
                try:
                    main(folder, recorder, rich='--rich' in sys.argv)
                finally:
                    recorder.shutdown()
                    thread.join()
