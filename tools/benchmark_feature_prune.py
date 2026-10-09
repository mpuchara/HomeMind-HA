"""Paired previous/current FeatureJournal pruning, with exact retained-key parity."""
import argparse
from itertools import product
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time
from types import MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adaptive_ai/src'))
if __name__ == '__main__':
    _scratch = tempfile.TemporaryDirectory(prefix='homemind-prune-import-')
    os.environ['ADAPTIVE_AI_DATA'] = _scratch.name
from observation_contract import FeatureJournal
from storage import Store


def previous_prune(self, entity_id=None):
    """Frozen 0.14.161 implementation, including its original ORDER BY."""
    now = float(self.clock())
    cutoff = now - self.retention_hours * 3600.0
    with self.store.lock, self.store.conn() as c:
        c.execute('DELETE FROM feature_window_entities WHERE window_id IN '
                  '(SELECT window_id FROM feature_windows WHERE protected_until<?)', (now,))
        c.execute('DELETE FROM feature_windows WHERE protected_until<?', (now,))
        c.execute('DELETE FROM feature_observation_events WHERE received_time<? AND protected_until<?', (cutoff, now))
        if entity_id:
            extra = c.execute('''SELECT event_key FROM feature_observation_events
                WHERE entity_id=? AND protected_until<? ORDER BY received_time DESC LIMIT -1 OFFSET ?''',
                (str(entity_id), now, self.max_events_per_entity)).fetchall()
            if extra:
                c.executemany('DELETE FROM feature_observation_events WHERE event_key=?', [(r[0],) for r in extra])
        total = int(c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0] or 0)
        if total > self.global_event_limit:
            doomed = c.execute('''SELECT event_key FROM feature_observation_events
                ORDER BY CASE WHEN protected_until>? THEN 1 ELSE 0 END, received_time ASC LIMIT ?''',
                (now, total-self.global_event_limit)).fetchall()
            c.executemany('DELETE FROM feature_observation_events WHERE event_key=?', [(r[0],) for r in doomed])
    return True


def seed_events(connection, rows):
    connection.executemany('''INSERT INTO feature_observation_events
        (event_key,contract_version,entity_id,event_time,received_time,state,attributes_json,source,quality,protected_until)
        VALUES(?,4,?,?,?,?,?,'ha_state_changed',1,?)''', rows)


def run(events=50000, excess=128, passes=3):
    series = []
    with tempfile.TemporaryDirectory(prefix='homemind-prune-benchmark-') as directory:
        folder = Path(directory)
        for mode, all_protected in product(('prune','append_and_prune'), (False,True)):
            base = folder / f'base-{mode}-{all_protected}.db'
            source = Store(base)
            FeatureJournal(source)
            # Independent payloads expose the old table scan's data-page cost.
            with source.conn() as c:
                seed_events(c, ((f'e{i:08d}', f'sensor.context_{i%64}', 900.+i/1000.,
                                 900.+i/1000., str(i%2), json.dumps({'data': 'x'*1024, 'id': i}),
                                 2000. if all_protected or i%5 else 0.)
                                for i in range(events+(excess if mode=='prune' else 0))))
                if mode=='append_and_prune':
                    c.execute("INSERT INTO feature_windows VALUES('coverage',4,'agent',950,800,1001,'decision',950,2000)")
                    c.executemany("INSERT INTO feature_window_entities VALUES('coverage',?)",
                                  [(f'sensor.context_{i}',) for i in range(64)])
            prepared = [dict(event_key=f'e{i:08d}', entity_id=f'sensor.context_{i%64}',
                             event_time=900.+i/1000.,received_time=900.+i/1000.,state=str(i%2),
                             attributes_json=json.dumps({'data':'x'*1024,'id':i}),source='ha_state_changed',quality=1.)
                        for i in range(events,events+excess)]
            measurements = {'previous': [], 'current': []}
            for repeat in range(passes):
                outputs = {}
                for name in (('previous','current') if repeat%2==0 else ('current','previous')):
                    path = folder / f'{mode}-{all_protected}-{repeat}-{name}.db'
                    shutil.copyfile(base, path)  # all source connections closed; WAL drained
                    store = Store(path)
                    journal = FeatureJournal(store, clock=lambda:1000., retention_hours=1e6,
                                             max_events_per_entity=events+excess+1, global_event_limit=events)
                    if name == 'previous':
                        with store.conn() as c:
                            c.execute('DROP INDEX idx_feature_obs_prune_cover')
                            c.execute('DROP INDEX idx_feature_windows_expiry')
                        journal.prune = MethodType(previous_prune,journal)
                    # Match the deployed runtime: pruning does not own a final
                    # connection-close checkpoint over the whole database.
                    store.start_wal_keeper()
                    started = time.perf_counter()
                    if mode=='append_and_prune':
                        journal.record_batch(prepared)
                        if excess<128:
                            journal.prune(entity_id=prepared[-1]['entity_id'])
                    else:
                        journal.prune()
                    measurements[name].append((time.perf_counter()-started)*1000)
                    with store.conn() as c:
                        outputs[name] = [r[0] for r in c.execute('SELECT event_key FROM feature_observation_events ORDER BY event_key')]
                    store.stop_wal_keeper()
                assert outputs['previous'] == outputs['current']
                assert len(outputs['current']) == events
            row = dict(mode=mode, all_protected=all_protected, rows=events+excess, removed=excess,
                       previous_median_ms=statistics.median(measurements['previous']),
                       current_median_ms=statistics.median(measurements['current']), passes=passes)
            row['speedup'] = row['previous_median_ms']/row['current_median_ms']
            series.append(row)
    return dict(synthetic=True, retained_key_parity=True, cases=series,
                limit='Pruning component including commit; not a forecast of HA event latency.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=50000)
    parser.add_argument('--excess', type=int, default=128)
    parser.add_argument('--passes', type=int, default=3)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(max(1,args.events),max(1,args.excess),max(1,args.passes)),
                     indent=None if args.compact else 2))
