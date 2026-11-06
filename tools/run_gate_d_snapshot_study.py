"""Serial, immutable-output Gate D study; run only on disposable WSL ext4 trees."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)


def manifest(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
            for folder in ('adaptive_ai/src', 'tools') for p in sorted((root/folder).rglob('*.py'))}


def run(root, out, name, scale='1000:200', mode='normal', candidates=0, profile=False, cpu=False):
    command = [sys.executable, str(root/'tools/benchmark_qualified_evidence.py'),
               '--cases',scale,'--modes',mode,'--candidates',str(candidates),
               '--ticks','24','--period','.4','--fanout','16','--output',str(out/(name+'.jsonl'))]
    if profile:
        command += ['--profile-prepare']
    env = dict(os.environ, HOMEMIND_PREPARATION_CPU_PROFILE='1' if cpu else '0')
    started = time.monotonic()
    with (out/(name+'.log')).open('xb') as log:
        try:
            code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=420).returncode
        except subprocess.TimeoutExpired:
            code = 124
    receipt = dict(name=name, command=command, exit_code=code, elapsed=time.monotonic()-started)
    save(out/(name+'.receipt.json'), receipt)
    print(json.dumps(receipt), flush=True)
    if code:
        return {'error':receipt}
    lines = (out/(name+'.jsonl')).read_text().splitlines()
    return json.loads(lines[-1])


def compare(left, right):
    if 'error' in left or 'error' in right:
        return dict(status='FAILED_RUN')
    ratios = {key:right[field]/left[field]-1 for key,field in (
        ('cpu','process_cpu_seconds'),('rss','rss_after_mb'))}
    ratios.update({key:right['event_to_intent'][key+'_ms']/left['event_to_intent'][key+'_ms']-1
                   for key in ('p95','p99')})
    parity = (left['input_fingerprint']==right['input_fingerprint'] and
              left['decision_signature']==right['decision_signature'] and
              left['original_intents']==right['original_intents']==24*left['fanout'])
    safe = all(not r['ha_service_calls'] and not r['collector']['errors']
               and not r['collector']['dropped'] and not r['event']['dropped']
               for r in (left,right))
    limits = dict(p95=.10,p99=.15,cpu=.15,rss=.10)
    return dict(status='PASS' if parity and safe and all(ratios[k]<=v for k,v in limits.items()) else 'FAIL',
                ratios=ratios, parity=parity, safe=safe,
                qualified=[r['dataset']['eligible'] for r in (left,right)],
                consumed=[r['collector']['predecision']['consumed'] if 'predecision' in r['collector'] else 0
                          for r in (left,right)],
                rejection_reasons=[r['prepare_error_reasons'] for r in (left,right)])


def main():
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('--phase',choices=('profiles','matrix','variance'),required=True)
    args=p.parse_args();root=args.root;out=root/args.phase;out.mkdir()
    trees={name:root/name for name in ('base','before','source')}
    plan=[(f'normal-{i+1}','1000:200','normal',0,bool(i%2)) for i in range(6)] + [
        ('busy128','1000:128','busy',128,False),('busy200','1000:200','busy',56,True),
        ('off','1000:200','off',0,False),('incomplete','1000:200','incomplete',0,True),
        ('unsupported','1000:200','busy',200,False),
        ('small50','50:10','normal',0,True),('small200','200:30','normal',0,False)]
    if args.phase=='variance':
        plan=[(f'variance-{i+1}','1000:200','normal',0,bool(i%2)) for i in range(6)]
    save(out/'protocol.json',dict(baseline='951dcd70ed20b5f74220307cfb7411fa2c6bcc5d',
        candidate='51b49b91',ticks=24,period=.4,fanout=16,plan=plan,
        limits=dict(p95=.10,p99=.15,cpu=.15,rss=.10),
        throughput='>=2x aggregate consumed AND qualified; improvement in every required supported case',
        profiles_separate=True,serial=True,phase=args.phase))
    save(out/'source-manifests.json',{name:manifest(path) for name,path in trees.items()})
    results=[]
    if args.phase=='profiles':
        for name in ('before','base','source'):
            run(trees[name],out,name+'-light',profile=True)
        for name in ('before','source'):
            run(trees[name],out,name+'-cpu',profile=True,cpu=True)
        return
    for name,scale,mode,candidates,reverse in plan:
        rows={}
        for arm in (('source','base') if reverse else ('base','source')):
            tree=trees['source'] if args.phase=='variance' else trees[arm]
            rows[arm]=run(tree,out,name+'-'+arm,scale,mode,candidates)
        result=dict(name=name,**compare(rows['base'],rows['source']))
        results.append(result)
        save(out/(name+'-comparison.json'),result)
        print(json.dumps(result),flush=True)
    save(out/'summary.json',results)


if __name__=='__main__':main()
