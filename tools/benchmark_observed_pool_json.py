"""Paired sensor-edge encoding cost with full retained examples and fresh labels."""
import argparse
import copy
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adaptive_ai/src'))
_scratch = tempfile.TemporaryDirectory(prefix='homemind-pool-json-')
os.environ['ADAPTIVE_AI_DATA'] = _scratch.name
from context_tournament_policy_candidate import _pool_json_payload, semantic_predictive_score


def previous(row, cached=None, *, samples_changed):
    if samples_changed or not row.get('screening'):
        row['screening'] = semantic_predictive_score(row['samples'])
    return tuple(json.dumps(row[name], separators=(',', ':'), sort_keys=True)
                 for name in ('history', 'samples', 'screening'))


def run(iterations=96):
    row = dict(history=[[float(i), i / 11.] for i in range(32)],
               samples=[dict(episode=str(i), ts=float(i), label=float(i % 2), value=i / 7.,
                             quality=.98, lag_1=i / 8., lag_3=i / 9., lag_10=i / 10.,
                             trend=i / 17., time_since_edge=.95,
                             interactions={f'primary_{j}': i / (13.+j) for j in range(3)})
                        for i in range(96)], screening={})
    series = []
    for repeat in range(3):
        result = {}
        outputs = {}
        for name, fn in ([('previous', previous), ('current', _pool_json_payload)] if repeat % 2 == 0
                         else [('current', _pool_json_payload), ('previous', previous)]):
            data = copy.deepcopy(row)
            cached = fn(data, samples_changed=True)
            times, rows = [], []
            for i in range(iterations):
                data['history'] = (data['history'] + [[32.+i, i / 17.]])[-32:]
                # Both ON/OFF outcomes, including brief visits, remain labels.
                label = i % 16 == 15
                if label:
                    data['samples'] = (data['samples'] + [dict(episode=f'new-{i}', label=float((i // 16) % 2), value=.5)])[-96:]
                started = time.perf_counter()
                cached = fn(data, cached, samples_changed=label)
                times.append((time.perf_counter() - started) * 1000)
                rows.append(cached)
            result[name] = dict(median_ms=statistics.median(times), total_ms=sum(times),
                                observations=iterations, fresh_labels=iterations//16)
            outputs[name] = rows
        assert outputs['previous'] == outputs['current']
        result['total_speedup'] = result['previous']['total_ms'] / result['current']['total_ms']
        series.append(result)
    return dict(synthetic=True, retained_examples=96, history_points=32, series=series,
                every_snapshot_byte_parity=True, fresh_label_parity=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=96)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(max(1, args.iterations)), indent=None if args.compact else 2))
