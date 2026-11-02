"""Synthetic HOST ONLY U05b.1 optimization/encoding benchmark, no HA/Pi4 claim."""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time

# Standalone benchmark subprocess, before NumPy import, matching bounded BLAS use.
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'adaptive_ai/src'))
from universal_action_scorer import UniversalActionScorer
from universal_neural_encoder import SharedSetEncoder
from universal_scorer_training import TrainingConfig, train_scorer, checkpoint_bytes
from universal_training_fixture import make_dataset


def peak_rss_mb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024.
    except ImportError:
        return None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenarios',default='5:1:64,50:10:80,200:30:120,1000:200:200')
    parser.add_argument('--epochs',type=int,default=24)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    report={'scope':'synthetic host-only offline core; no runtime/HA/Pi4 acceptance',
            'trained':False,'validation_status':'not_validated','scenarios':[]}
    for scenario in args.scenarios.split(','):
        sensors,devices,samples=map(int,scenario.split(':'))
        dataset,split=make_dataset(sensors=sensors,devices=devices,samples=samples)
        cpu=time.process_time();peak_before=peak_rss_mb()
        result=train_scorer(UniversalActionScorer(seed=1),SharedSetEncoder(),dataset,**split,
                            config=TrainingConfig(epochs=args.epochs))
        cpu=time.process_time()-cpu
        if result['status']!='optimization_complete':
            raise RuntimeError(str(result))
        timings=[]
        for sample in dataset.samples[-8:]:
            started=time.perf_counter();result['model'].scores(sample)
            timings.append((time.perf_counter()-started)*1000)
        row=dict(sensors=sensors,devices=devices,samples=samples,updates=result['updates'],
                 feature_bytes=result['feature_bytes'],process_cpu_seconds=cpu,
                 peak_rss_before_mb=peak_before,peak_rss_after_mb=peak_rss_mb(),
                 checkpoint_bytes=len(checkpoint_bytes(result['checkpoint'])),
                 prediction={'samples':len(timings),'includes_full_frozen_encoding':True,
                             'p50_ms':statistics.median(timings),'max_ms':max(timings)},
                 before=result['checkpoint']['before'],after=result['checkpoint']['after'],
                 timing=result['timing'])
        report['scenarios'].append(row)
    encoded=json.dumps(report,indent=2,allow_nan=False)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded)
    print(encoded)


if __name__=='__main__':
    main()
