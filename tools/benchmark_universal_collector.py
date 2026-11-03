"""Synthetic HOST collector cardinality/queue/SQLite cost; no HA/Pi acceptance."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
from types import SimpleNamespace

_bootstrap=tempfile.TemporaryDirectory(prefix='hm-collector-bootstrap-')
os.environ['ADAPTIVE_AI_DATA']=_bootstrap.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adaptive_ai/src'))
from storage import Store
from observation_contract import FeatureJournal
from universal_history_collector import HistoricalTrainingCollector,source_from_collection


def case(sensors,devices):
    with tempfile.TemporaryDirectory(prefix='hm-collector-case-') as directory:
        path=Path(directory)/'offline.db';store=Store(path)
        journal=FeatureJournal(store,clock=lambda:1000.)
        collector=HistoricalTrainingCollector(store,journal,clock=lambda:1000.)
        store._universal_training_retention_changed=collector.retention_changed
        initial=sum(p.stat().st_size for p in Path(directory).glob('offline.db*'))
        tracemalloc.start();wall=time.perf_counter();cpu=time.process_time()
        collector.connection(True)
        collector.registry('entity',[dict(entity_id=f'binary_sensor.s{i}',device_id=f'd{i%devices}') for i in range(sensors)],at=1.)
        collector.registry('device',[dict(id=f'd{i}',area_id=f'room{i%10}') for i in range(devices)],at=1.)
        collector.registry('area',[dict(area_id=f'room{i}') for i in range(10)],at=1.)
        states={f'binary_sensor.s{i}':dict(state='off',last_updated=datetime.fromtimestamp(10.,timezone.utc).isoformat(),
                                        attributes={'device_class':'motion'}) for i in range(sensors)}
        collector.snapshot(states,received_ts=20.)
        def drain():
            while collector.pending or collector.pending_gap is not None:
                if not collector.drain():raise AssertionError(collector.diagnostics())
        drain()
        producer=0.;events=256
        for i in range(events):
            before=time.perf_counter()
            assert collector.event(f'binary_sensor.s{i%sensors}',dict(state='on' if i%2 else 'off'),
                                   event_ts=21.+i*.01,received_ts=21.+i*.01)
            producer+=time.perf_counter()-before
            if i%32==31:drain()
        for i in range(devices):
            intent=SimpleNamespace(intent_id=f'case-{i}',agent_id=f'a{i}',created_at=25.,target_entity=f'light.l{i}',
                target_property='power',desired_value=1.,policy_version=12,model_revision='synthetic',decision_source='policy')
            assert collector.decision(intent,dict(id=f'a{i}'),dict(state='off'),required=tuple(states),affected=())
            if i%16==15:drain()
        collector.barrier(40.);drain()
        with store.conn() as c:
            c.execute('BEGIN');source_from_collection(c,['a0'],start=20.,through=40.)
        elapsed=time.perf_counter()-wall;used_cpu=time.process_time()-cpu
        current,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        sizes={p.name:p.stat().st_size for p in Path(directory).glob('offline.db*')}
        return dict(sensors=sensors,devices=devices,events=events,wall_seconds=elapsed,cpu_seconds=used_cpu,
            producer_events_per_second=events/max(producer,1e-9),
            end_to_end_events_per_second=events/elapsed,python_peak_bytes=peak,
            database_growth_bytes=sum(sizes.values())-initial,database_bytes=sizes,collector=collector.diagnostics(),
            note='single writer drained in bounded batches; latency is enqueue-to-commit, not HA event-to-intent')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',default='5:1,50:10,200:30,1000:200')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();results=[]
    for item in args.cases.split(','):
        sensors,devices=map(int,item.split(':'))
        if not 1<=sensors<=1000 or not 1<=devices<=200:parser.error('bounded cardinality required')
        results.append(case(sensors,devices))
    report=dict(scope='synthetic host only; no Pi4/HA/physical acceptance',passed=True,cases=results)
    raw=json.dumps(report,indent=2,allow_nan=False)+'\n'
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(raw,encoding='utf-8')
    print(raw)


if __name__=='__main__':main()
