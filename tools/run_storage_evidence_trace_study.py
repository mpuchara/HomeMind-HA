"""Run serial diagnostic comparisons with a frozen acceptance harness."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from run_gate_d_snapshot_study import manifest, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    out = args.root/'traces'
    out.mkdir()
    trees = {name: args.root/'path-study'/name for name in ('before', 'source')}
    plans = [('normal', '1000:200', ('before', 'source', 'source', 'before', 'before', 'before')),
             ('small50', '50:10', ('before', 'source'))]
    save(out/'protocol.json', dict(plan=plans, manifests={name: manifest(tree) for name, tree in trees.items()}))
    probe = Path(__file__).with_name('trace_evidence_availability.py')
    for workload, scale, arms in plans:
        for index, arm in enumerate(arms, 1):
            label = f'{workload}-{index}-{arm}'
            command = [sys.executable, str(probe), str(trees[arm]), str(out/(label+'.trace.json')),
                       '--cases', scale, '--modes', 'normal', '--candidates', '0',
                       '--ticks', '24', '--period', '.4', '--fanout', '16',
                       '--output', str(out/(label+'.jsonl'))]
            load_before = os.getloadavg()
            with (out/(label+'.log')).open('xb') as log:
                code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=420).returncode
            save(out/(label+'.receipt.json'), dict(command=command, exit_code=code,
                                                  load_before=load_before, load_after=os.getloadavg()))
            print(label, code, flush=True)
            if code:
                return code
    return 0


if __name__ == '__main__':
    sys.exit(main())
