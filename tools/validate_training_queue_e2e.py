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
CAP_FROZEN = 'fixture-cap-frozen'
CAP_DISPOSABLE = 'fixture-cap-disposable'


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
    # Startup, HTTP, queue, Engine, HistoryManager and process supervision
    # use the shipped composition. The fixture injects pressure before publish.
    import trial_queue_main
    core = trial_queue_main.core
    default_server = core.ThreadingHTTPServer
    topology = port_file.parent / 'fixture-topology.json'
    if topology.exists():
        prepare = core.prepare_engine_extensions

        def prepare_with_registry():
            prepare()
            # Model the registry normally supplied by HA's websocket.
            entries = json.loads(topology.read_text(encoding='utf-8'))
            core.ENGINE.update_entity_registry(entries)

        core.prepare_engine_extensions = prepare_with_registry
    if port_file.stem == 'restarted':
        install_cap_probe(core, port_file.with_suffix('.cap.json'))

    def bind(_address, handler):
        listener = default_server(('127.0.0.1', 0), handler)
        port_file.write_text(str(listener.server_port), encoding='ascii')
        return listener

    core.ThreadingHTTPServer = bind
    core.main()


def install_cap_probe(core, receipt):
    import history_recovery
    import training_process

    run_attempt = training_process._run_isolated_training_attempt_impl

    def run_with_old_history(history, start_ts, end_ts, job, job_path):
        store = core.STORE
        if store.get_agent_config(job['agent_id'])['target_entity'] == TARGETS[2]:
            start = max(job['sequence_start_ts'], job['sequence_target_end_ts'] - 7 * 86400)
            store.archive_batch([(TARGETS[2], stamp, 'off', {}, None, 'ha_history_full')
                                 for stamp in (start - 120, start - 60)])
        return run_attempt(history, start_ts, end_ts, job, job_path)

    training_process._run_isolated_training_attempt_impl = run_with_old_history

    publish = history_recovery.publish_generation

    def publish_after_prune(store, generation_path, agent_id, *args, **kwargs):
        if not receipt.exists() and store.get_agent_config(agent_id)['target_entity'] == TARGETS[2]:
            journal = core.ENGINE.feature_journal
            with store.lock, store.conn() as c:
                old_history = c.execute('''SELECT event.ts,event.state FROM entity_history AS event
                    JOIN __training_history_windows AS fence USING(entity_id)
                    WHERE fence.owner=? AND event.entity_id=? AND event.rowid<=fence.boundary
                      AND event.ts<fence.start_ts AND fence.operation IS NULL ORDER BY event.ts LIMIT 1''',
                    (agent_id, TARGETS[2])).fetchone()
                assert old_history and old_history[1] == 'off', 'Expected an old row preceding the causal seed'
                frozen = c.execute('''SELECT event.event_time FROM feature_observation_events AS event
                    JOIN __training_feature_windows AS fence USING(entity_id)
                    WHERE fence.owner=? AND event.event_key=? AND event.rowid<=fence.boundary
                      AND event.event_time BETWEEN fence.start_ts AND fence.end_ts
                      AND fence.operation IS NULL''', (agent_id, CAP_FROZEN)).fetchone()
                assert frozen, 'The cap probe must run while its radar observation is frozen'
            store.archive_upsert(TARGETS[2], old_history[0], 'on', source='ha_history_full')
            disposable = 'sensor.training_retention_fixture'
            stamp = frozen[0] + 1
            journal.record(disposable, state(disposable, 'off'), event_time=stamp, received_time=stamp,
                           source='ha_event', event_key=CAP_DISPOSABLE)
            with store.lock:
                with store.conn() as c:
                    assert c.execute('SELECT state FROM entity_history WHERE entity_id=? AND ts=?',
                                     (TARGETS[2], old_history[0])).fetchone()[0] == 'on'
                    before = c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
                old_limit = journal.global_event_limit
                try:
                    journal.global_event_limit = before - 1
                    journal.prune()
                finally:
                    journal.global_event_limit = old_limit
                with store.conn() as c:
                    after = c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
                    survivors = {row[0] for row in c.execute(
                        'SELECT event_key FROM feature_observation_events WHERE event_key IN (?,?)',
                        (CAP_FROZEN, CAP_DISPOSABLE))}
                receipt.write_text(json.dumps({'older_history_updated': True, 'evicted': before - after,
                                               'frozen_row_retained': CAP_FROZEN in survivors,
                                               'disposable_row_evicted': CAP_DISPOSABLE not in survivors}),
                                   encoding='utf-8')
        return publish(store, generation_path, agent_id, *args, **kwargs)

    history_recovery.publish_generation = publish_after_prune


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
    features = (model or {}).get('schema', {}).get('entities', [])
    assert RADAR in features, f'Expected radar in learned features, got {features}'
    with store.conn() as conn:
        count, watermark = conn.execute(
            'SELECT COUNT(*),MAX(id) FROM historical_experiences WHERE agent_id=?', (aid,)).fetchone()
        assert count > 0 and model['_history_watermark'] == watermark
        assert conn.execute('SELECT 1 FROM training_generation_backup WHERE agent_id=?', (aid,)).fetchone()
    assert agent['benchmark_samples'] >= 12, agent
    holdout = model['_frozen_holdout_counts']['per_action']
    assert all(holdout.get(action, {}).get('samples', 0) > 0 for action in ('0', '1')), holdout
    return {'agent_id': aid, 'experiences': count, 'benchmark_samples': agent['benchmark_samples'],
            'features': features, 'watermark': watermark, 'holdout_per_action': holdout}


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
    journal.record(RADAR, state(RADAR, 'on'), event_time=frozen_stamp + 120, received_time=frozen_stamp + 120,
                   source='ha_event', event_key=CAP_FROZEN)
    return ids[:3], states, frozen_stamp, {
        'agents': len(ids), 'entities': len(states), 'seeded_observations': 480*(len(targets)+1)+10000*len(context),
        'archive_window_hours': duration / 3600.,
    }


def main(folder, recorder, rich=False):
    from observation_contract import FeatureJournal
    from storage import STORE as store

    journal = FeatureJournal(store)
    agent_ids, recorder.states, frozen_stamp, fixture = prepare_fixture(store, journal, rich)
    if rich:
        kitchen = [RADAR, *TARGETS, 'sensor.fixture_illuminance_0']
        entities = [
            {'entity_id': row['entity_id'], 'area_id': 'kitchen' if row['entity_id'] in kitchen else 'other'}
            for row in recorder.states
        ]
        (folder/'fixture-topology.json').write_text(json.dumps(entities), encoding='utf-8')
    training_timeout = 1800 if rich else 90
    env = dict(os.environ, HA_BASE_URL=f'http://127.0.0.1:{recorder.server_port}/api',
               SUPERVISOR_TOKEN='', HA_TOKEN='', PYTHONHASHSEED='0', OPENBLAS_NUM_THREADS='1')
    successes = []
    timings = []

    def completed(port, aid):
        outcome = until(lambda: durable_shadow(store, aid), timeout=training_timeout)
        path = f'/api/agents/{aid}/training-diagnostics'
        report = until(lambda: (row if (row := call(port, path))['overall'].get('completed') else None))
        profile = call(port, '/api/status')['history']['recent_training_profiles'][aid]
        sequence = profile['training_job_timings'].get('training_session_profile') or {}
        report['sequence'] = {key: sequence[key] for key in ('chunks', 'chunk_elapsed_seconds', 'phase_seconds')
                              if key in sequence}
        successes.append(outcome)
        timings.append(report)

    with runtime(folder, env, 'first') as port:
        enqueue(port, agent_ids[0], 0)
        enqueue(port, agent_ids[1], 1)
        queue = until(lambda: (data if (data := call(port, '/api/status')['training_queue'])['active'] else None),
                      timeout=20)
        assert queue['active']['agent_id'] == agent_ids[0], queue
        assert queue['queued'][0]['agent_id'] == agent_ids[1], queue
        completed(port, agent_ids[0])
        completed(port, agent_ids[1])
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
        completed(port, aid)
        cap_probe = json.loads((folder / 'restarted.cap.json').read_text(encoding='utf-8'))
        assert cap_probe == {'older_history_updated': True, 'evicted': 1, 'frozen_row_retained': True,
                             'disposable_row_evicted': True}, cap_probe
        diagnostics = timings[-1]
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
                      'control_requests': recorder.control_requests, 'cap_probe': cap_probe, 'http_ms': http_latency,
                      'http_percentiles_ms': percentiles, 'jobs': timings}, indent=2))


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
