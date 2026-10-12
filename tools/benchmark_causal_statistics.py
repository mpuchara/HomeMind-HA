"""Causal trajectory prefix replay: legacy parity and deterministic work gate."""
import argparse
import copy
import json
from pathlib import Path
import sqlite3
import sys
import time
import zlib
import bisect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai' / 'src'))
from causal_home_statistics import CausalHomeStatistics
from context_engine import ContextEngine
from home_state import RoomBeliefModel
from settings import DEFAULT_OPTIONS


def fixture(cycles=400):
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE entity_history (id INTEGER PRIMARY KEY, entity_id TEXT, '
                       'ts REAL, received_ts REAL, state TEXT, attributes_json TEXT)')
    connection.execute('CREATE INDEX history_entity_ts ON entity_history(entity_id,ts)')
    states = {eid: {'entity_id': eid, 'state': 'off', 'attributes': {'device_class': 'motion'}}
              for eid in ('binary_sensor.hall', 'binary_sensor.room')}
    context = ContextEngine(dict(DEFAULT_OPTIONS))
    context.configure(states, entities={eid: {'area_id': area} for eid, area in
                      [('binary_sensor.hall', 'hall'), ('binary_sensor.room', 'room')]})
    base = 1701000000.0
    rows = [(eid, base, base, 'off', json.dumps(states[eid]['attributes'])) for eid in states]
    for n in range(cycles):
        ts = base + 10 + n * 40
        rows.extend((eid, ts + offset, ts + offset + delay, value,
                     json.dumps(states[eid]['attributes'])) for eid, offset, value, delay in
                    [('binary_sensor.hall', 0, 'on', 0), ('binary_sensor.room', 2, 'on', 2),
                     ('binary_sensor.hall', 15, 'off', 0), ('binary_sensor.room', 15, 'off', 0)])
    connection.executemany('INSERT INTO entity_history(entity_id,ts,received_ts,state,attributes_json) '
                           'VALUES(?,?,?,?,?)', rows)
    connection.commit()
    return connection, context, base, base + cycles * 40 + 100


def legacy_statistics_at(index, timestamp, counts):
    # Frozen pre-0.14.183 query algorithm; query expiry is deliberately independent.
    if timestamp < index.start:
        model = RoomBeliefModel(index.context.options.get('home_model_half_life_days', 45))
        return model.graph, model.dwell, model.calibration
    position = max(0, bisect.bisect_right(index.times, timestamp) - 1)
    raw = json.loads(zlib.decompress(index.snapshots[position]))
    model = RoomBeliefModel(index.context.options.get('home_model_half_life_days', 45), raw['stats'])
    for name, value in raw['runtime'].items():
        setattr(model, name, value)
    model.area_sources = {area: set(ids) for area, ids in raw['area_sources'].items()}
    model.arrivals.extend((area, stamp) for area, stamp in raw['arrivals'])
    for row in index._rows(index.times[position], timestamp):
        index._observe(model, row)
        counts['rows'] += 1
    model.expire(timestamp)
    return model.graph, model.dwell, model.calibration


def run():
    connection, context, base, end = fixture()
    try:
        index = CausalHomeStatistics(connection, context, base, end)
        # Two independent feature cursors alternate and rewind at chunk-like boundaries.
        stamps = [(base + offset, cursor) for offset in range(1, 12000, 3)
                  for cursor in ('onset', 'persistence')]
        counts = {'rows': 0}
        started = time.perf_counter()
        expected = [copy.deepcopy(legacy_statistics_at(index, ts, counts)) for ts, _ in stamps]
        legacy_seconds = time.perf_counter() - started
        started = time.perf_counter()
        for (ts, cursor), reference in zip(stamps, expected):
            if index.statistics_at(ts, cursor=cursor) != reference:
                raise AssertionError(f'causal statistics mismatch at {ts}, {cursor}')
        optimized_seconds = time.perf_counter() - started
        status = index.status()
        assert status['query_rows'] < counts['rows'] / 10, status
        assert status['compressed_bytes'] + status['cursor_bytes'] <= status['max_bytes']
        return {'identical': True, 'queries': len(stamps), 'legacy_rows': counts['rows'],
                'optimized_rows': status['query_rows'], 'legacy_seconds': legacy_seconds,
                'optimized_seconds': optimized_seconds,
                'component_speedup': legacy_seconds / max(optimized_seconds, 1e-9),
                'status': status,
                'timing_note': 'Synthetic component timing; not a complete Pi training estimate'}
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(), indent=None if args.compact else 2))
