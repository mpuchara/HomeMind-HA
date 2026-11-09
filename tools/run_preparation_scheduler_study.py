"""Run the six Normal same-revision pairs with opt-in scheduler tracing."""
import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from run_gate_d_snapshot_study import manifest, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--pairs', type=int, default=6)
    parser.add_argument('--contenders', type=int, choices=range(9), default=0)
    parser.add_argument('--stream-only', action='store_true')
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--pause-tick', type=int)
    args = parser.parse_args()
    cpu = None
    if args.contenders:
        cpu = min(os.sched_getaffinity(0))
        os.sched_setaffinity(0, {cpu})
    args.output.mkdir()
    trees = {'base': args.source, 'source': args.candidate or args.source}
    probe = Path(__file__).with_name('trace_evidence_availability.py')
    save(args.output/'protocol.json', dict(
        source='9c245577727c93ede3c59100f11844bf336acb1a', manifest=manifest(args.source),
        probes={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (probe, probe.with_name('trace_preparation_scheduler.py'))},
        pairs=args.pairs, diagnostic_only=True, affinity_cpu=cpu, contenders=args.contenders,
        stream_only=args.stream_only, pause_tick=args.pause_tick,
        role_manifests={role: manifest(tree) for role, tree in trees.items()}))
    for index in range(args.pairs):
        for role in (('base', 'source') if index % 2 == 0 else ('source', 'base')):
            prefix = args.output/f'normal-{index+1}-{role}'
            command = [sys.executable, str(probe), str(trees[role]), str(prefix.with_suffix('.trace.json')),
                       '--scheduler', '--cases', '1000:200', '--modes', 'normal', '--candidates', '0',
                       '--ticks', '24', '--period', '.4', '--fanout', '16',
                       '--output', str(prefix.with_suffix('.jsonl'))]
            if args.stream_only:
                command += ['--stream-contenders', str(args.contenders)]
            if args.pause_tick is not None:
                command += ['--pause-tick', str(args.pause_tick)]
            before = os.getloadavg()
            with prefix.with_suffix('.log').open('xb') as log:
                burners = []
                try:
                    for _ in range(0 if args.stream_only else args.contenders):
                        burners.append(subprocess.Popen([sys.executable, '-c', 'while True: pass']))
                    code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=420).returncode
                except subprocess.TimeoutExpired:
                    code = 124
                finally:
                    for burner in burners:
                        burner.terminate()
                    for burner in burners:
                        burner.wait()
            save(prefix.with_suffix('.receipt.json'), dict(command=command, exit_code=code,
                                                             load_before=before, load_after=os.getloadavg()))
            print(prefix.name, code, flush=True)
            if code:
                return code
    return 0


if __name__ == '__main__':
    sys.exit(main())
