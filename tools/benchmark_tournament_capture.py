"""Independent-source causal snapshot parity/cost; one changed entity per pass."""
import argparse
import hashlib
import json
from pathlib import Path
import pickle
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def one(source, passes, entities=50):
    sys.path[:0] = [str(source/'adaptive_ai/src'), str(source/'tools')]
    import uuid
    import benchmark_shadow_context as fixture
    import context_tournament_promotion as promotion
    from storage import Store
    with tempfile.TemporaryDirectory() as directory, fixture.training_contract(), \
         patch.object(promotion, '_migrate_schema', promotion._migrate_schema), \
         patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion), \
         patch('uuid.uuid4', return_value=uuid.UUID(int=215)), \
         patch('time.time', return_value=fixture.STAMP):
        f = fixture.Fixture(Store(Path(directory)/'capture.db'))
        for i in range(max(0, entities-len(f.states))):
            eid = f'sensor.extra_{i}'
            for age in reversed(range(24)):
                st = fixture.state(eid, str(i%100), fixture.STAMP-age,
                    friendly_name=eid, unit_of_measurement='%', device_class='humidity')
                f.temporal.add(eid, fixture.STAMP-age, st)
            f.states[eid] = st
            f.engine.entity_registry[eid] = dict(entity_id=eid, device_id=eid, area_id='room_0')
        queue = f.service._deferred_observations
        times, signatures, charges = [], [], []
        for step in range(passes+1):
            stamp = fixture.STAMP+step
            eid = 'binary_sensor.room_1'
            f.states[eid] = fixture.state(eid, 'on' if step%2 else 'off', stamp,
                                          device_class='occupancy')
            f.temporal.add(eid, stamp, f.states[eid])
            f.engine.runtime[f.agent['id']]['last_inference_ts'] = stamp
            f.engine.state_revision = step
            started = time.perf_counter()
            with patch('time.time', return_value=stamp):
                job = queue._capture(f.agent, f.states, {eid}, None)
            times.append((time.perf_counter()-started)*1000)
            packet = pickle.loads(job['blob'])
            if 'history' in job:
                packet['samples'] = job['history']
            for field in ('states', 'registry'):
                if field in job:
                    packet[field] = job[field]
            attrs = packet.pop('policy')
            model = job['policy_type'].__new__(job['policy_type'])
            vars(model).update(attrs)
            packet['policy_export'] = model.export()
            packet['excluded'] = sorted(packet['excluded'])
            signatures.append(hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest())
            charges.append(job['charge'])
        return dict(entities=len(f.states), cold_ms=times[0], warm_median_ms=statistics.median(times[1:]),
                    warm_p95_ms=sorted(times[1:])[int((passes-1)*.95)],
                    retained_charge_bytes=max(charges), frames_sha256=signatures,
                    temporal_cache=queue.snapshot().get('temporal_snapshots'))


def paired(baseline, passes, entities=50):
    series = []
    for repeat in range(3):
        pair = {}
        for name in (('before','after') if repeat%2==0 else ('after','before')):
            source = baseline if name == 'before' else ROOT
            output = subprocess.check_output([sys.executable, str(Path(__file__).resolve()),
                '--one','--source-root',str(source),'--passes',str(passes),'--entities',str(entities)],text=True)
            pair[name] = json.loads(output)
        assert pair['before']['frames_sha256'] == pair['after']['frames_sha256']
        pair['warm_speedup'] = pair['before']['warm_median_ms']/pair['after']['warm_median_ms']
        series.append(pair)
    return dict(series=series, every_causal_frame_identical=True,
                median_warm_speedup=statistics.median(row['warm_speedup'] for row in series),
                scope='Snapshot component only; no HA ingress, device or memory-utilization claim')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=ROOT)
    parser.add_argument('--baseline-root',type=Path)
    parser.add_argument('--passes',type=int,default=32)
    parser.add_argument('--entities',type=int,default=50)
    parser.add_argument('--one',action='store_true')
    args = parser.parse_args()
    print(json.dumps(one(args.source_root,args.passes,args.entities) if args.one else paired(args.baseline_root,args.passes,args.entities)))
