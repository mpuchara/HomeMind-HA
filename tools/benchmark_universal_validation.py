"""Synthetic HOST ONLY U05b.2 evidence-gate scenarios; never a Pi4 acceptance run."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[name]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adaptive_ai/src'))
from universal_training_fixture import make_dataset
from universal_action_scorer import UniversalActionScorer
from universal_neural_encoder import SharedSetEncoder
from universal_scorer_training import TrainingConfig,train_scorer
from universal_scorer_validation import validate_core_artifact,ValidationPolicy


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',default='5:1:128,50:10:128,200:30:180,1000:200:200')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result={'scope':'synthetic host-only offline evidence gate; no Pi4/HA/publication acceptance',
            'policy':{'family_alpha':.05,'accuracy_half_width':.25,'independence_window_seconds':60.},
            'cases':[]}
    for case in args.cases.split(','):
        sensors,devices,samples=map(int,case.split(':'))
        dataset,split=make_dataset(sensors=sensors,devices=devices,samples=samples,step_seconds=120.)
        training=train_scorer(UniversalActionScorer(seed=1),SharedSetEncoder(),dataset,**split,
                              config=TrainingConfig(epochs=24))
        if training['status']!='optimization_complete':raise RuntimeError(str(training))
        wall,cpu=time.perf_counter(),time.process_time()
        report=validate_core_artifact(training['checkpoint'],dataset,policy=ValidationPolicy())
        cpu,wall=time.process_time()-cpu,time.perf_counter()-wall
        result['cases'].append(dict(sensors=sensors,devices=devices,samples=samples,
            training_seconds=training['timing']['total_seconds'],validation_seconds=wall,
            validation_cpu_seconds=cpu,report=report))
        if devices==1 and report['status']!='passed':raise AssertionError(str(report))
        if devices>1 and report['status']!='insufficient_evidence':raise AssertionError(str(report))
    encoded=json.dumps(result,indent=2,allow_nan=False)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded)
    print(encoded)


if __name__=='__main__':main()
