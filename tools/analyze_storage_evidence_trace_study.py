"""Summarize the diagnostic boundaries without assigning unsupported causes."""
import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def median_ms(values):
    return statistics.median(values)/1e6 if values else None


def summarize(trace_path):
    trace = json.loads(trace_path.read_text())
    run_path = trace_path.with_name(trace_path.name.replace('.trace.json', '.jsonl'))
    run = json.loads(run_path.read_text().splitlines()[-1])
    groups = defaultdict(list)
    for event in trace['events']:
        groups[event['kind']].append(event)
    prepared = defaultdict(list)
    for event in groups['prepare']:
        if event['published']:
            prepared[event['agent']].append(event)
    misses = [row for row in groups['begin'] if row['outcome'] == 'not_prepared_at_lookup']
    later_started = [any(turn['started_ns'] > row['ns'] for turn in prepared[row['agent']])
                     for row in misses]
    queue_ms = []
    first_request = {}
    for event in sorted(groups['request'] + groups['prepare'], key=lambda row: (
            row['started_ns'] if row['kind']=='prepare' else row['ns'])):
        agent = event['agent']
        if event['kind']=='request':
            first_request.setdefault(agent, event['ns'])
        elif agent in first_request:
            queue_ms.append(event['started_ns']-first_request.pop(agent))
    writer = groups['write_completed']
    results = dict(
        overflow=trace['overflow'], fingerprint=run['input_fingerprint'], signature=run['decision_signature'],
        original_decisions=run['original_intents'],
        begins=Counter(row['outcome'] for row in groups['begin']),
        binds=Counter(row['outcome'] for row in groups['bind']),
        observed_match_rejections=Counter(
            '+'.join(key for key, value in row.items()
                     if value is True and (key.endswith('_mismatch') or key.startswith('observed_')))
            or 'unattributed' for row in groups['match_rejected']),
        requests=len(groups['request']), preparation_attempts=len(groups['prepare']),
        preparation_published=sum(map(len, prepared.values())),
        preparation_errors=[row['error'] for row in groups['prepare'] if row['error']],
        peak_pending=max((row['pending'] for row in groups['prepare']), default=0),
        misses_with_later_preparation_started=sum(later_started),
        median_observed_first_request_to_attempt_ms=median_ms(queue_ms),
        median_preparation_wall_ms=median_ms([row['ns']-row['started_ns'] for row in groups['prepare']]),
        preparation_cpu_ms=sum(row['cpu_ns'] for row in groups['prepare'])/1e6,
        preparation_native_cursor_calls=sum(row['native_cursor_calls'] for row in groups['prepare']),
        preparation_native_cursor_wall_ms=sum(row['native_cursor_ns'] for row in groups['prepare'])/1e6,
        verify_rejected=Counter(row['rejection'] for row in groups['verify'] if row['rejection']),
        verify_wall_ms=sum(row['elapsed_ns'] for row in groups['verify'])/1e6,
        accepted_submissions=sum(row['accepted'] for row in groups['submit']),
        write_completed=len(writer), receipts=sum(row['receipt'] for row in writer),
        median_queued_to_write_completed_ms=median_ms([row['ns']-round(row['queued_perf_s']*1e9)
                                                      for row in writer]),
        persisted_missing_proof=run['history']['missing_proof'],
        qualified=run['dataset']['eligible'], qualification_rejections=run['dataset'].get('rejected', {}),
        collector_drops=run['collector']['dropped'], collector_errors=run['collector']['errors'],
        event_drops=run['event']['dropped'], ha_calls=run['ha_service_calls'],
        clean_sql_calls_whole_process=trace['clean_sql_calls'],
        clean_sql_timed_cpu_ms_whole_process=trace['clean_sql_cpu_ns']/1e6)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('traces', type=Path)
    args = parser.parse_args()
    print(json.dumps({path.name:summarize(path) for path in sorted(args.traces.glob('*.trace.json'))},
                     indent=2))


if __name__ == '__main__':
    main()
