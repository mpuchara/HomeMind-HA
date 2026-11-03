"""Offline HA-native audit experiments; no live HA/network/Recorder database.

Transport A/B/C uses one generated corpus per repetition. Journal cost uses the
real HomeMind FeatureJournal/V3 schema in disposable SQLite. Recorder inspection
is auxiliary only: none of these experiments issues a coverage witness.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

from ha_native_experiments import recorder_assessment,transport_case


def journal_case(sensors,per_sensor=4):
    from storage import Store
    from observation_contract import FeatureJournal
    from storage_budget import BudgetCursor
    calls=Counter();execute=BudgetCursor.execute;many=BudgetCursor.executemany
    def counted(cursor,*args,**kwargs):calls['execute']+=1;return execute(cursor,*args,**kwargs)
    def counted_many(cursor,*args,**kwargs):calls['executemany']+=1;return many(cursor,*args,**kwargs)
    with tempfile.TemporaryDirectory(prefix='ha-native-journal-') as folder,ExitStack() as stack:
        store=Store(Path(folder)/'journal.db')
        journal=FeatureJournal(store,clock=lambda:2000.,retention_hours=1000000,max_events_per_entity=100000)
        stack.enter_context(patch.object(BudgetCursor,'execute',counted))
        stack.enter_context(patch.object(BudgetCursor,'executemany',counted_many))
        rows=[dict(entity_id=f'sensor.fixture_{i}',state=dict(state=str(tick),attributes={'unit_of_measurement':'x'}),
                   event_time=1000.+tick,received_time=1000.1+tick,source='synthetic_ha_event',quality=1.)
              for tick in range(per_sensor) for i in range(sensors)]
        wall=time.perf_counter();cpu=time.process_time()
        for offset in range(0,len(rows),128):journal.record_batch(rows[offset:offset+128])
        timing=dict(wall_ms=(time.perf_counter()-wall)*1000,cpu_ms=(time.process_time()-cpu)*1000)
        write_calls=dict(calls)
        with store.conn() as c:
            count=c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
            try:
                objects={r[0]:r[1] for r in c.execute('SELECT name,SUM(pgsize) FROM dbstat GROUP BY name')
                         if r[0].startswith(('feature_','universal_'))}
            except Exception as exc:objects={'unavailable':type(exc).__name__}
        files={p.name:p.stat().st_size for p in Path(folder).glob('journal.db*')}
        assert count==len(rows)
        return dict(sensors=sensors,rows=count,per_sensor=per_sensor,batch_size=128,timing=timing,
            write_sql_api_calls=write_calls,sqlite_objects_bytes=objects,sqlite_files_bytes=files,
            recorder_rows=None,actual_duplicate_rows=None,coverage_witness=False,
            limits='Actual local FeatureJournal writes with V3 triggers, no live Recorder or concurrent inference. SQL API calls exclude internal trigger VM work; dbstat reports object pages, not logical payload size. No inference about which entities a household Recorder includes.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',default='5:1,50:10,200:30,1000:200')
    p.add_argument('--events',type=int,default=400);p.add_argument('--repeats',type=int,default=2)
    p.add_argument('--journal',action='store_true');p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():p.error('choose a new immutable results path')
    if not 10<=args.events<=2000 or not 1<=args.repeats<=3:p.error('bounded events/repeats required')
    cases=[tuple(map(int,pair.split(':'))) for pair in args.cases.split(',')]
    if any(not (5<=s<=1000 and 1<=d<=200) for s,d in cases):p.error('bounded cardinalities required')
    with tempfile.TemporaryDirectory(prefix='ha-native-bootstrap-') as bootstrap:
        os.environ['ADAPTIVE_AI_DATA']=bootstrap
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adaptive_ai/src'))
        transport=[dict(repeat=rep,**transport_case(s,d,args.events)) for s,d in cases for rep in range(args.repeats)]
        journal=[journal_case(s) for s,d in cases] if args.journal else []
        inspection=[recorder_assessment(rows,['sensor.example']) for rows in (
            None,[],[[dict(entity_id='sensor.example',state='1',attributes={},context={},
                           last_changed='original',last_updated='original')]],[[dict(state='2')]])]
    result=dict(scope='Offline synthetic HA-native audit, not HA server/network/Recorder/Pi measurements',
        transport=transport,journal=journal,recorder_inspection=inspection,
        architecture_decision=dict(filtered_compact='research only, runtime cache only; no evidence qualification',
            dual_stream='not adopted: measured additive wire cost; no measured end-to-end benefit',
            recorder_replacement='rejected: API responses lack original local receipt/coverage/AI authority',
            recorder_auxiliary='existing HAClient/HistoryManager retained; no hot-path history query'),
        actual_ha_version=None,production_configuration_changed=False)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print('HA_NATIVE_JSON='+json.dumps(result,separators=(',',':')))


if __name__=='__main__':main()
