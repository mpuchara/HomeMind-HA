"""Run serial U05b.4c.1e comparisons with one common harness and immutable outputs."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from benchmark_runtime_recovery import quantiles, source_identity


def write_json(path, data):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')


def run_case(root, out, name, mode, sensors, agents, probe=False):
    log = out / (name + '.log')
    args = [sys.executable, str(Path(__file__).with_name('benchmark_runtime_scaling.py')),
            '--source-root', str(root), '--agents', str(agents), '--sensor-count', str(sensors),
            '--warmup', '8', '--events', '40', '--candidates', '--correct', '--training', '--collector']
    if mode == 'matched':
        args += ['--matched-updates', '64', '--matched-writes', '2']
    if probe:
        args += ['--probe']
    with log.open('xb') as stream:
        try:
            code = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, timeout=900).returncode
        except subprocess.TimeoutExpired:
            code = 124
    text = log.read_bytes()
    row = dict(name=name, mode=mode, sensors=sensors, agents=agents, probe=probe,
               deadline_seconds=30, exit_code=code, log_sha256=hashlib.sha256(text).hexdigest(),
               command=args)
    encoded = next((line.partition(b'=')[2] for line in text.splitlines()
                    if line.startswith(b'SCALING_JSON=')), None)
    if code == 0 and encoded:
        with gzip.GzipFile(filename='', mode='wb', fileobj=(out / (name + '.json.gz')).open('xb'),
                           mtime=0) as stream:
            stream.write(encoded)
        data = json.loads(encoded)
        row.update({key: data[key] for key in ('input_sha256', 'decisions_sha256', 'background_work',
                   'candidate_count', 'ha_calls', 'process_cpu_ms', 'wall_ms', 'rss_mb', 'peak_rss_mb',
                   'collector', 'matched_workload')})
        row['warmup'] = quantiles(data['warmup_pipeline_samples_ms'])
        row['event_to_intent'] = quantiles(data['telemetry_raw_ms']['event_to_intent'])
        row['raw_sha256'] = hashlib.sha256(encoded).hexdigest()
        row['status'] = 'completed'
    else:
        row['status'] = 'failed'
        row['error_tail'] = text.decode(errors='replace')[-4000:]
    write_json(out / (name + '.summary.json'), row)
    print(name, row['status'], row.get('event_to_intent'), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('baseline', 'audit', 'current', 'output'):
        parser.add_argument('--' + arg, type=Path, required=True)
    parser.add_argument('--series', choices=('primary', 'extension', 'diagnostic'), default='primary')
    args = parser.parse_args()
    roots = {name: getattr(args, name).resolve() for name in ('baseline', 'audit', 'current')}
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    if args.series == 'primary':
        groups = [('matched', 1000, 200, i) for i in range(1, 5)]
        groups += [('natural', 1000, 200, i) for i in range(1, 3)]
        groups += [(mode, s, a, 1) for s, a in [(200, 30), (50, 10), (5, 1)]
                   for mode in ('matched', 'natural')]
    elif args.series == 'extension':
        groups = [('matched', 1000, 200, i) for i in (5, 6)]
    else:
        groups = [('matched', 200, 30, 1), ('matched', 1000, 200, 1)]
    plan = []
    for mode, sensors, agents, pair in groups:
        variants = ['baseline', 'current'] if pair % 2 else ['current', 'baseline']
        if args.series == 'diagnostic':
            variants.insert(1, 'audit')
        for variant in variants:
            name = f'{mode}-{sensors}-{agents}-{pair}-{variant}'
            plan.append(dict(name=name, variant=variant, mode=mode, sensors=sensors, agents=agents))
    harness = Path(__file__).with_name('benchmark_runtime_scaling.py')
    write_json(out / 'plan.json', dict(series=args.series, cases=plan,
               harness_sha256=hashlib.sha256(harness.read_bytes()).hexdigest(),
               sources={name: source_identity(root) for name, root in roots.items()}))
    for case in plan:
        run_case(roots[case['variant']], out, case['name'], case['mode'], case['sensors'],
                 case['agents'], probe=args.series == 'diagnostic')


if __name__ == '__main__':
    main()
