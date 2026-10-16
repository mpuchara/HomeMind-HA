"""Exact screening parity with the 0.14.208 statistical reference."""
from collections import defaultdict
import copy
import math
import random
import unittest
from unittest.mock import patch

import support
import context_tournament_policy_candidate as screening


def reference_corr(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if screening._finite(x) is not None and screening._finite(y) is not None]
    if len(pairs) < 3:
        return 0.0
    ax, ay = [x for x, _ in pairs], [y for _, y in pairs]
    mx, my = sum(ax) / len(ax), sum(ay) / len(ay)
    vx = sum((x - mx) ** 2 for x in ax)
    vy = sum((y - my) ** 2 for y in ay)
    if vx <= 1e-12 or vy <= 1e-12:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    return max(-1.0, min(1.0, cov / math.sqrt(vx * vy)))


def reference_score(samples, min_samples=8):
    rows = [dict(x) for x in (samples or []) if isinstance(x, dict)]
    if len(rows) < int(min_samples):
        return dict(score=0.0, best_group=None, samples=len(rows), groups={}, status='needs_more_data')
    labels = [row.get('label') for row in rows]
    groups = defaultdict(list)
    for row in rows:
        for key in ('value', 'quality', 'lag_1', 'lag_3', 'lag_10', 'trend', 'time_since_edge'):
            groups[key].append(row.get(key))
        for key, value in sorted((row.get('interactions') or {}).items()):
            groups[f'interaction:{key}'].append(value)
    scores = {}
    for key, values in groups.items():
        paired = [(x, y) for x, y in zip(values, labels)
                  if screening._finite(x) is not None and screening._finite(y) is not None]
        if len(paired) >= int(min_samples):
            scores[key] = abs(reference_corr([x for x, _ in paired], [y for _, y in paired])) * min(
                1.0, len(paired) / max(1.0, float(min_samples * 2)))
    if not scores:
        return dict(score=0.0, best_group=None, samples=len(rows), groups={}, status='neutral')
    best_group = max(scores, key=lambda key: (scores[key], key))
    best = max(0.0, min(1.0, float(scores[best_group])))
    return dict(score=best, best_group=best_group, samples=len(rows),
                groups={k: round(float(v), 6) for k, v in sorted(scores.items())},
                status='screened' if best > .05 else 'neutral')


def sample_rows(count=96):
    return [dict(episode=str(i), ts=float(i), label=float(i % 2), value=i / 7.,
                 quality=.98, lag_1=i / 8., lag_3=i / 9., lag_10=i / 10.,
                 trend=i / 17., time_since_edge=.95,
                 interactions={f'primary_{j}': i / (13. + j) for j in range(3)})
            for i in range(count)]


class ScreeningPerformanceTests(unittest.TestCase):
    def test_full_retained_window_is_bit_exact_and_input_is_owned(self):
        rows = sample_rows()
        before = copy.deepcopy(rows)
        expected = reference_score(rows)
        actual = screening.semantic_predictive_score(rows)
        self.assertEqual(actual, expected)
        actual['groups'].clear()
        self.assertEqual(screening.semantic_predictive_score(rows), expected)
        self.assertEqual(rows, before)

    def test_randomized_sparse_malformed_and_nonfinite_values_match_reference(self):
        rng = random.Random(210)
        choices = [None, '', 'bad', '0.4', float('nan'), float('inf'), -float('inf'), True, False]
        for trial in range(150):
            rows = sample_rows(rng.randrange(1, 110))
            for row in rows:
                for key in ('label', 'value', 'quality', 'lag_1', 'trend'):
                    row[key] = rng.choice(choices) if rng.random() < .35 else rng.uniform(-4, 4)
                row['interactions'] = {k: v for k, v in row['interactions'].items() if rng.random() < .7}
            rows.extend([None, 3, []])
            minimum = rng.choice([1, 3, 8, 12, 96])
            with self.subTest(trial=trial):
                self.assertEqual(screening.semantic_predictive_score(rows, minimum), reference_score(rows, minimum))

    def test_short_neutral_constant_and_perfect_correlation(self):
        for count in (0, 2, 7, 8, 16, 96):
            for label in (0., 1.):
                rows = [dict(label=label, value=1.) for _ in range(count)]
                self.assertEqual(screening.semantic_predictive_score(rows), reference_score(rows))
        for sign in (-1, 1):
            rows = [dict(label=float(i), value=sign * float(i)) for i in range(16)]
            self.assertEqual(screening.semantic_predictive_score(rows)['score'], 1.)

    def test_tie_breaking_and_sparse_interaction_alignment_are_unchanged(self):
        rows = [dict(label=float(i % 2), value=float(i % 2),
                     interactions={'z': float(i % 2), 'a': float(i % 2)} if i % 3 else {'a': 0.})
                for i in range(96)]
        self.assertEqual(screening.semantic_predictive_score(rows), reference_score(rows))
        for row in rows:
            row['interactions'] = dict(reversed(list(row['interactions'].items())))
        self.assertEqual(screening.semantic_predictive_score(rows), reference_score(rows))

    def test_direct_correlation_redundancy_and_nonfinite_pairs_are_exact(self):
        xs = [None, 0., '1', 'bad', float('nan'), 2., 3., float('inf'), 4.]
        ys = [1., 3., '2', 4., 3., 1., 0., 5., -1.]
        self.assertEqual(screening._corr(xs, ys), reference_corr(xs, ys))
        rows = sample_rows()
        other = [row | {'value': -row['value']} for row in rows]
        self.assertEqual(screening.redundancy_score(rows, other), {'score': 1., 'samples': 96})

    def test_label_always_refreshes_screening_and_canonical_payload(self):
        row = dict(history=[[1., 1.]], samples=sample_rows(), screening={})
        payload = screening._pool_json_payload(row, samples_changed=True)
        row['samples'] = row['samples'][1:] + [dict(label=0., value=19., interactions={'new': 1.})]
        with patch.object(screening, 'semantic_predictive_score', wraps=screening.semantic_predictive_score) as compute:
            updated = screening._pool_json_payload(row, payload, samples_changed=True)
        compute.assert_called_once()
        self.assertNotEqual(payload[1], updated[1])
        self.assertEqual(row['screening'], reference_score(row['samples']))

    def test_label_is_validated_once_per_sample_and_no_second_corr_filter(self):
        rows = sample_rows()
        original = screening._finite
        with patch.object(screening, '_finite', wraps=original) as finite, patch.object(
                screening, '_corr', side_effect=AssertionError('already validated pairs')):
            self.assertEqual(screening.semantic_predictive_score(rows), reference_score(rows))
            # Reference evaluation also uses _finite, so measure a separate call.
            finite.reset_mock()
            screening.semantic_predictive_score(rows)
        self.assertEqual(finite.call_count, 96 * 11)  # label + ten feature groups


if __name__ == '__main__':
    unittest.main()
