"""Paired promotion snapshot/JSON/SQLite cost; all evidence and model data retained."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
sys.path.insert(0, str(ROOT))
_scratch = tempfile.TemporaryDirectory(prefix='homemind-promotion-snapshots-')
os.environ['ADAPTIVE_AI_DATA'] = _scratch.name
from context_tournament import ContextTournament
import context_tournament_promotion as promotion
from context import action_values
from model_encoding_cache import ModelEncodingCache
import shadow_model_json
from storage import Store
from tools.benchmark_native_witness import fixture


def previous_observer(service, original):
    """Frozen 0.14.169 observer with automatic promotion disabled in this experiment."""
    def observe(agent, states=None, changed=None):
        result = original(agent, states, changed)
        aid = str(agent['id'])
        tournament = service.state(aid)
        actions = [float(x) for x in action_values(agent)]
        cfg = promotion.tournament_config()
        assert not cfg['enabled']
        now = time.time()
        for challenger in tournament['challenger_features']:
            model = service._load_shadow_model(aid, challenger, len(actions))
            promotion.ensure_promotion_epoch(model, len(actions), now, cfg)
            promotion.absorb_new_scored_evidence(model, actions, now, cfg)
            service._save_shadow_model(aid, challenger, model)
        return result
    return observe


def run_mode(mode, payloads, passes, label_every):
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'benchmark.db')
        # No background writer: explicitly flush the same five-observation batches.
        service = ContextTournament(store, SimpleNamespace())
        keys = [f'sensor.challenger_{i}' for i in range(len(payloads))]
        service.state = lambda aid: {'challenger_features': keys}
        agent = dict(id='paired', target_entity='light.example', target_property='power', min_value=0, max_value=1)
        models = []
        for key, payload in zip(keys, payloads):
            model = service._blank_shadow_model(2)
            model.update(candidate_policy=copy.deepcopy(payload), class_totals=[0, 0],
                         active_correct_by_class=[0, 0], shadow_correct_by_class=[0, 0],
                         evaluation_started_ts=1000., observation_opportunities=0,
                         available_observations=0)
            service._shadow_models[(agent['id'], key)] = model
            models.append(model)
        step = 0
        def original(agent, states=None, changed=None):
            # Represents the existing metrics layer's 60-second cumulative snapshot.
            for key, model in zip(keys, models):
                model['observation_opportunities'] += 1
                model['available_observations'] += int(step % 7 != 0)
                if step % label_every == label_every - 1:
                    model['samples'] += 1
                    idx = model['samples'] % 2
                    model['class_totals'][idx] += 1
                    model['active_correct_by_class'][idx] += int(model['samples'] % 3 != 0)
                    model['shadow_correct_by_class'][idx] += 1
                    model['last_scored_ts'] = 1000. + step
                    # A labelled episode changes trained policy content too;
                    # the writer must encode that fresh body, not an old cache hit.
                    model['candidate_policy']['rows'][0]['weight'] = model['samples'] / 100.
                if step % 60 == 0:
                    service._save_shadow_model(agent['id'], key, model)
            return dict(scored=int(step % label_every == label_every - 1))
        service.observe_shadow = original
        promotion.install_promotion(service)
        observer = previous_observer(service, original) if mode == 'previous' else service.observe_shadow
        times, state_history = [], []
        checkpoint_count = 0
        for step in range(passes):
            with patch('context_tournament_promotion.time.time', return_value=1000. + step):
                started = time.perf_counter()
                result = observer(agent, {}, set())
                if step % 5 == 4:
                    count = service._flush_shadow_models()
                    if count:
                        with store.conn() as connection:
                            connection.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
                        checkpoint_count += 1
                times.append((time.perf_counter() - started) * 1000)
            # Detached proof of every live result, not just final samples/accuracy.
            state_history.append((result, hashlib.sha256(json.dumps(models, sort_keys=True).encode()).hexdigest()))
        service._flush_shadow_models()
        with store.conn() as connection:
            final = [json.loads(row[0]) for row in connection.execute(
                'SELECT model_json FROM context_tournament_shadow ORDER BY challenger_entity')]
        assert final == models, 'final step must contain a label, retaining complete cumulative model data'
        return dict(median_observation_ms=statistics.median(times), total_ms=sum(times),
                    snapshots=service.shadow_persistence_snapshot()['queued'],
                    written_rows=service.shadow_persistence_snapshot()['flushed'],
                    checkpoint_batches=checkpoint_count), state_history, final


def run(passes=64, label_every=8, width=512, rows=512):
    assert passes % label_every == 0
    payloads = fixture(width, rows)
    series = []
    for repeat in range(3):
        results = {}
        with patch.dict(promotion.OPTIONS, {'context_tournament_enabled': False}):
            for mode in (('previous', 'current') if repeat % 2 == 0 else ('current', 'previous')):
                # Independent caches prevent the first run from warming the second.
                with patch.object(shadow_model_json, 'MODEL_ENCODING_CACHE', ModelEncodingCache()):
                    results[mode] = run_mode(mode, payloads, passes, label_every)
        assert results['previous'][1:] == results['current'][1:], 'all live states and final durable models must match'
        old, new = results['previous'][0], results['current'][0]
        series.append(dict(previous=old, current=new, total_speedup=old['total_ms']/new['total_ms']))
    return dict(synthetic=True, models=4, passes=passes, independent_labels=passes//label_every,
                width=width, rows=rows, every_live_result_and_model_parity=True,
                final_durable_json_parity=True, series=series,
                limit='Promotion snapshot/JSON/SQLite component; no claim about HA latency or candidate validation cost.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--passes', type=int, default=64)
    parser.add_argument('--label-every', type=int, default=8)
    parser.add_argument('--width', type=int, default=512)
    parser.add_argument('--rows', type=int, default=512)
    args = parser.parse_args()
    print(json.dumps(run(args.passes, args.label_every, args.width, args.rows), indent=2))
