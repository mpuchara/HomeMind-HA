"""Aggregate immutable U05b.4c.1e run summaries by independent run pair."""
import argparse
from collections import defaultdict
import json
from math import comb
from pathlib import Path

BASELINE = 'b3a67c0a2d264697ac69f5ee16bdd673b843f6d3'
AUDIT = '613e2e2638df6ac58b873bbcc2a93d78fd63a8c2'
CURRENT = '7cf56533187a7db43a0d5d384b8e82444c0108d1'
MARGINS = {'p95_ms': 1.10, 'p99_ms': 1.15}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def paired_test(ratios, margin):
    n = len(ratios)
    within = sum(r < margin for r in ratios)
    above = sum(r > margin for r in ratios)

    def tail(k):
        return sum(comb(n, i) for i in range(k, n+1)) / 2**n

    return dict(independent_pairs=n, margin_ratio=margin, within=within, above=above,
                noninferiority_p=tail(within), regression_p=tail(above))


def summarize(root):
    series = {}
    comparisons = defaultdict(dict)
    for label in ('primary', 'extension', 'diagnostic'):
        path = root / label
        if not (path / 'plan.json').exists():
            continue
        plan = read(path / 'plan.json')
        results = []
        for case in plan['cases']:
            name = case['name']
            rows = sorted(path.glob(name + '*.summary.json'))
            if not rows:
                continue
            result = read(rows[-1])
            results.append(result)
            if label != 'diagnostic':
                pair_name, variant = name.rsplit('-', 1)
                comparisons[pair_name][variant] = result
        series[label] = dict(plan_size=len(plan['cases']), completed_attempts=len(results),
                             cases=results)
    pairs = []
    for name, runs in comparisons.items():
        if not all(runs.get(k, {}).get('status') == 'completed' for k in ('baseline', 'current')):
            continue
        base, current = runs['baseline'], runs['current']
        ratios = {metric: current['event_to_intent'][metric] / base['event_to_intent'][metric]
                  for metric in (*MARGINS, 'p50_ms', 'max_ms')}
        parity = (base['input_sha256'] == current['input_sha256'] and
                  base['decisions_sha256'] == current['decisions_sha256'] and
                  base['ha_calls'] == current['ha_calls'] == 0 and
                  base['background_work']['correct_writes'] == current['background_work']['correct_writes'])
        pairs.append(dict(name=name, ratios=ratios, observed_parity=parity))
    matched = [p for p in pairs if p['name'].startswith('matched-1000-200-')]
    natural = [p for p in pairs if p['name'].startswith('natural-1000-200-')]
    test = {m: paired_test([p['ratios'][m] for p in matched], limit)
            for m, limit in MARGINS.items()}
    status = 'INCONCLUSIVE'
    if len(matched) >= 4 and all(p['observed_parity'] for p in matched + natural):
        if any(v['regression_p'] < .05 for v in test.values()):
            status = 'REGRESSION'
        elif len(matched) == 6 and len(natural) == 2 and all(v['noninferiority_p'] < .05 for v in test.values()) and all(
                p['ratios'][m] <= limit for p in natural for m, limit in MARGINS.items()):
            status = 'NO_REGRESSION'
    return dict(stage='U05b.4c.1e', baseline_sha=BASELINE, audit_sha=AUDIT, current_sha=CURRENT,
                production_change=False, series=series, run_pairs=pairs,
                gate_d=status, matched_large_tests=test,
                qualified_evidence_under_load='NOT TESTED', hardware='NOT TESTED',
                prior_recovery='UNIVERSAL_U05B4C1_RECOVERY_SUMMARY.json',
                limitations=['Physical collector rows and V2/V3 qualification require a separate experiment.',
                             'Natural-mode training counts and actual CPU overlap are measured outcomes.',
                             'Decision hash covers observed outcomes and intent status in the fixture.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='Root containing primary, extension and diagnostic folders')
    args = parser.parse_args()
    print(json.dumps(summarize(args.root), indent=2))


if __name__ == '__main__':
    main()
