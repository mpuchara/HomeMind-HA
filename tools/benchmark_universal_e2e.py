"""HOST SYNTHETIC original Engine V2 evidence -> V3 -> qualified offline artifact.

Same producer/harness for either --source-root. Default ValidationPolicy and
fixed train-only config; insufficient large cohorts remain insufficient. No HA,
live model, automatic trainer or retrospective timestamp edits.
"""
import argparse
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from unittest.mock import patch

from universal_e2e_fixture import OriginalEvidenceScenario
from runtime_scaling_probe import Probe


def case(root,sensors,devices):
    sys.path.insert(0,str(root/'adaptive_ai/src'))
    for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[key]='1'
    with tempfile.TemporaryDirectory(prefix='hm-native-e2e-') as folder,ExitStack() as stack:
        os.environ.setdefault('ADAPTIVE_AI_DATA',str(Path(folder)/'bootstrap'))
        from storage import Store
        from telemetry import rss_mb
        from universal_checkpoint_lifecycle import CheckpointLifecycle
        from universal_scorer_training import TrainingConfig,context_from_sample
        from universal_scorer_validation import ValidationPolicy
        import universal_checkpoint_lifecycle as lifecycle
        import numpy
        policy=ValidationPolicy();config=TrainingConfig(epochs=24)
        timings={};probe=Probe(capacity=64)
        def measure(name,action):
            before={k:v['count'] for k,v in probe.snapshot()['operations'].items()}
            wall=time.perf_counter();cpu=time.process_time();rss=rss_mb()
            value=action()
            after=probe.snapshot()['operations']
            timings[name]=dict(wall_ms=(time.perf_counter()-wall)*1000,cpu_ms=(time.process_time()-cpu)*1000,
                rss_before_mb=rss,rss_after_mb=rss_mb(),
                native_sqlite_calls=sum(v['count']-before.get(k,0) for k,v in after.items() if k.startswith('sqlite_execute.')))
            return value
        store=Store(Path(folder)/'native.db')
        scenario=measure('setup',lambda:OriginalEvidenceScenario(store,sensors=sensors,devices=devices))
        timings['setup']['native_sqlite_calls']=None  # Probe is installed below.
        stack.callback(scenario.close)
        probe.install(stack,scenario.engine,store)
        # Every device is included. These bounded small cohorts deliberately do
        # not manufacture the 48/57/72 future units required per larger slice.
        samples=max(128,devices)
        measure('original_event_collection',lambda:scenario.collect_events(samples))
        source=measure('native_source_export',scenario.source)
        def dataset():
            with store.conn() as c:c.execute('BEGIN');return source.dataset(c)
        data=measure('training_dataset',dataset)
        doc=json.loads(source.raw)
        assert len(data.samples)==samples and not data.rejected,data.rejected
        assert len({s.target_device for s in data.samples})==devices
        assert len(doc['agent_ids'])==devices
        assert doc['coverage']['version']==3 and doc['coverage']['historical_sensor_universe_count']==sensors
        assert all(row['receipt']['version']==2 for row in doc['decisions'])
        service=CheckpointLifecycle(store)
        trained=measure('neural_training',lambda:service.rebuild(source,parent_seed=1,encoder_seed=41,
            **scenario.split(),config=config))
        assert trained['status']=='optimization_complete',trained
        assert trained['updates']>0
        validate=lifecycle.validate_core_artifact
        def measured_validation(*args,**kwargs):return measure('quality_gate_inside_publication',lambda:validate(*args,**kwargs))
        with patch.object(lifecycle,'validate_core_artifact',side_effect=measured_validation):
            published=measure('publication_total',lambda:service.publish(trained['checkpoint'],source,policy=policy))
        assert published['status']==('published' if devices==1 else 'insufficient_evidence'),published
        artifact=None;projection=None
        if published['status']=='published':
            aid=published['artifact_id']
            with store.conn() as c:artifact=json.loads(c.execute('SELECT artifact FROM universal_validated_checkpoints WHERE id=?',(aid,)).fetchone()[0])
            loaded=measure('guarded_load',lambda:CheckpointLifecycle(Store(store.path)).load(aid,source))
            context=context_from_sample(data.samples[-1]);assert not hasattr(context,'desired_action')
            projection=measure('guarded_offline_inference',lambda:loaded.rank_context(context))
            assert not projection['control_eligible'] and not projection['promotion_eligible']
            report=artifact['report']
        else:report=published['report']
        with store.conn() as c:
            observation_count=c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
            original_bytes=c.execute('SELECT SUM(LENGTH(body)) FROM universal_collection_decisions').fetchone()[0]
            receipt_count=c.execute('SELECT COUNT(*) FROM universal_collection_decisions').fetchone()[0]
            provenance_count=c.execute('SELECT COUNT(*) FROM provenance_decisions').fetchone()[0]
            claims=c.execute('SELECT COUNT(*) FROM universal_checkpoint_holdouts').fetchone()[0]
        assert receipt_count==provenance_count==samples
        assert scenario.ha.call_count==0
        counts=scenario.collector.diagnostics();assert counts['dropped']==0 and counts['errors']==0,counts
        physical_files={p.name:p.stat().st_size for p in Path(folder).glob('native.db*')}
        digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        result=dict(synthetic=True,sensors=sensors,devices=devices,planned_trials=samples,
            eligible=len(data.samples),rejected=list(data.rejected),dataset_id=data.dataset_id,
            supervision_revision=data.supervision_revision,source_id=source.source_id,
            history_source_revision=doc['coverage']['source_revision'],coverage_version=3,receipt_version=2,
            source_bytes=len(source.raw),native_original_receipt_bytes=original_bytes,
            original_intents=receipt_count,native_provenance=provenance_count,observations=observation_count,
            ha_calls=scenario.ha.call_count,collector=counts,timings=timings,
            optimization_status=trained['status'],updates=trained['updates'],feature_bytes=trained['feature_bytes'],
            encoder_id=trained['checkpoint']['encoder_id'],scorer_weights_id=digest(trained['checkpoint']['scorer']),
            training_config=asdict(config),policy=asdict(policy),split=scenario.split(),
            validation=report,publication_status=published['status'],artifact_id=published.get('artifact_id'),
            artifact_bytes=len(json.dumps(artifact).encode()) if artifact else None,holdout_claims=claims,
            guarded_inference=projection,sqlite_files=physical_files,database_family_bytes=sum(physical_files.values()),
            storage=store.sqlite_snapshot(),probe=probe.snapshot(),
            versions=dict(python=sys.version,numpy=numpy.__version__,platform=platform.platform()),
            limits='Synchronous sparse synthetic producer, not production yield. Distinct trial identities/time blocks are scenario assumptions, not evidence of household independence. Native SQL probes omit direct in-memory arbiter-copy statements and setup; timings include equal probe overhead. Validation is nested in publication. No artifact/load/inference for insufficient cohorts. No Pi/real HA acceptance.')
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cases',default='5:1,50:10,200:30,1000:200')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();results=[]
    with tempfile.TemporaryDirectory(prefix='hm-e2e-bootstrap-') as bootstrap:
        # Keep module-level default Store isolated AND alive across every case.
        os.environ['ADAPTIVE_AI_DATA']=bootstrap
        for item in args.cases.split(','):
            sensors,devices=map(int,item.split(':'))
            if not 5<=sensors<=1000 or not 1<=devices<=200:parser.error('bounded cardinalities required')
            print(f'CASE {sensors}/{devices}',file=sys.stderr,flush=True)
            results.append(case(args.source_root.resolve(),sensors,devices))
    raw=json.dumps(dict(scope='HOST synthetic original V2/V3 full lifecycle',cases=results),indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(raw,encoding='utf-8')
    print(raw)


if __name__=='__main__':main()
