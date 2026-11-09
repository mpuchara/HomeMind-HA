"""Join bounded scheduler traces to native receipts and synthetic decisions."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


def analyze(path):
    raw = json.loads(path.read_text())
    report = json.loads(path.with_name(path.name.replace('.trace.json', '.jsonl')).read_text().splitlines()[-1])
    events = sorted(raw['events'], key=lambda row: row['ns'])
    actors = defaultdict(lambda: defaultdict(list))
    pending = {}
    positions = {}
    writes = {}
    outcomes = {}
    qualified = set()
    for event in events:
        kind = event['kind']
        agent = event.get('agent')
        if agent:
            actors[agent][kind].append(event)
        if kind == 'enqueue':
            pending[agent] = None
        elif kind == 'selected':
            pending.pop(agent, None)
        elif kind == 'lookup' and event['pending']:
            positions[event['ns']] = list(pending).index(agent)+1 if agent in pending else None
        elif kind == 'write_completed':
            writes[event['decision']] = event
        elif kind == 'verify':
            outcomes[event['decision']] = event['rejection'] or 'accepted'
        elif kind == 'qualified':
            qualified.add((agent, event['decision_ts']))
    first = next(row['ns'] for row in events if row['kind'] == 'lookup')
    missing = []
    for agent, stages in actors.items():
        if len(stages['lookup']) != len(stages['bind']):
            raise ValueError(f'lookup/bind count differs for {agent}')
        for lookup, bind in zip(stages['lookup'], stages['bind']):
            decision = bind['decision']
            native = writes.get(decision)
            if (agent, bind['decision_ts']) in qualified:
                continue
            at = lookup['ns']
            published = [row['ns'] for row in stages['published']]
            selected = [row['ns'] for row in stages['selected']]
            reason = 'pending_at_lookup' if lookup['pending'] else 'absent_at_lookup'
            if lookup['prepared']:
                reason = 'prepared_at_lookup'
            elif selected and selected[0] < at and not any(ns < at for ns in published):
                reason = 'selected_without_publication_at_lookup'
            missing.append(dict(
                agent=agent, target=lookup.get('target'), decision=decision,
                decision_ts=bind['decision_ts'], lookup_ns=at, reason=reason,
                prepared=lookup['prepared'], pending=lookup['pending'], queue_position=positions.get(at),
                requests_before_lookup=[row['ns'] for row in stages['enqueue'] if row['ns'] <= at],
                request_history=stages['enqueue'], selection_ns=selected, publication_ns=published,
                publication_observed_before_lookup=any(ns < at for ns in published),
                match_rejections=[row for row in stages['match_rejected'] if at <= row['ns'] <= bind['ns']],
                preparation_attempts=stages['prepare'], bind=bind['outcome'],
                writer_verification=outcomes.get(decision),
                writer_completed_ns=native['ns'] if native else None,
                missing_proof=native['missing_proof'] if native else None,
                qualified=False, avoidable_delay='UNKNOWN'))
    lookups = sum(len(stages['lookup']) for stages in actors.values())
    if raw['overflow'] or lookups-len(missing) != report['dataset']['eligible']:
        raise ValueError(f'incomplete evidence trace for {path.name}')
    preparations = [row for row in events if row['kind']=='prepare']
    selections = [row for row in events if row['kind']=='selected']
    return dict(
        decisions=report['original_intents'], fingerprint=report['input_fingerprint'],
        signature=report['decision_signature'], qualified=report['dataset']['eligible'],
        consumed=report['collector']['predecision']['consumed'], native=report['history'],
        collector_drops=report['collector']['dropped'], event_drops=report['event']['dropped'],
        ha_calls=report['ha_service_calls'], collector_errors=report['collector']['errors'],
        sqlite_deferrals=report['collector']['deferred_for_sqlite'],
        cpu_s=report['process_cpu_seconds'], latency=report['event_to_intent'],
        rss_mb=report['rss_after_mb'], writer_batches=report['collector']['batches'],
        events=Counter(row['kind'] for row in events),
        missing_reasons=Counter(row['reason'] for row in missing),
        preparation_cpu_ms=sum(row['cpu_ns'] for row in preparations)/1e6,
        preparation_wall_median_ms=median((row['ns']-row['started_ns'])/1e6 for row in preparations),
        preparation_sql_wall_ms=sum(row['native_cursor_ns'] for row in preparations)/1e6,
        selected_ms=[round((row['ns']-first)/1e6,3) for row in selections],
        missing=missing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('traces', type=Path)
    args = parser.parse_args()
    print(json.dumps({path.name:analyze(path) for path in sorted(args.traces.glob('*.trace.json'))}))


if __name__ == '__main__':
    main()
