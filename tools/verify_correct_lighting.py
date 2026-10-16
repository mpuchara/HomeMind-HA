"""Independent-source reproductions; synthetic observations, no HA dispatch."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def one(source):
    with tempfile.TemporaryDirectory(prefix='correct-lighting-') as directory:
        os.environ['ADAPTIVE_AI_DATA'] = directory
        sys.path[:0] = [str(source/'adaptive_ai/src'), str(source/'tests'), str(source)]
        from test_correct_multigeneration_history import CorrectGenerationHistoryTests
        from agent_correct_generation_history import build_correct_history
        from test_observation_contract import sensor_state, add_live
        from context import TemporalHistory
        from observation_contract import FeatureSchemaV12, _feature_observation
        from fast_runtime import stabilize_fast_light_power_decision
        from home_state import RoomBeliefModel
        from support import agent
        f = CorrectGenerationHistoryTests(); f.setUp()
        try:
            at = f.ts + 10
            with f.store.conn() as c:
                c.execute('DELETE FROM entity_history'); c.execute('DELETE FROM decision_history')
                c.executemany('INSERT INTO decision_history VALUES(?,?,?,?)',
                    [(f.root['id'],at+i,1,i%2) for i in range(10)])
            f.store.archive_batch([('light.history',at-10,'off',{},None,'test')])
            history = build_correct_history(f.manager, f.g0['generation_id'], at-1, at+10, None)
            current = history['series']['current']['points'][-1]['value']
            desired = [p['value'] for p in history['series']['live_desired']['points']]
            h = TemporalHistory(maxlen=24); states = {}; base = 1800000000.
            a = agent(); schema = FeatureSchemaV12(128,['sensor.room_lux'])
            def add(eid, value, offset, changed=None):
                st = sensor_state(eid,value,base+offset,
                    last_changed=base+changed if changed is not None else None,
                    unit='lx' if eid.endswith('lux') else None)
                add_live(h,st,base+offset); states[eid]=st
            def observe(offset):
                return _feature_observation(schema,'sensor.room_lux',states['sensor.room_lux'],
                    a,states,h,base+offset,base+offset)[0]
            add('light.kitchen','off',-3); add('sensor.room_lux',20,-2)
            add('light.kitchen','on',0,0); add('sensor.room_lux',200,1)
            initial = observe(1)
            for offset in range(2,301):
                add('sensor.room_lux',180+offset%20,offset)
                add('light.kitchen','on',offset,0)
            final = observe(301)
            off_value, confirming = stabilize_fast_light_power_decision(a,{},1,0,
                'hybrid_tiny_mlp_ridge_guard',base+301)
            belief = RoomBeliefModel()
            for offset in range(601):
                belief.observe('binary_sensor.kitchen','kitchen',1.,base+offset,
                               evidence={'role':'radar_occupancy'})
            presence = belief.forecast('kitchen',base+600)['occupancy_now']
            return dict(current_physical_on_with_archive_lag=current,
                desired_contains_actual_shadow_flips=set(desired)=={0,1},
                policy_replay_used=history['policy_replay_used'],
                raw_history_samples=len(h.samples['sensor.room_lux']),
                initial_ambient=initial['physical_value'], final_ambient=final['physical_value'],
                final_ambient_valid=bool(final['valid']), neural_short_off_value=off_value,
                neural_off_confirmation=confirming,
                continued_presence_after_600_seconds=presence,
                pass_all=current==1 and final['physical_value']==20 and bool(final['valid'])
                    and off_value==1 and confirming and set(desired)=={0,1} and presence>.8)
        finally:
            f.tearDown()


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--one',action='store_true');p.add_argument('--source-root',type=Path,default=ROOT)
    p.add_argument('--baseline-root',type=Path)
    p.add_argument('--check',action='store_true')
    a=p.parse_args()
    if a.one:
        result=one(a.source_root.resolve())
    else:
        if a.baseline_root is None:p.error('--baseline-root required')
        result=dict(synthetic=True,independent_sources=True,
            limit='Production history reader, feature extractor and OFF confirmation with constructed observed inputs. Does not establish the exact state of the user database or their fitted model, and never sends HA commands.')
        for key,source in [('before',a.baseline_root.resolve()),('after',ROOT)]:
            output=subprocess.check_output([sys.executable,str(Path(__file__).resolve()),'--one',
                '--source-root',str(source)],text=True)
            result[key]=json.loads(output.strip().splitlines()[-1])
        assert result['after']['pass_all'],result
    print(json.dumps(result,indent=None if a.one else 2))
    if a.check:
        assert result.get('pass_all'),result
