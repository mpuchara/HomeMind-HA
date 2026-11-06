"""Keep writer component diagnostics separate from registered acceptance."""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    p.add_argument('output', type=Path)
    args = p.parse_args()
    profiles = {}
    for path in sorted((args.root/'writer-profiles').glob('*.profile.json')):
        name = path.name.removesuffix('.profile.json')
        probe = load(path)
        case = json.loads(path.with_name(name+'.jsonl').read_text().splitlines()[-1])
        rows = probe['rows']
        # New diagnostics preserve setup/load/finalization separately. Retain
        # the historical aggregate report without discarding those raw rows.
        if any(key.startswith(('setup/', 'load/', 'finalization/')) for key in rows):
            merged = {}
            for key, values in rows.items():
                key = key.split('/', 1)[1]
                total = merged.setdefault(key, [0, 0, 0])
                for index, value in enumerate(values): total[index] += value
            rows = merged
            sql = {}
            for key, value in probe['sql_trace_events'].items():
                key = key.rsplit('/', 1)[-1]
                sql[key] = sql.get(key, 0) + value
            probe['sql_trace_events'] = sql
        writer = rows['universal_history_collector.HistoricalTrainingCollector._write']
        cpu = writer[1]/1e6
        transactions = probe['sql_trace_events'].get('BEGIN', 0)
        upserts = sum(case['collector'].get(k, 0) for k in ('native_event_upserts', 'native_seed_upserts'))
        qualified = case['history']['counts'].get('v2_receipts', 0)
        profiles[name] = dict(diagnostic_only=True, writer_cpu_ms=cpu, writer_attempts=writer[0],
            writer_committed_batches=case['collector']['batches'],
            traced_begin_events=transactions, native_upserts=upserts,
            persisted_qualified_receipts=qualified, eligible_dataset_samples=case['dataset']['eligible'],
            cpu_ms_per_writer_attempt=cpu/writer[0] if writer[0] else None,
            cpu_ms_per_traced_begin=cpu/transactions if transactions else None,
            normalization_note='Whole writer CPU includes setup/finalization and retries; BEGIN trace events are not a success counter. Native phase entry/exit costs remain separately available in raw rows.',
            cpu_ms_per_native_upsert=cpu/upserts if upserts else None,
            cpu_ms_per_qualified_receipt=cpu/qualified if qualified else None,
            batch_sizes=probe['batch_sizes'], sql_trace_events=probe['sql_trace_events'],
            protection_states=probe['protection_states'],
            exclusive_top=sorted(rows.items(), key=lambda row:row[1][2], reverse=True)[:25],
            original_intents=case['original_intents'], input_fingerprint=case['input_fingerprint'],
            collector_errors=case['collector']['errors'], collector_dropped=case['collector']['dropped'])
    result = dict(profiles=profiles)
    for phase in ('matrix', 'variance', 'preflight-ablation'):
        path = args.root/phase/'summary.json'
        result[phase] = load(path) if path.exists() else 'NOT COMPLETE'
    pairs = result['matrix']
    if isinstance(pairs, list):
        active = [r for r in pairs if r['name'].startswith(('normal-', 'busy'))]
        broad = active+[r for r in pairs if r['name'].startswith('small')]
        def throughput(rows):
            totals = {field:[sum(row[field][i] for row in rows) for i in (0,1)]
                      for field in ('consumed', 'qualified')}
            return dict(totals=totals, at_least_2x=all(v[1]>=2*v[0] for v in totals.values()),
                        every_supported_case_improves=all(r[field][1]>r[field][0]
                            for r in rows for field in ('consumed', 'qualified')))
        result['throughput'] = dict(primary=throughput(active), broader=throughput(broad))
        result['registered_screen_passes'] = sum(r['status']=='PASS' for r in pairs)
        result['registered_screen_total'] = len(pairs)
        result['gate_d'] = ('PASS' if all(r['status']=='PASS' for r in pairs)
            and all(v['at_least_2x'] and v['every_supported_case_improves']
                    for v in result['throughput'].values()) else 'BLOCKER')
        validations = []
        for pair in pairs:
            for arm in ('base', 'source'):
                path = args.root/'matrix'/(pair['name']+'-'+arm+'.jsonl')
                row = json.loads(path.read_text().splitlines()[-1])
                if 'error' in row:
                    validations.append(dict(name=path.stem, valid=False, error=row['error']))
                    continue
                counts = row['history']['counts']
                negative = row['mode'] in ('off', 'incomplete') or row['durable_configs']>256
                valid = (counts.get('written_decisions', 0)==(0 if row['mode']=='off' else row['original_intents'])
                         and (not negative or (not counts.get('v2_receipts', 0) and not row['dataset']['eligible']))
                         and (row['durable_configs']<=256 or set(row['prepare_error_reasons'])=={'agent bound'}))
                validations.append(dict(name=path.stem, valid=valid, negative=negative))
        result['native_validation'] = validations
        if not all(r['valid'] for r in validations): result['gate_d'] = 'BLOCKER'
    args.output.write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
