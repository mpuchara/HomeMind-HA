"""Dense overflow lookup transport only; not Pi end-to-end training timing."""
import hashlib,json,os,sys,tempfile,time
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
import support
from storage import Store
from observation_contract import FeatureJournal,ObservationTransitionEdgeIndex,ObservationSQLiteTemporalTracker
from detached_mapping_snapshots import DetachedMappingSnapshots

with tempfile.TemporaryDirectory() as folder:
    store=Store(Path(folder)/'dense.db');FeatureJournal(store)
    eid='binary_sensor.dense';base=1791900000.
    for lo in range(0,20000,1000):
        store.archive_batch([(eid,base+i,'on' if (i//37)%2 else 'off',{'device_class':'occupancy'},None,'fixture',base+i) for i in range(lo,lo+1000)])
    whole=ObservationTransitionEdgeIndex.build(store,[eid],base,base+20000,max_rows_per_entity=4096)
    assert whole.fallback_entities==[eid]
    lo,hi=base+15000,base+15300
    started=time.perf_counter();overlay=whole.for_chunk(store,lo,hi);build=time.perf_counter()-started
    assert not overlay.fallback_entities
    reports=[];signatures=[]
    for index in (whole,overlay):
        tracker=ObservationSQLiteTemporalTracker(store,[eid],SimpleNamespace(options={},relevant_entities=lambda:[]),lo,hi,transition_edge_index=index)
        rows=[];started=time.perf_counter()
        for i in range(400):
            at=lo+100+(i*17)%195
            rows.append((tracker.directional_transition_before(eid,at,bool(i%2),40),tracker.first_directional_transition_after(eid,at-40,at,not bool(i%2))))
        seconds=time.perf_counter()-started
        reports.append(dict(seconds=seconds,stats=tracker.stats()));signatures.append(hashlib.sha256(json.dumps(rows).encode()).hexdigest());tracker.close()
    assert signatures[0]==signatures[1]
    mapping={str(i):dict(state='on',attributes=dict(values=list(range(20)))) for i in range(780)}
    snapshots=[]
    for complete in (False,True):
        cache=DetachedMappingSnapshots(reuse_complete=complete);cache.capture(mapping)
        started=time.perf_counter()
        for i in range(80):result,_=cache.capture(dict(mapping))
        snapshots.append(dict(seconds=time.perf_counter()-started,stats=cache.snapshot()))
        assert result==mapping
    print(json.dumps(dict(component_only=True,causal_signature=signatures[0],queries=800,baseline=reports[0],chunk_overlay=reports[1],overlay_build_seconds=build,lookup_speedup=reports[0]['seconds']/reports[1]['seconds'],including_build_speedup=reports[0]['seconds']/(reports[1]['seconds']+build),state_snapshot_baseline=snapshots[0],state_snapshot_complete=snapshots[1],state_snapshot_speedup=snapshots[0]['seconds']/snapshots[1]['seconds'])))
