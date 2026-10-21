import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from support import ROOT
from telemetry import Telemetry
from replay import ReplayQueryCache
from storage import Store
from observation_contract import FeatureJournal, ObservationTransitionEdgeIndex, ObservationSQLiteTemporalTracker
from training_process import parallel_profile_options, resolve_training_resource_profile
from settings import DEFAULT_OPTIONS
from detached_mapping_snapshots import DetachedMappingSnapshots


class Performance223Tests(unittest.TestCase):
    def test_logged_headroom_admits_both_workers_above_previous_oom(self):
        memory={'total_mb':7806.3,'available_mb':5317.9}
        profile=resolve_training_resource_profile(parallel_profile_options(DEFAULT_OPTIONS,2,memory),memory)
        self.assertEqual(profile['effective_memory_limit_mb'],1536)
        self.assertEqual(profile['worker_options']['training_worker_effective_sqlite_cache_mb'],128)
        self.assertLessEqual(2*profile['effective_memory_limit_mb']+512,memory['available_mb'])
        custom=dict(DEFAULT_OPTIONS,training_parallel_worker_memory_limit_mb=640)
        self.assertEqual(resolve_training_resource_profile(parallel_profile_options(custom,2,memory),memory)['effective_memory_limit_mb'],640)

    def test_empty_replay_results_cannot_grow_persistent_cache_without_bound(self):
        cache=ReplayQueryCache(max_rows=32)
        for i in range(1000):cache.put('SELECT ?', (i,), [])
        self.assertEqual(cache.status()['rows'],0)
        self.assertEqual(cache.status()['entries'],32)
        self.assertEqual(cache.get('SELECT ?', (999,)),[])
        self.assertIsNone(cache.get('SELECT ?', (0,)))
        cache.put('SELECT ?', (999,), [{'id':1}]);self.assertEqual(cache.rows,1)
        cache.put('SELECT ?', (999,), []);self.assertEqual(cache.rows,0)

    def test_trend_is_bounded_and_idle_windows_are_gaps(self):
        clock=[0.]
        with patch('telemetry.time.monotonic',side_effect=lambda:clock[0]):
            telemetry=Telemetry();telemetry.observe('event_to_decision',123)
            first=telemetry.snapshot()['metrics']['event_to_decision']['trend']
            self.assertEqual(first,[{'age_seconds':0.,'p95_ms':123.,'count':1}])
            telemetry.snapshot();self.assertEqual(len(telemetry.trends['event_to_decision']),1)
            for i in range(1,150):clock[0]=i*5.;telemetry.snapshot()
            final=telemetry.snapshot()['metrics']['event_to_decision']['trend']
            self.assertLessEqual(len(final),121)
            self.assertIsNone(final[-1]['p95_ms']);self.assertEqual(final[-1]['count'],0)
            self.assertEqual(telemetry.counts['event_to_decision'],1)

    def test_shared_state_frames_stay_private_and_in_place_corrections_are_detected(self):
        cache=DetachedMappingSnapshots(reuse_complete=True)
        source={'sensor.a':{'attributes':{'nested':[1,2]},'state':'on'}}
        first,_=cache.capture(source);second,_=cache.capture(dict(source))
        self.assertEqual(cache.snapshot()['complete_hits'],1)
        source['sensor.a']['attributes']['nested'][0]=9
        third,_=cache.capture(source)
        self.assertEqual(first['sensor.a']['attributes']['nested'],[1,2])
        self.assertEqual(third['sensor.a']['attributes']['nested'],[9,2])
        source['sensor.a']=dict(source['sensor.a'],state='off')
        fourth,_=cache.capture(source);self.assertEqual(fourth['sensor.a']['state'],'off')

    def test_dense_chunk_overlay_has_exact_legacy_results_and_is_not_retained_as_full_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'dense.db');journal=FeatureJournal(store)
            eid='binary_sensor.dense';base=1791900000.
            store.archive_batch([(eid,base+i,'on' if (i//37)%2 else 'off',{'device_class':'occupancy'},None,'fixture',base+i) for i in range(3200)])
            journal.record_batch([
                dict(entity_id=eid,state={'state':value,'attributes':{'device_class':'occupancy'}},
                     event_time=base+event,received_time=base+received,source='ha_state_changed',event_key=str(event))
                for event,received,value in ((1790,1900,'on'),(1820,1950,'unavailable'),(2290,2400,'off'))
            ])
            whole=ObservationTransitionEdgeIndex.build(store,[eid],base,base+3200,max_rows_per_entity=800)
            self.assertEqual(whole.fallback_entities,[eid])
            context=SimpleNamespace(options={},relevant_entities=lambda:[])
            for start in (base+1800,base+2300):
                overlay=whole.for_chunk(store,start,start+200)
                self.assertTrue(overlay.chunk_overlay);self.assertEqual(overlay.fallback_entities,[])
                self.assertLessEqual(len(overlay.records[eid]),800)
                old=ObservationSQLiteTemporalTracker(store,[eid],context,start,start+200)
                new=ObservationSQLiteTemporalTracker(store,[eid],context,start,start+200,transition_edge_index=overlay)
                try:
                    for at in (start+30,start+150,start+80,start+195):
                        for positive in (True,False):
                            self.assertEqual(old.directional_transition_before(eid,at,positive,50),new.directional_transition_before(eid,at,positive,50))
                            self.assertEqual(old.first_directional_transition_after(eid,at-50,at,positive),new.first_directional_transition_after(eid,at-50,at,positive))
                    self.assertGreater(new.stats()['transition_edge_index_hits'],0)
                finally:old.close();new.close()
            self.assertEqual(whole.records,{})
            self.assertEqual(whole.fallback_entities,[eid])

    def test_real_sparkline_renderer(self):
        result=subprocess.run(['node','tools/verify_latency_sparkline.js'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
