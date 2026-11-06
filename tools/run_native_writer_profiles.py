"""Serial native writer diagnostics on immutable disposable ext4 source trees."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    args = p.parse_args()
    root = args.root
    out = root/'writer-profiles'
    out.mkdir()
    arms = ('base', 'before', 'current', 'source')
    workloads = [('small50', '50:10', 'normal', 0),
                 ('normal', '1000:200', 'normal', 0),
                 ('small200', '200:30', 'normal', 0),
                 ('unsupported', '1000:200', 'busy', 200)]
    manifests = {arm: {str(f.relative_to(root/arm)): hashlib.sha256(f.read_bytes()).hexdigest()
                      for folder in ('adaptive_ai/src', 'tools')
                      for f in sorted((root/arm/folder).rglob('*.py'))} for arm in arms}
    hashes = [manifests[arm]['tools/benchmark_qualified_evidence.py'] for arm in arms]
    if len(set(hashes)) != 1:
        raise ValueError('identical compatible harness required')
    (out/'protocol.json').write_text(json.dumps(dict(diagnostic_only=True, ticks=24,
        period=.4, fanout=16, workloads=workloads, arms=arms, manifests=manifests), indent=2))
    for workload, scale, mode, candidates in workloads:
        for arm in arms:
            tree = root/arm
            name = workload+'-'+arm
            command = [sys.executable, str(root/'source/tools/profile_native_writer.py'),
                       '--source-root', str(tree), '--harness', str(tree/'tools/benchmark_qualified_evidence.py'),
                       '--profile-output', str(out/(name+'.profile.json')),
                       '--cases', scale, '--modes', mode, '--candidates', str(candidates),
                       '--ticks', '24', '--period', '.4', '--fanout', '16',
                       '--output', str(out/(name+'.jsonl'))]
            with (out/(name+'.log')).open('xb') as stream:
                try:
                    code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=420).returncode
                except subprocess.TimeoutExpired:
                    code = 124
            (out/(name+'.receipt.json')).write_text(json.dumps(dict(command=command, exit_code=code)))
            print(name, code, flush=True)
            if code:
                raise RuntimeError('diagnostic failed; preserve the incomplete arm')


if __name__ == '__main__':
    main()
