"""Serial alternating paired runs; persist every raw sample before summarizing."""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--counts', default='0,1,3,4,5,10,20')
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--events', type=int, default=20)
    parser.add_argument('--scheduler', action='store_true')
    parser.add_argument('--diagnostics', action='store_true', help='Same current sources, tracing off versus on')
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error('repeats must be 1..10')
    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    summary = []
    for count in [int(x) for x in args.counts.split(',')]:
        for independent in ([False] if args.scheduler else [False, True]):
            pairs = []
            for repeat in range(args.repeats):
                pair = {}
                for label in (['before','after'] if repeat%2 == 0 else ['after','before']):
                    source = args.baseline_root if label == 'before' and not args.diagnostics else root
                    command = [sys.executable, str(root/'tools/benchmark_runtime_scaling.py'),
                               '--source-root', str(source), '--agents', str(count), '--events', str(args.events)]
                    if independent:
                        command.append('--independent')
                    if args.scheduler:
                        command.append('--scheduler')
                    if args.diagnostics and label == 'after':
                        command.append('--debug')
                    proc = subprocess.run(command, text=True, capture_output=True, timeout=240)
                    name = f'{count}-{"independent" if independent else "shared"}-{repeat}-{label}'
                    (args.output/(name+'.log')).write_text(proc.stdout+proc.stderr)
                    if proc.returncode:
                        raise RuntimeError(f'{name}: exit {proc.returncode}; see raw log')
                    line = next(line for line in reversed(proc.stdout.splitlines()) if line.startswith('SCALING_JSON='))
                    pair[label] = json.loads(line.split('=',1)[1])
                    (args.output/(name+'.json')).write_text(json.dumps(pair[label],indent=2,sort_keys=True))
                key = 'snapshot_sha256' if args.scheduler else 'decisions_sha256'
                parity = pair['before'][key] == pair['after'][key]
                pairs.append(dict(repeat=repeat, parity=parity, **pair))
                print(f'{count} independent={independent} repeat={repeat} parity={parity}', flush=True)
                if not parity:
                    raise AssertionError(f'Decision/snapshot parity failed: {name}')
            def percentile(values, p):
                values = sorted(values)
                return values[min(len(values)-1,int((len(values)-1)*p))] if values else None
            row = dict(agents=count, independent=independent, repeats=args.repeats, parity=True)
            for label in ('before','after'):
                samples = [value for pair in pairs for value in pair[label]['samples_ms' if args.scheduler else 'samples']]
                row[label] = dict(samples=len(samples), p50_ms=percentile(samples,.5),
                    p95_ms=percentile(samples,.95), p99_ms=percentile(samples,.99),
                    p99_sufficient=len(samples)>=1000,
                    series_p95_ms=[percentile(pair[label]['samples_ms' if args.scheduler else 'samples'],.95) for pair in pairs])
                if args.scheduler:
                    row[label]['thread_cpu_median_ms'] = statistics.median(v for pair in pairs for v in pair[label]['thread_cpu_ms'])
                else:
                    metrics = [pair[label]['telemetry_raw_ms'].get('event_to_decision',[]) for pair in pairs]
                    decision_samples = [value for values in metrics for value in values]
                    row[label]['event_to_decision'] = dict(samples=len(decision_samples),
                        p50_ms=percentile(decision_samples,.5),p95_ms=percentile(decision_samples,.95),
                        p99_ms=percentile(decision_samples,.99), p99_sufficient=len(decision_samples)>=1000)
            summary.append(row)
            (args.output/'summary.json').write_text(json.dumps(summary,indent=2))


if __name__ == '__main__':
    main()
