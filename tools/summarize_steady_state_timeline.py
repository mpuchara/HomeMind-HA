"""Summarize non-overlapping dispatch phases and overlapping timeline exposures."""
import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path

from benchmark_runtime_recovery import quantiles


def intersection_ms(intervals, start, end):
    """Union duration clipped to one target interval."""
    left = right = start
    duration = 0
    for begin, finish in sorted(intervals):
        if finish <= start or begin >= end:
            continue
        begin, finish = max(start, begin), min(end, finish)
        if begin > right:
            duration += right - left
            left = begin
        right = max(right, finish)
    return (duration + right - left) / 1e6


def summarize(trace):
    threads = trace['threads']
    lower = trace.get('steady_start_ns') or trace['start_ns']
    work = defaultdict(list)
    for thread in threads:
        for kind, start, end, _fields in thread['rows']:
            work[kind].append((start, end))
    rows = []
    for thread in threads:
        by_entry = {}
        agents = []
        intents = []
        own = defaultdict(list)
        for kind, start, end, fields in thread['rows']:
            own[kind].append((start, end))
            if kind == 'executor_queue':
                by_entry[(fields['target'], end)] = (start, fields['event_ns'])
            elif kind == 'agent':
                agents.append((start, end, fields['agent']))
            elif kind == 'intent_submit':
                intents.append(start)
        for kind, entry, exit_ns, fields in thread['rows']:
            if kind != 'worker_active':
                continue
            key = fields['target'], entry
            if key not in by_entry:
                continue
            queued, received = by_entry[key]
            if received is None or not lower <= received <= queued <= entry:
                continue
            agent = next((a for a in agents if entry <= a[0] <= a[1] <= exit_ns), None)
            if agent is None:
                continue
            entered, completed, aid = agent
            intent = next((ts for ts in intents if entered <= ts <= completed), None)
            if intent is None:
                continue
            # Partition only event -> first submit for the first agent on the target.
            phases = {
                'event_to_submission': (queued-received)/1e6,
                'executor_queue': (entry-queued)/1e6,
                'worker_entry_to_agent': (entered-entry)/1e6,
                'agent_to_intent_submit': (intent-entered)/1e6,
            }
            own_exposure = {k: intersection_ms(own[k], entered, intent) for k in
                            ('store_lock_wait', 'sqlite_execute', 'context_features', 'prediction')}
            rows.append(dict(target=key[0], agent=aid, received_ns=received,
                total_ms=(intent-received)/1e6, phases_ms=phases, own_exposure_ms=own_exposure))
    rows.sort(key=lambda row: row['total_ms'], reverse=True)
    slowest = rows[:min(20, len(rows))]
    for row in slowest:
        received = row['received_ns']
        submitted = received + round(row['phases_ms']['event_to_submission']*1e6)
        row['background_pre_admission_exposure_ms'] = {
            kind: intersection_ms(work[kind], received, submitted) for kind in (
                'training_update', 'model_write', 'historical_collector_write', 'tournament_observation',
                'tiny_shadow_schema', 'sqlite_execute')}
        row['worker_active_pre_admission_exposure_ms'] = intersection_ms(
            work['worker_active'], received, submitted)
    phase_names = ('event_to_submission', 'executor_queue', 'worker_entry_to_agent', 'agent_to_intent_submit')
    return dict(joined_targets=len(rows), dropped_by_thread={t['thread']: t['dropped'] for t in threads},
                total=quantiles([row['total_ms'] for row in rows]),
                phase_quantiles={p: quantiles([row['phases_ms'][p] for row in rows]) for p in phase_names},
                slowest=slowest,
                scope='Complete retained joins only. Exposures overlap and omit evicted spans. '
                      'Phase quantiles describe marginal distributions and cannot be added.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw', type=Path)
    args = parser.parse_args()
    with gzip.open(args.raw) as stream:
        raw = json.load(stream)
    print(json.dumps(summarize(raw['probe']), indent=2))


if __name__ == '__main__':
    main()
