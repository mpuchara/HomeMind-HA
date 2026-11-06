"""Serial allocation/remaining-cost diagnostics after the acceptance matrix."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('tools',type=Path)
    args=p.parse_args();root=args.root;out=root/'supplement';out.mkdir()
    for arm in ('base','before','source'):
        shutil.copytree(root/arm,out/arm)
        shutil.copyfile(args.tools/'benchmark_qualified_evidence.py',out/arm/'tools/benchmark_qualified_evidence.py')
    jobs=[('base','1000:200','normal',0,False),
          ('base','50:10','normal',0,False),('before','50:10','normal',0,False),('source','50:10','normal',0,False),
          ('base','1000:200','busy',200,False),('source','1000:200','busy',200,False),
          ('base','50:10','normal',0,True),('source','50:10','normal',0,True)]
    for index,(arm,scale,mode,candidates,cpu) in enumerate(jobs):
        name=f'{index}-{arm}';env=dict(os.environ,HOMEMIND_COLLECTOR_CPU_PROFILE='1' if cpu else '0')
        command=[sys.executable,str(out/arm/'tools/benchmark_qualified_evidence.py'),
                 '--cases',scale,'--modes',mode,'--candidates',str(candidates),'--ticks','24',
                 '--period','.4','--fanout','16','--profile-prepare','--output',str(out/(name+'.jsonl'))]
        with (out/(name+'.log')).open('xb') as stream:
            code=subprocess.run(command,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=420).returncode
        print(name,code,flush=True)
    for arm in ('before','source'):
        command=[sys.executable,str(args.tools/'profile_preparation_memory.py'),
            '--source-root',str(out/arm),'--output',str(out/(arm+'-memory-case.jsonl')),
            '--memory-output',str(out/(arm+'-memory.json'))]
        with (out/(arm+'-memory.log')).open('xb') as stream:
            code=subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,timeout=420).returncode
        print(arm,'memory',code,flush=True)
    # Run the isolated full-copy candidate only after all load diagnostics.
    experiment=out/'source/tools/experiment_bounded_snapshot_copy.py'
    shutil.copyfile(args.tools/experiment.name,experiment)
    with (out/'copy-experiment.json').open('xb') as stream:
        env=dict(os.environ,ADAPTIVE_AI_DATA=str(out/'copy-bootstrap'))
        code=subprocess.run([sys.executable,str(experiment)],env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=120).returncode
    print('copy-experiment',code,flush=True)


if __name__=='__main__':main()
