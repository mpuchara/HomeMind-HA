"""U05b.2 offline evidence gate. Passing is NOT publication or promotion.

Uses U05a identity/split and the existing exact paired sign-test implementation.
Repeated points cannot inflate independent support: episodes and overlapping
time buckets are merged. Independence beyond these blocks is a caller evidence
assumption, not a fact inferred from different episode ids.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import math

import numpy as np

from policy_tiny_mlp_training import exact_paired_mlp_win_p_value
from universal_training_data import chronological_split, _digest
from universal_scorer_training import (dataset_identity, load_core_checkpoint, checkpoint_bytes,
                                      sample_features, logits, MAX_FEATURE_BYTES, _yield, _Cancelled)

VALIDATION_VERSION = 1


@dataclass(frozen=True)
class ValidationPolicy:
    family_alpha: float = .05
    accuracy_half_width: float = .25
    independence_window_seconds: float = 60.

    def validate(self):
        for name, low, high in (('family_alpha', .000001, .1),
                                ('accuracy_half_width', .01, .5),
                                ('independence_window_seconds', 1., 86400.)):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('invalid validation policy '+name)

    def required_units(self, family_size=1):
        # Two-sided Hoeffding bound for bounded independent unit accuracies,
        # with Bonferroni allocation for multiple reported device slices.
        return math.ceil(math.log(2*max(1, family_size)/self.family_alpha)
                         / (2*self.accuracy_half_width**2))


def _group(sample):
    action = sample.available_actions.candidates[0]
    return (action.entity_id.partition('.')[0], action.property_name, action.kind)


def _slice(sample):
    action = sample.available_actions.candidates[0]
    return (action.logical_device_id, action.property_name)


def _units(samples, seconds):
    """Merge whole episode intervals when their fixed time buckets overlap.

    A long episode spanning several windows cannot be split into independent
    votes; overlapping episodes are conservatively merged too. No per-second
    loop or allocation proportional to the wall-clock span is performed.
    """
    intervals = {}
    for sample in samples:
        if not isinstance(sample.episode_id, str) or not sample.episode_id:
            raise ValueError('missing explicit episode identity')
        bucket = math.floor(sample.observation_timestamp/seconds)
        old = intervals.get(sample.episode_id, (bucket, bucket))
        intervals[sample.episode_id] = (min(old[0], bucket), max(old[1], bucket))
    episodes = {}
    end, members = None, []
    def flush():
        if members:
            identity = min(members)
            for episode in members:
                episodes[episode] = identity
    for episode, (lo, hi) in sorted(intervals.items(), key=lambda item: (*item[1], item[0])):
        if end is not None and lo > end:
            flush();members=[];end=None
        members.append(episode)
        end = hi if end is None else max(end, hi)
    flush()
    return {sample.sample_id: episodes[sample.episode_id] for sample in samples}


def _target_template(sample):
    action = next(a for a in sample.available_actions.candidates if a.value == sample.desired_action)
    if action.kind == 'categorical':
        return ('categorical', action.option_label)
    span = action.upper-action.lower
    return ('relative_position', (action.value-action.lower)/span if span else .5)


def _keep_current(sample):
    matches = [a.value for a in sample.available_actions.candidates if a.value == a.current_value]
    return matches[0] if len(matches) == 1 else None


def _modal_value(sample, template):
    actions = sample.available_actions.candidates
    if template[0] == 'categorical':
        matches = [a.value for a in actions if a.option_label == template[1]]
        return matches[0] if len(matches) == 1 else None
    # Baseline-only projection of a training relative-position mode. This never
    # rounds a Correct label or alters the desired action used by training.
    def order(action):
        span = action.upper-action.lower
        position = (action.value-action.lower)/span if span else .5
        return (abs(position-template[1]), action.value)
    return min(actions, key=order).value


def _unit_mean(pairs):
    grouped = defaultdict(list)
    for unit, value in pairs:
        grouped[unit].append(value)
    return math.fsum(math.fsum(values)/len(values) for values in grouped.values())/len(grouped)


def _fit_baseline(training, units):
    grouped = defaultdict(list)
    for sample in training:
        grouped[_group(sample)].append(sample)
    baseline = {}
    exported = []
    for group, samples in sorted(grouped.items()):
        by_unit = defaultdict(list)
        for sample in samples:
            by_unit[units[sample.sample_id]].append(sample)
        counts = defaultdict(float)
        for points in by_unit.values():
            mass = math.fsum(point.sample_weight for point in points)
            for point in points:
                counts[_target_template(point)] += point.sample_weight/mass
        mode = min(counts, key=lambda key: (-counts[key], repr(key)))
        modal_score = _unit_mean((units[s.sample_id], float(_modal_value(s, mode) == s.desired_action)) for s in samples)
        current_score = _unit_mean((units[s.sample_id], float(_keep_current(s) == s.desired_action)) for s in samples)
        selected = 'modal' if modal_score > current_score else 'keep_current'
        baseline[group] = (selected, mode)
        exported.append({'group': list(group), 'selected': selected, 'modal_template': list(mode),
                         'modal_training_accuracy': modal_score, 'current_training_accuracy': current_score,
                         'training_units': len(by_unit)})
    return baseline, exported


def _summarize(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['unit']].append(row)
    model, baseline, current, differences = [], [], [], []
    wins = losses = 0
    for points in grouped.values():
        m = sum(p['model_correct'] for p in points)
        b = sum(p['baseline_correct'] for p in points)
        c = sum(p['current_correct'] for p in points)
        model.append(m/len(points));baseline.append(b/len(points));current.append(c/len(points))
        differences.append((m-b)/len(points))
        wins += int(m > b);losses += int(m < b)
    n = len(grouped)
    p = exact_paired_mlp_win_p_value(wins, losses)
    return {'points': len(rows), 'independent_units': n,
            'accuracy': math.fsum(model)/n, 'baseline_accuracy': math.fsum(baseline)/n,
            'keep_current_accuracy': math.fsum(current)/n, 'mean_paired_gain': math.fsum(differences)/n,
            'winning_units': wins, 'losing_units': losses, 'tied_units': n-wins-losses,
            'improvement_p_value_upper_bound': max(p, math.ulp(0.)),
            'abstentions': sum(r['predicted'] is None for r in rows),
            'false_on': sum(r['kind'] == 'binary' and r['desired'] == 0 and r['predicted'] == 1 for r in rows),
            'false_off': sum(r['kind'] == 'binary' and r['desired'] == 1 and r['predicted'] == 0 for r in rows)}


def validate_core_artifact(checkpoint, dataset, *, policy=ValidationPolicy(), cancel=None):
    """Recompute offline quality from pinned weights and a current U05a manifest.

    No checkpoint flag is changed. Caller must prepare current evidence; atomic
    freshness/publication and repeated-holdout-use tracking remain U05b.3 work.
    A caller must declare policy before examining holdout results.
    """
    policy.validate()
    report = {'validation_version': VALIDATION_VERSION, 'status': 'insufficient_evidence',
              'policy': asdict(policy), 'policy_id': _digest(asdict(policy)),
              'trained': False, 'control_eligible': False, 'promotion_eligible': False,
              'reasons': [], 'scope': 'offline manifest only; no publication or physical acceptance'}
    def finish(status, reason=None):
        report['status'] = status
        if reason is not None:
            report['reasons'].append(reason)
        report['report_checksum'] = _digest(report)
        return report
    try:
        _yield(cancel)
        dataset_identity(dataset, cancel=cancel)
        try:
            model = load_core_checkpoint(checkpoint_bytes(checkpoint))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            return finish('invalid_artifact', str(exc))
        report.update(artifact_id=checkpoint['checksum'], dataset_id=dataset.dataset_id,
                      supervision_revision=dataset.supervision_revision, encoder_id=checkpoint['encoder_id'])
        if (checkpoint['dataset_id'] != dataset.dataset_id
                or checkpoint['supervision_revision'] != dataset.supervision_revision):
            return finish('dataset_invalidated', 'dataset_or_supervision_revision_changed')
        ids = [s.supervision_event_id for s in dataset.samples]
        if len(ids) != len(set(ids)) or len({s.sample_id for s in dataset.samples}) != len(ids):
            return finish('insufficient_evidence', 'duplicate_supervision_or_sample')
        split = checkpoint['split']
        training, holdout = chronological_split(dataset, **split)
        if not training or not holdout:
            return finish('insufficient_evidence', 'empty_train_or_future_holdout')
        if split['holdout_from']-split['train_until'] < policy.independence_window_seconds:
            return finish('insufficient_evidence', 'split_embargo_below_independence_window')
        manifest = [{key: getattr(s, key) for key in ('sample_id','supervision_event_id',
                     'history_source_revision','generation_id','episode_id')} for s in (*training,*holdout)]
        if checkpoint['samples'] != manifest:
            return finish('invalid_artifact', 'training_manifest_mismatch')
        train_units = _units(training, policy.independence_window_seconds)
        test_units = _units(holdout, policy.independence_window_seconds)
        # Audit labels, but never use holdout labels to fit/select the baseline.
        prepared = {}
        feature_bytes = 0
        for sample in (*training, *holdout):
            _yield(cancel)
            example = sample_features(sample, model.encoder, model.scorer, cancel=cancel)
            feature_bytes += example.matrix.nbytes
            if feature_bytes > MAX_FEATURE_BYTES:
                return finish('insufficient_evidence', 'validation_feature_byte_cap_exceeded')
            prepared[sample.sample_id] = example
        train_features = [prepared[s.sample_id].matrix for s in training]
        mean = np.mean([x.mean(axis=0, dtype=np.float64) for x in train_features], axis=0)
        variance = np.mean([np.mean((x-mean)**2, axis=0) for x in train_features], axis=0)
        scale = np.maximum(np.sqrt(variance), 1e-3)
        if not np.array_equal(model.mean, mean) or not np.array_equal(model.scale, scale):
            return finish('invalid_artifact', 'normalization_not_derived_from_training_partition')
        for phase, points in (('train',training),('holdout',holdout)):
            if any(checkpoint[when][phase]['samples'] != len(points) for when in ('before','after')):
                return finish('invalid_artifact', 'training_partition_receipt_mismatch')
        baseline, baseline_receipt = _fit_baseline(training, train_units)
        report['baseline'] = baseline_receipt
        report['baseline_id'] = _digest(baseline_receipt)
        required = policy.required_units()
        report['required_global_units'] = required
        report['training_units'] = len(set(train_units.values()))
        report['holdout_units'] = len(set(test_units.values()))
        if report['training_units'] < required or report['holdout_units'] < required:
            return finish('insufficient_evidence', 'too_few_independent_global_units')
        if any(item['training_units'] < required for item in baseline_receipt):
            return finish('insufficient_evidence', 'too_few_training_units_for_baseline_group')
        if any(_group(s) not in baseline for s in holdout):
            return finish('insufficient_evidence', 'unseen_holdout_semantic_group')
        required_slices = {_slice(s) for s in (*training, *holdout)}
        test_slices = defaultdict(set)
        for sample in holdout:
            test_slices[_slice(sample)].add(test_units[sample.sample_id])
        slice_minimum = policy.required_units(len(required_slices))
        report['required_units_per_slice'] = slice_minimum
        uncovered = [{'slice': list(key), 'units': len(test_slices[key])} for key in sorted(required_slices)
                     if len(test_slices[key]) < slice_minimum]
        if uncovered:
            report['uncovered_slices'] = uncovered
            return finish('insufficient_evidence', 'missing_independent_device_property_coverage')
        rows, train_rows = [], []
        for samples, units, destination in ((training, train_units, train_rows),(holdout, test_units, rows)):
            for sample in samples:
                _yield(cancel)
                example = prepared[sample.sample_id]
                scores = logits(model.scorer, model.normalize(example).matrix)
                maxima = np.flatnonzero(scores == np.max(scores))
                predicted = sample.available_actions.candidates[int(maxima[0])].value if len(maxima) == 1 else None
                choice, template = baseline[_group(sample)]
                base_value = _modal_value(sample, template) if choice == 'modal' else _keep_current(sample)
                destination.append({'unit': units[sample.sample_id], 'slice': _slice(sample),
                    'action_slice': (*_slice(sample), _target_template(sample)),
                    'kind': sample.available_actions.candidates[0].kind, 'desired': sample.desired_action,
                    'predicted': predicted, 'model_correct': predicted == sample.desired_action,
                    'baseline_correct': base_value == sample.desired_action,
                    'current_correct': _keep_current(sample) == sample.desired_action})
        report['train'] = _summarize(train_rows)
        report['holdout'] = _summarize(rows)
        report['slices'] = [{'slice': list(key), **_summarize([r for r in rows if r['slice'] == key])}
                            for key in sorted(required_slices)]
        action_keys = {r['action_slice'] for r in rows}
        report['action_slices'] = [
            {'slice': [key[0], key[1], list(key[2])],
             **_summarize([r for r in rows if r['action_slice'] == key])}
            for key in sorted(action_keys)]
        # Zero observed device regression is deliberately conservative. It is
        # not a statistical proof that the true regression probability is zero.
        regressions = [s['slice'] for s in report['slices'] if s['mean_paired_gain'] < 0]
        if regressions:
            report['regressing_slices'] = regressions
            return finish('validation_failed', 'observed_device_property_regression')
        action_regressions = [s['slice'] for s in report['action_slices'] if s['mean_paired_gain'] < 0]
        if action_regressions:
            report['regressing_action_slices'] = action_regressions
            return finish('validation_failed', 'observed_action_class_regression')
        if report['holdout']['mean_paired_gain'] <= 0:
            return finish('validation_failed', 'no_positive_gain_over_train_only_baseline')
        if report['holdout']['improvement_p_value_upper_bound'] > policy.family_alpha:
            return finish('validation_failed', 'paired_unit_improvement_not_significant')
        return finish('passed')
    except _Cancelled:
        return finish('cancelled', 'cancelled')
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return finish('insufficient_evidence', str(exc))
