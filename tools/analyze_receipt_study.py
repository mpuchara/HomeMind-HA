"""Summarize phase-scoped diagnostics without promoting them to acceptance."""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    profiles = {}
    for path in sorted((args.root/'profiles').glob('*.profile.json')):
        name = path.name.removesuffix('.profile.json')
        profile = load(path)
        case = json.loads(path.with_name(name+'.jsonl').read_text().splitlines()[-1])
        phases = {}
        for phase in ('setup','load','finalization'):
            rows = {key.split('/',1)[1]:value for key,value in profile['rows'].items() if key.startswith(phase+'/')}
            writer = rows.get('universal_history_collector.HistoricalTrainingCollector._write', [0,0,0])
            captures = rows.get('universal_training_data.HistoricalDecision.capture', [0,0,0])
            phases[phase] = dict(writer_calls=writer[0], writer_cpu_ms=writer[1]/1e6,
                cpu_ms_per_batch=writer[1]/1e6/writer[0] if writer[0] else None,
                capture_calls=captures[0],
                cpu_ms_per_capture=captures[1]/1e6/captures[0] if captures[0] else None,
                exclusive_top=sorted(rows.items(), key=lambda row:-row[1][2])[:30])
        profiles[name] = dict(phases=phases, counts=case['history']['counts'],
            dataset=case['dataset'], sql_trace_events=profile['sql_trace_events'],
            source_revision_counts=profile.get('source_revision_counts'),
            declaration_revision_counts=profile.get('declaration_revision_counts'),
            topology_revision_counts=profile.get('topology_revision_counts'),
            batch_sizes=profile['batch_sizes'], harness_sha256=profile['harness_sha256'])
    result = dict(diagnostics_only=profiles)
    for phase in ('screen','matrix','variance'):
        path = args.root/phase/'summary.json'
        result[phase] = load(path) if path.exists() else 'NOT RUN'
    matrix = result['matrix']
    if isinstance(matrix,list):
        active = [row for row in matrix if row['name'].startswith(('normal-','busy'))]
        broad = active+[row for row in matrix if row['name'].startswith('small')]
        result['throughput'] = {}
        for key,rows in [('primary',active),('including_small',broad)]:
            sums = {field:[sum(row[field][i] for row in rows) for i in (0,1)] for field in ('consumed','qualified')}
            result['throughput'][key] = dict(totals=sums, twice=all(value[1]>=2*value[0] for value in sums.values()),
                every_supported_improves=all(row[field][1]>row[field][0] for row in rows for field in ('consumed','qualified')))
        result['screen_passes'] = sum(row['status']=='PASS' for row in matrix)
        result['gate_d'] = 'PASS' if result['screen_passes']==13 and all(row['twice'] and row['every_supported_improves'] for row in result['throughput'].values()) else 'BLOCKER'
        checks = []
        for path in sorted((args.root/'matrix').glob('*.jsonl')):
            row = json.loads(path.read_text().splitlines()[-1])
            counts = row['history']['counts']
            negative = row['mode'] in ('off','incomplete') or row['durable_configs']>256
            checks.append(dict(name=path.stem,
                persisted_decisions=counts.get('written_decisions',0),
                native_persistence=counts.get('written_decisions',0)==(0 if row['mode']=='off' else row['original_intents']),
                provenance=counts.get('valid_provenance',0)==counts.get('written_decisions',0),
                negative_control=(not negative or (not counts.get('v2_receipts',0) and not row['dataset']['eligible'])),
                unsupported=(row['durable_configs']<=256 or set(row['prepare_error_reasons'])=={'agent bound'})))
        result['native_checks'] = checks
        if not all(row['native_persistence'] and row['provenance'] and row['negative_control'] and row['unsupported'] for row in checks): result['gate_d']='BLOCKER'
    args.output.write_text(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
