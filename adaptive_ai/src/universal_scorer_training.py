"""U05b.1 offline neural ranking core, frozen U03, no runtime installation.

Optimization is real; statistical acceptance/publication is NOT implemented in
this step. Core checkpoints deliberately remain trained=False, unvalidated and
non-promotable. They cannot be loaded by the shipped U04 observer as verified
models. No HA, Executor, ActionIntent, Candidate Manager or SQLite dependency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import threading
import time

import numpy as np

from training_budget import TRAINING_BUDGET
from universal_action_scorer import UniversalActionScorer, SCORER_CONTRACT_VERSION
from universal_actions import ACTION_CONTRACT_VERSION, ActionSet
from universal_neural_encoder import SharedSetEncoder, IncrementalNeuralHome
from universal_observation import CONTRACT_VERSION as OBSERVATION_VERSION
from universal_training_data import (TrainingDataset, CONTRACT_VERSION as DATA_VERSION,
                                     TRUSTED_SOURCES, EXCLUDED_KINDS, chronological_split,
                                     _digest)

CORE_VERSION = 1
MAX_EXAMPLES = 2048
MAX_ACTIONS = 128
MAX_FEATURE_BYTES = 32 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
_ADMISSION = threading.Lock()
_THREAD = 'adaptive-ai-index-universal-scorer'


@dataclass(frozen=True)
class TrainingConfig:
    epochs: int = 80
    batch_size: int = 8
    learning_rate: float = 0.01
    l2: float = 0.0001
    gradient_clip: float = 5.0
    patience: int = 8
    min_delta: float = 0.000001
    seed: int = 43

    def validate(self):
        for name, lo, hi in (('epochs', 1, 256), ('batch_size', 1, 32),
                             ('patience', 1, 64), ('seed', 0, 2**32-1)):
            value = getattr(self, name)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError('invalid training '+name)
        for name, lo, hi in (('learning_rate', 1e-6, .1), ('l2', 0., .1),
                             ('gradient_clip', 1e-6, 100.), ('min_delta', 0., 1.)):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError('invalid training '+name)


@dataclass(frozen=True)
class RankingExample:
    matrix: np.ndarray
    target: int
    weight: float
    sample_id: str


class _Cancelled(Exception):
    pass


def _yield(cancel=None):
    if cancel is not None and cancel.is_set():
        raise _Cancelled()
    TRAINING_BUDGET.checkpoint('universal_scorer_core', thread_name=_THREAD)
    if cancel is not None and cancel.is_set():
        raise _Cancelled()


def dataset_identity(dataset, *, cancel=None):
    """Verify the existing U05a content identity; no new provenance ledger."""
    if not isinstance(dataset, TrainingDataset) or dataset.version != DATA_VERSION:
        raise ValueError('incompatible TrainingDataset')
    if not dataset.supervision_revision or not 0 <= len(dataset.samples) <= MAX_EXAMPLES:
        raise ValueError('invalid dataset metadata or size')
    if sum(len(s.home_observation) for s in dataset.samples) > 250000:
        raise ValueError('dataset token reference cap exceeded')
    digests = []
    for sample in dataset.samples:
        _yield(cancel)
        digests.append(_digest(asdict(sample)))
    identity = _digest(dict(version=DATA_VERSION, supervision=dataset.supervision_revision, samples=digests))
    if identity != dataset.dataset_id:
        raise ValueError('dataset content identity mismatch')
    return identity


def sample_features(sample, encoder, scorer, *, cancel=None):
    """Use U03/U04 features without reimplementing replay or consulting live HA."""
    if not isinstance(sample.available_actions, ActionSet):
        raise ValueError('missing historical action mask')
    if (sample.version != DATA_VERSION or sample.observation_contract_version != OBSERVATION_VERSION
            or sample.available_actions.version != ACTION_CONTRACT_VERSION
            or sample.supervision_source not in TRUSTED_SOURCES
            or not sample.supervision_event_id or not sample.history_source_revision
            or not sample.registry_revision or not sample.sample_id
            or sample.available_actions.agent_id != sample.agent_id):
        raise ValueError('ineligible sample contract/provenance')
    actions = sample.available_actions.candidates
    if not 2 <= len(actions) <= MAX_ACTIONS:
        raise ValueError('action cardinality outside core training bound')
    if not math.isfinite(sample.sample_weight) or not 0 < sample.sample_weight <= 100:
        raise ValueError('invalid sample weight')
    if not math.isfinite(sample.observation_timestamp) or not math.isfinite(sample.label_available_ts):
        raise ValueError('invalid sample clocks')
    if not math.isfinite(sample.available_actions.mask_timestamp) or sample.available_actions.mask_timestamp > sample.observation_timestamp:
        raise ValueError('future historical action mask')
    matches = [i for i, action in enumerate(actions) if action.value == sample.desired_action]
    if len(matches) != 1:
        raise ValueError('desired action not uniquely present in historical legal mask')
    if len({a.value for a in actions}) != len(actions):
        raise ValueError('duplicate legal actions')
    if any(a.entity_id != sample.target_device or a.property_name != sample.target_property
           or a.mask_timestamp != sample.available_actions.mask_timestamp for a in actions):
        raise ValueError('action target or clock mismatch')
    if encoder.room_width != scorer.room_width:
        raise ValueError('encoder/scorer width mismatch')
    if not 1 <= len(sample.home_observation) <= 10000:
        raise ValueError('missing or oversized historical context')
    mirror = IncrementalNeuralHome(encoder)
    ids = set()
    for index, token in enumerate(sample.home_observation):
        if (token.entity_id in ids or token.event_ts > sample.observation_timestamp
                or token.received_ts > sample.observation_timestamp or token.kind in EXCLUDED_KINDS):
            raise ValueError('ineligible causal observation')
        ids.add(token.entity_id)
        mirror.upsert(token, now=sample.observation_timestamp)
        if index % 16 == 0:
            _yield(cancel)
    if not mirror.diagnostics()['available'] or not any(t.available and t.quality > 0 for t in sample.home_observation):
        raise ValueError('no usable historical observations')
    home = mirror.home()
    matrix = np.stack([np.concatenate((home, mirror.room(a.area_id), scorer.action_features(a)))
                       for a in actions]).astype(np.float32)
    if not np.isfinite(matrix).all():
        raise ValueError('nonfinite encoded features')
    matrix.setflags(write=False)
    return RankingExample(matrix, matches[0], float(sample.sample_weight), sample.sample_id)


def logits(scorer, matrix):
    x = np.asarray(matrix, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != scorer.input_width or not np.isfinite(x).all():
        raise ValueError('invalid ranking feature matrix')
    w1, b1 = scorer.layers['dense1']
    w2, b2 = scorer.layers['dense2']
    hidden = np.maximum(x @ w1.T + b1, 0.)
    output = (hidden @ w2.T + b2).reshape(-1)
    if not np.isfinite(output).all():
        raise ValueError('nonfinite scorer output')
    return output


def loss_gradients(scorer, examples, *, l2=0.):
    """Weighted listwise CE, one softmax per device decision, exact derivatives.

    Action count does not multiply a decision's supervision weight. Continuous
    targets must be exact legal proposals, never silently rounded to a neighbor.
    Softmax is an internal loss calculation, not calibrated control confidence.
    """
    if not examples or not math.isfinite(l2) or l2 < 0:
        raise ValueError('invalid ranking batch/regularization')
    if any(e.matrix.ndim != 2 or not 2 <= len(e.matrix) <= MAX_ACTIONS
           or type(e.target) is not int or not 0 <= e.target < len(e.matrix)
           or not math.isfinite(e.weight) or e.weight <= 0 for e in examples):
        raise ValueError('invalid ranking example')
    x = np.concatenate([e.matrix for e in examples]).astype(np.float64)
    scores = logits(scorer, x)
    w1, b1 = scorer.layers['dense1']
    w2, b2 = scorer.layers['dense2']
    hidden = np.maximum(x @ w1.T + b1, 0.)
    delta = np.zeros((len(x), 1), dtype=np.float64)
    mass = math.fsum(e.weight for e in examples)
    offset = 0
    loss = 0.
    for example in examples:
        end = offset + len(example.matrix)
        group = scores[offset:end]
        shifted = group - np.max(group)
        exponential = np.exp(shifted)
        total = np.sum(exponential)
        loss += example.weight * (math.log(total) - float(shifted[example.target])) / mass
        derivative = exponential / total
        derivative[example.target] -= 1.
        delta[offset:end, 0] = derivative * (example.weight / mass)
        offset = end
    back = (delta @ w2) * (hidden > 0.)
    gradients = {'dense1': (back.T @ x + l2*w1, np.sum(back, axis=0)),
                 'dense2': (delta.T @ hidden + l2*w2, np.sum(delta, axis=0))}
    loss += .5*l2*(float(np.sum(w1.astype(np.float64)**2)) + float(np.sum(w2.astype(np.float64)**2)))
    if not math.isfinite(loss) or any(not np.isfinite(a).all() for pair in gradients.values() for a in pair):
        raise ValueError('nonfinite loss/gradient')
    return loss, gradients


class CoreScorer:
    """Isolated offline ranking model with persisted train-only normalization."""
    def __init__(self, scorer, encoder, mean, scale):
        self.scorer, self.encoder = scorer, encoder
        self.mean = np.asarray(mean, dtype=np.float64).copy()
        self.scale = np.asarray(scale, dtype=np.float64).copy()
        if (self.mean.shape != (scorer.input_width,) or self.scale.shape != self.mean.shape
                or not np.isfinite(self.mean).all() or not np.isfinite(self.scale).all()
                or np.any(self.scale < 1e-3) or encoder.room_width != scorer.room_width):
            raise ValueError('invalid core normalization/embedding width')

    def normalize(self, example):
        matrix = np.clip((example.matrix-self.mean)/self.scale, -6., 6.)
        return RankingExample(matrix, example.target, example.weight, example.sample_id)

    def scores(self, sample):
        row = self.normalize(sample_features(sample, self.encoder, self.scorer))
        return logits(self.scorer, row.matrix)


def _metrics(model, rows, *, cancel=None):
    loss = mass = correct = 0.
    for row in rows:
        _yield(cancel)
        score = logits(model.scorer, row.matrix)
        shifted = score-np.max(score)
        loss += row.weight*(math.log(float(np.exp(shifted).sum()))-float(shifted[row.target]))
        correct += row.weight*float(int(np.argmax(score)) == row.target)
        mass += row.weight
    return {'loss': loss/mass, 'accuracy': correct/mass, 'samples': len(rows)}


def train_scorer(parent, encoder, dataset, *, train_until, holdout_from, holdout_until,
                 config=TrainingConfig(), cancel=None):
    """U05b.1: bounded Adam on an isolated child. Always non-promotable.

    Early stopping monitors training loss only. Future holdout is measured before
    and after optimization and never drives epoch/normalization selection. Formal
    acceptance versus baseline and publication-time undo gates belong to U05b.2/3.
    Cancellation discards the child; a restart deterministically rebuilds it.
    """
    config.validate()
    if not _ADMISSION.acquire(blocking=False):
        return {'status': 'busy', 'checkpoint': None}
    started = time.perf_counter()
    updates = 0
    try:
        _yield(cancel)
        dataset_identity(dataset, cancel=cancel)
        ids = [s.supervision_event_id for s in dataset.samples]
        sample_ids = [s.sample_id for s in dataset.samples]
        if len(set(ids)) != len(ids) or len(set(sample_ids)) != len(sample_ids):
            raise ValueError('duplicate supervision/sample identity')
        train, holdout = chronological_split(dataset, train_until=train_until,
                                             holdout_from=holdout_from, holdout_until=holdout_until)
        if len(train) < 2 or len(holdout) < 2:
            return {'status': 'insufficient_evidence', 'reason': 'need_train_and_future_holdout', 'checkpoint': None}
        # Detach both networks; caller's active weights and encoder cannot change.
        parent_checkpoint = parent.checkpoint()
        frozen = SharedSetEncoder(token_width=encoder.token_width, room_width=encoder.room_width,
                                  hidden_width=encoder.hidden_width, checkpoint=encoder.checkpoint())
        child = UniversalActionScorer(room_width=parent.room_width, hidden_width=parent.hidden_width,
                                      checkpoint=parent_checkpoint)
        prepared, used_bytes = [], 0
        for sample in (*train, *holdout):
            row = sample_features(sample, frozen, child, cancel=cancel)
            used_bytes += row.matrix.nbytes
            if used_bytes > MAX_FEATURE_BYTES:
                raise ValueError('prepared feature byte cap exceeded')
            prepared.append(row)
        # Train-only moments; every decision contributes equal row mass regardless
        # of how many legal actions it exposes. Sample weights act in the loss.
        raw_train = prepared[:len(train)]
        mean = np.mean([r.matrix.mean(axis=0, dtype=np.float64) for r in raw_train], axis=0)
        variance = np.mean([np.mean((r.matrix-mean)**2, axis=0) for r in raw_train], axis=0)
        model = CoreScorer(child, frozen, mean, np.maximum(np.sqrt(variance), 1e-3))
        rows = [model.normalize(row) for row in prepared]
        training, testing = rows[:len(train)], rows[len(train):]
        before = {'train': _metrics(model, training, cancel=cancel), 'holdout': _metrics(model, testing, cancel=cancel)}
        moments = {k: [np.zeros_like(a, dtype=np.float64) for a in pair] for k, pair in child.layers.items()}
        variances = {k: [a.copy() for a in pair] for k, pair in moments.items()}
        rng = np.random.default_rng(config.seed)
        best_loss, best_layers, stale, best_epoch = math.inf, None, 0, 0
        losses, epoch_seconds = [], []
        preparation_seconds = time.perf_counter()-started
        for epoch in range(config.epochs):
            epoch_started = time.perf_counter()
            order = rng.permutation(len(training))
            for start in range(0, len(order), config.batch_size):
                _yield(cancel)
                batch = [training[int(i)] for i in order[start:start+config.batch_size]]
                _, gradients = loss_gradients(child, batch, l2=config.l2)
                norm = math.sqrt(sum(float(np.sum(a*a)) for pair in gradients.values() for a in pair))
                clip = min(1., config.gradient_clip/max(norm, 1e-12))
                updates += 1
                for key, pair in child.layers.items():
                    for j, parameter in enumerate(pair):
                        gradient = gradients[key][j]*clip
                        moments[key][j] = .9*moments[key][j]+.1*gradient
                        variances[key][j] = .999*variances[key][j]+.001*gradient**2
                        direction = (moments[key][j]/(1.-.9**updates))/(np.sqrt(variances[key][j]/(1.-.999**updates))+1e-8)
                        parameter[:] = (parameter.astype(np.float64)-config.learning_rate*direction).astype(np.float32)
                        if not np.isfinite(parameter).all():
                            raise ValueError('nonfinite optimizer weights')
            current = _metrics(model, training, cancel=cancel)['loss']
            losses.append(current)
            if best_loss-current > config.min_delta:
                best_loss, stale, best_epoch = current, 0, epoch+1
                best_layers = {k: tuple(a.copy() for a in pair) for k, pair in child.layers.items()}
            else:
                stale += 1
            epoch_seconds.append(time.perf_counter()-epoch_started)
            if stale >= config.patience:
                break
        child.layers = best_layers
        _yield(cancel)
        validation_started = time.perf_counter()
        after = {'train': _metrics(model, training, cancel=cancel), 'holdout': _metrics(model, testing, cancel=cancel)}
        checkpoint = {
            'core_version': CORE_VERSION, 'stage': 'U05b.1', 'trained': False,
            'optimization_complete': True, 'validation_status': 'not_validated',
            'control_eligible': False, 'promotion_eligible': False,
            'data_contract_version': DATA_VERSION, 'observation_contract_version': OBSERVATION_VERSION,
            'action_contract_version': ACTION_CONTRACT_VERSION, 'scorer_contract_version': SCORER_CONTRACT_VERSION,
            'dataset_id': dataset.dataset_id, 'supervision_revision': dataset.supervision_revision,
            'encoder_id': _digest(frozen.checkpoint()), 'encoder': frozen.checkpoint(),
            'scorer': child.checkpoint(), 'parent_id': _digest(parent_checkpoint),
            'normalization': {'mean': model.mean.tolist(), 'scale': model.scale.tolist()},
            'training_config': asdict(config), 'before': before, 'after': after,
            'epochs': len(losses), 'best_epoch': best_epoch, 'updates': updates, 'epoch_losses': losses,
            'split': {'train_until': train_until, 'holdout_from': holdout_from, 'holdout_until': holdout_until},
            'samples': [{key: getattr(s, key) for key in (
                'sample_id', 'supervision_event_id', 'history_source_revision', 'generation_id', 'episode_id')}
                for s in (*train, *holdout)],
        }
        checkpoint['checksum'] = _digest(checkpoint)
        return {'status': 'optimization_complete', 'checkpoint': checkpoint, 'model': model,
                'timing': {'preparation_seconds': preparation_seconds, 'epoch_seconds': epoch_seconds,
                           'validation_seconds': time.perf_counter()-validation_started,
                           'total_seconds': time.perf_counter()-started},
                'feature_bytes': used_bytes, 'updates': updates}
    except _Cancelled:
        return {'status': 'cancelled', 'checkpoint': None, 'updates_discarded': updates}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return {'status': 'insufficient_evidence', 'reason': str(exc), 'checkpoint': None}
    finally:
        _ADMISSION.release()


def checkpoint_bytes(checkpoint):
    raw = json.dumps(checkpoint, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise ValueError('core checkpoint byte cap exceeded')
    return raw


def _decode_core_checkpoint(raw):
    """Checksum/shape/contract checked experimental artifact, never pickle."""
    if not isinstance(raw, bytes) or len(raw) > MAX_CHECKPOINT_BYTES:
        raise ValueError('invalid checkpoint bytes')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate checkpoint key')
            result[key] = value
        return result
    data = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(data, dict):
        raise ValueError('checkpoint must be a JSON object')
    checksum = data.pop('checksum', None)
    if _digest(data) != checksum:
        raise ValueError('core checkpoint checksum mismatch')
    if (data.get('core_version') != CORE_VERSION or data.get('stage') != 'U05b.1'
            or data.get('trained') is not False or data.get('optimization_complete') is not True
            or data.get('validation_status') != 'not_validated'
            or data.get('control_eligible') is not False or data.get('promotion_eligible') is not False
            or data.get('data_contract_version') != DATA_VERSION
            or data.get('observation_contract_version') != OBSERVATION_VERSION
            or data.get('action_contract_version') != ACTION_CONTRACT_VERSION
            or data.get('scorer_contract_version') != SCORER_CONTRACT_VERSION
            or data.get('encoder_id') != _digest(data['encoder'])):
        raise ValueError('incompatible or falsely verified core checkpoint')
    TrainingConfig(**data['training_config']).validate()
    for key in ('dataset_id', 'supervision_revision', 'encoder_id', 'parent_id'):
        value = data.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('invalid checkpoint identity')
    if (type(data.get('epochs')) is not int or not 1 <= data['epochs'] <= 256
            or type(data.get('best_epoch')) is not int or not 1 <= data['best_epoch'] <= data['epochs']
            or type(data.get('updates')) is not int or not 1 <= data['updates'] <= 524288
            or len(data['epoch_losses']) != data['epochs']
            or any(not math.isfinite(v) or v < 0 for v in data['epoch_losses'])):
        raise ValueError('invalid optimization receipt')
    if not isinstance(data['samples'], list) or not 4 <= len(data['samples']) <= MAX_EXAMPLES:
        raise ValueError('invalid sample manifest')
    for key in ('sample_id', 'supervision_event_id'):
        values = [s[key] for s in data['samples']]
        if any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
            raise ValueError('invalid or duplicated manifest identity')
    e, s = data['encoder'], data['scorer']
    encoder = SharedSetEncoder(token_width=e['token_width'], room_width=e['room_width'],
                               hidden_width=e['hidden_width'], checkpoint=e)
    scorer = UniversalActionScorer(room_width=s['room_width'], hidden_width=s['hidden_width'], checkpoint=s)
    return CoreScorer(scorer, encoder, **data['normalization'])


def load_core_checkpoint(raw):
    try:
        return _decode_core_checkpoint(raw)
    except (TypeError, KeyError, OverflowError, AttributeError) as exc:
        raise ValueError('malformed core checkpoint') from exc


def core_artifact_status(checkpoint, current_dataset):
    """Undo/data changes require a fresh child rebuild, never subtract weights.

    This is a diagnostic invalidation helper, not a promotion/publication gate.
    U05b.3 still needs a transactional fresh-evidence provider at publication.
    """
    load_core_checkpoint(checkpoint_bytes(checkpoint))
    dataset_identity(current_dataset)
    changed = (current_dataset.dataset_id != checkpoint['dataset_id']
               or current_dataset.supervision_revision != checkpoint['supervision_revision'])
    return {'status': 'requires_rebuild' if changed else 'unvalidated_core',
            'promotion_eligible': False, 'control_eligible': False}
