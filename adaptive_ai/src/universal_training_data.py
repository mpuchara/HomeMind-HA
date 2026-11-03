"""U05a bounded offline data foundation. No model mutation or physical authority.

Caller-owned SQLite snapshots and historical decision receipts are mandatory.
Never reconstruct missing authority/topology from the current Engine or HA.
Rebuild datasets from current feedback state after undo; this is not an online
incremental label-consumption ledger. U05b must validate the dataset revision
again at publication and rebuild an isolated child when supervision is revoked.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math

from manual_feedback_contract import ACTIVE_STATUSES
from training_budget import TRAINING_BUDGET
from universal_actions import ActionSet, from_device_agent_mask
from universal_ha_adapter import CausalObservationReplay, journal_observation, read_feature_journal
from universal_observation import CONTRACT_VERSION as OBSERVATION_VERSION, SensorToken

CONTRACT_VERSION = 1
MAX_SAMPLES = 2048
MAX_TOKEN_REFERENCES = 250000
MAX_ACTION_REFERENCES = 65536
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
BUDGET_THREAD = 'adaptive-ai-index-universal-data'
TRUSTED_SOURCES = frozenset((
    'correct', 'teach_rl', 'manual', 'teaching', 'teach_desired',
    'ui_manual_correction', 'ui_keep_current', 'ui_teach_desired',
    'physical_manual_change',
))
# Until causal photometry is adapted, do not train need from emitted light/power.
EXCLUDED_KINDS = frozenset(('illuminance', 'power'))


def _number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a timestamp/action')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite training metadata')
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _bounded(rows, cap):
    if not 1 <= int(cap) <= MAX_SAMPLES:
        raise ValueError('invalid sample cap')
    result = []
    for row in rows:
        if len(result) >= int(cap):
            raise ValueError('sample cap exceeded; no silent truncation')
        result.append(row)
    return result


@dataclass(frozen=True)
class Supervision:
    supervision_event_id: str
    feedback_id: str
    agent_id: str
    generation_id: str | None
    episode_id: str | None
    decision_ts: float
    available_ts: float
    desired_action: float
    source: str
    active: bool
    sample_weight: float = 1.0


@dataclass(frozen=True)
class ReplayCoverage:
    """Collector's persisted complete-history watermark, not inferred from rows.

    The offline caller must supply this from collection/retention evidence for
    its exact read snapshot. Seeing one sensor seed does not prove that no
    intervening events were dropped. A gap or missing watermark is ineligible.
    """
    source_revision: str
    start_ts: float
    complete_through_ts: float
    entity_ids: frozenset[str]
    complete: bool
    version: int = CONTRACT_VERSION
    seed_receipts: tuple[tuple[str, float, float], ...] = ()
    collector_retention_epoch: int | None = None

    def __post_init__(self):
        object.__setattr__(self, 'seed_receipts', tuple(tuple(r) for r in self.seed_receipts))


@dataclass(frozen=True)
class HistoricalDecision:
    """Versioned historical receipt, recorded before the requested action.

    Payload is immutable JSON containing agent, descriptor, legal_mask, state,
    required_sensor_ids and affected_sensor_ids. Both sensor lists are required;
    the caller must capture the historical dependency/exclusion policy, not
    obtain it from today's registry. A receipt is evidence, never dispatch approval.
    """
    decision_ts: float
    event_ts: float
    received_ts: float
    registry_information_ts: float
    registry_revision: str
    payload_json: str
    version: int = CONTRACT_VERSION

    @classmethod
    def capture(cls, *, decision_ts, event_ts, received_ts,
                registry_information_ts, registry_revision, agent, descriptor,
                legal_mask, state, required_sensor_ids, affected_sensor_ids,
                predecision_evidence=None,affected_contract_version=None,
                affected_sensor_evidence=None,affected_sensor_status=None):
        payload = dict(agent=agent, descriptor=descriptor, legal_mask=legal_mask,
                       state=state, required_sensor_ids=sorted(set(required_sensor_ids)),
                       affected_sensor_ids=(None if affected_sensor_ids is None and affected_contract_version is not None
                                            else sorted(set(affected_sensor_ids))))
        if predecision_evidence is not None:
            payload['predecision_evidence']=predecision_evidence
        if affected_contract_version is not None:
            payload.update(affected_contract_version=affected_contract_version,
                affected_sensor_evidence=affected_sensor_evidence,affected_sensor_status=affected_sensor_status)
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > 262144:
            raise ValueError('historical decision receipt too large')
        return cls(*map(_number, (decision_ts, event_ts, received_ts,
                                  registry_information_ts)), str(registry_revision), encoded,
                   version=2 if affected_contract_version is not None else CONTRACT_VERSION)


@dataclass(frozen=True)
class UniversalTrainingSample:
    version: int
    observation_contract_version: int
    observation_timestamp: float
    registry_revision: str
    home_observation: tuple[SensorToken, ...]
    target_device: str
    target_property: str
    available_actions: ActionSet
    desired_action: float
    supervision_source: str
    supervision_event_id: str
    episode_id: str | None
    agent_id: str
    generation_id: str | None
    label_available_ts: float
    sample_weight: float
    sample_id: str
    history_source_revision: str


@dataclass(frozen=True)
class TrainingDataset:
    version: int
    samples: tuple[UniversalTrainingSample, ...]
    rejected: tuple[tuple[str, str], ...]
    supervision_revision: str
    dataset_id: str


def read_supervision(conn, agent_ids, *, as_of_ts, max_rows=MAX_SAMPLES):
    """Read explicit journal facts, including tombstones, in one bounded query.

    Existing teach_rl_label links carry the generation-independent Correct id.
    A journaled manual action without a Teach-RL link uses its durable feedback
    id. Old unlinked labels and historical agent predictions are not trusted.
    Caller owns a consistent read transaction; no migrations or writes occur.
    """
    ids = sorted(set(map(str, agent_ids)))
    if not ids:
        return ()
    if len(ids) > 256 or not 1 <= max_rows <= MAX_SAMPLES:
        raise ValueError('supervision query bounds exceeded')
    marks = ','.join('?' for _ in ids)
    cursor = conn.execute(f'''
        WITH roots AS (
            SELECT DISTINCT COALESCE(root_agent_id,agent_id) AS root
            FROM manual_feedback_journal WHERE agent_id IN ({marks})
        )
        SELECT j.*, l.supervision_event_id AS linked_event_id, e.ref_id AS effect_ref,
               l.undone_ts AS label_undone_ts, e.status AS effect_status,
               l.sample_ts AS linked_ts, l.desired AS linked_desired
        FROM manual_feedback_journal j
        LEFT JOIN manual_feedback_effects e ON e.feedback_id=j.feedback_id
             AND e.ref_type='teach_rl_label' AND e.effect_kind='learning'
        LEFT JOIN teaching_rl_labels l ON CAST(l.id AS TEXT)=e.ref_id
        WHERE COALESCE(j.root_agent_id,j.agent_id) IN (SELECT root FROM roots)
              AND j.created_ts<=?
        ORDER BY j.selected_ts,j.feedback_id LIMIT ?
    ''', (*ids, _number(as_of_ts), max_rows + 1))
    columns = [entry[0] for entry in cursor.description]
    rows = _bounded((dict(zip(columns, row)) for row in cursor), max_rows)
    result = []
    for row in rows:
        TRAINING_BUDGET.checkpoint('universal_supervision_read', thread_name=BUDGET_THREAD)
        source = str(row['source'])
        if source not in TRUSTED_SOURCES or row['correct_action'] is None:
            continue
        if row['contract_version'] != 1:
            continue
        if row['effect_ref'] is not None and row['linked_ts'] is None:
            raise ValueError('dangling supervision link; provenance audit required')
        active = (row['undone_ts'] is None and row['label_undone_ts'] is None
                  and row['application_status'] in ACTIVE_STATUSES
                  and row['effect_status'] != 'undone'
                  and (row['effect_ref'] is None or row['linked_ts'] is not None)
                  and _number(row['selected_ts']) <= _number(as_of_ts))
        if row['linked_ts'] is not None:
            active = active and (_number(row['linked_ts']) == _number(row['selected_ts'])
                                 and _number(row['linked_desired']) == _number(row['correct_action']))
        result.append(Supervision(
            str(row['linked_event_id'] or 'feedback-v1:' + row['feedback_id']),
            str(row['feedback_id']), str(row['agent_id']), row['generation_id'],
            row['episode_id'], _number(row['selected_ts']), _number(row['created_ts']),
            _number(row['correct_action']), source, bool(active),
        ))
    return tuple(result)


def _eligible_sample(label, receipt, replay, coverage):
    if receipt.version not in (CONTRACT_VERSION,2):
        raise ValueError('unsupported_decision_contract')
    at = _number(label.decision_ts)
    if _number(receipt.decision_ts) != at:
        raise ValueError('decision_time_mismatch')
    if any(_number(value) > at for value in (
            receipt.event_ts, receipt.received_ts, receipt.registry_information_ts)):
        raise ValueError('future_decision_information')
    if len(receipt.payload_json.encode()) > 262144:
        raise ValueError('decision_receipt_too_large')
    data = json.loads(receipt.payload_json)
    evidence=data.get('predecision_evidence')
    if receipt.version==CONTRACT_VERSION and ('affected_contract_version' in data
            or (evidence is not None and 'affected_contract_version' in evidence)):
        raise ValueError('affected_decision_contract_downgrade')
    if receipt.version==2:
        from universal_affected_sensors import VERSION as AFFECTED_VERSION, validate
        if type(data.get('affected_contract_version')) is not int or data['affected_contract_version']!=AFFECTED_VERSION or evidence is None:
            raise ValueError('affected_missing_or_unsupported_contract')
        if data.get('affected_sensor_status')!='complete':
            raise ValueError(data.get('affected_sensor_status') or 'affected_declaration_missing')
        if evidence.get('affected_contract_version')!=AFFECTED_VERSION or data.get('affected_sensor_evidence')!=evidence.get('affected_sensor_evidence'):
            raise ValueError('affected_predecision_binding_mismatch')
        ids=validate(data.get('affected_sensor_evidence'),evidence,
                     sensor_universe=None if coverage is None else coverage.entity_ids,
                     topology_information_ts=receipt.registry_information_ts)
        if data.get('affected_sensor_ids') is None or list(ids)!=data['affected_sensor_ids']:
            raise ValueError('affected_sensor_set_mismatch')
    if evidence is not None:
        from universal_predecision import validate_binding
        intent=dict(intent_id=evidence['decision_id'],created_at=at,agent_id=label.agent_id,
                    target_entity=data['agent']['target_entity'],target_property=data['agent']['target_property'],
                    policy_version=evidence['policy_version'],model_revision=evidence['model_revision'])
        validate_binding(evidence,intent,data['agent'],data['state'])
        if (evidence.get('verified_revision')!=evidence['arbiter_revision']
                or evidence['created_ts']!=receipt.event_ts or evidence['available_ts']!=receipt.received_ts
                or evidence['topology']!=receipt.registry_revision
                or evidence['descriptor']!=data['descriptor'] or evidence['legal_mask']!=data['legal_mask']
                or (label.generation_id is not None and label.generation_id!=evidence['generation_id'])):
            raise ValueError('invalid_predecision_training_evidence')
    required = set(data['required_sensor_ids'])
    affected = set(data['affected_sensor_ids'])
    if not required or len(required) > 10000:
        raise ValueError('missing_or_oversized_historical_dependency_set')
    if (coverage is None or coverage.version not in (CONTRACT_VERSION, 2)
            or coverage.complete is not True or not coverage.source_revision
            or not isinstance(coverage.entity_ids, frozenset)
            or not required <= coverage.entity_ids
            or not _number(coverage.start_ts) <= at <= _number(coverage.complete_through_ts)):
        raise ValueError('missing_or_incomplete_history_coverage')
    snapshot = replay.snapshot(at)
    if snapshot['registry_revision'] == 'unregistered' or receipt.registry_revision != snapshot['registry_revision']:
        raise ValueError('missing_or_mismatched_historical_registry')
    tokens = {token.entity_id: token for token in snapshot['tokens']}
    if not required <= tokens.keys():
        raise ValueError('incomplete_history')
    selected = tuple(tokens[eid] for eid in sorted(required - affected)
                     if tokens[eid].kind not in EXCLUDED_KINDS)
    if not selected or not any(t.available and t.quality > 0 for t in selected):
        raise ValueError('no_usable_causal_context')
    if any(t.event_ts > at or t.received_ts > at for t in selected):
        raise ValueError('future_observation')
    seeds = set(coverage.seed_receipts) if coverage.version == 2 else set()
    if any(t.event_ts < coverage.start_ts and (t.entity_id,t.event_ts,t.received_ts) not in seeds for t in selected):
        raise ValueError('seed_outside_verified_history_coverage')
    agent = data['agent']
    if str(agent['id']) != label.agent_id:
        raise ValueError('agent_identity_mismatch')
    actions = from_device_agent_mask(agent, data['descriptor'], data['legal_mask'],
                                     data['state'], mask_timestamp=receipt.event_ts)
    desired = _number(label.desired_action)
    if not actions.candidates or not any(c.value == desired for c in actions.candidates):
        raise ValueError('desired_action_not_in_historical_legal_set')
    weight = _number(label.sample_weight)
    if not 0 < weight <= 100:
        raise ValueError('invalid_sample_weight')
    identity = _digest(dict(event=label.supervision_event_id, time=at,
                            target=agent['target_entity'], property=agent['target_property']))
    return UniversalTrainingSample(
        CONTRACT_VERSION, OBSERVATION_VERSION, at, receipt.registry_revision,
        selected, str(agent['target_entity']), str(agent['target_property']), actions,
        desired, label.source, label.supervision_event_id, label.episode_id,
        label.agent_id, label.generation_id, _number(label.available_ts), weight, identity,
        coverage.source_revision,
    )


def build_dataset(supervision, decisions, replay, *, coverage=None, max_samples=MAX_SAMPLES):
    """Deterministic rebuild: deduplicate before sampling, revoke across copies.

    `decisions` is keyed by (agent_id, decision_ts). No persistent consumed-id
    set is needed: each new isolated model trains once per logical event from
    a fresh manifest. Do not incrementally replay this manifest into an already
    trained model without the U05b checkpoint/dataset revision protocol.
    """
    labels = _bounded(supervision, max_samples)
    revision = _digest(sorted((asdict(row) for row in labels),
                             key=lambda row: json.dumps(row, sort_keys=True)))
    groups = {}
    for label in labels:
        groups.setdefault(label.supervision_event_id, []).append(label)
    samples, rejected = [], []
    token_references = action_references = 0
    receipt_bytes = 0
    for event_id, copies in sorted(groups.items()):
        TRAINING_BUDGET.checkpoint('universal_dataset_build', thread_name=BUDGET_THREAD)
        try:
            if not event_id or any(not row.active for row in copies):
                raise ValueError('revoked_or_conflicted_supervision')
            if any(row.source not in TRUSTED_SOURCES for row in copies):
                raise ValueError('untrusted_supervision_source')
            facts = {(_number(r.decision_ts), _number(r.desired_action)) for r in copies}
            if len(facts) != 1:
                raise ValueError('conflicting_supervision_copies')
            targets = set()
            for row in copies:
                copy_receipt = decisions.get((row.agent_id, row.decision_ts))
                if copy_receipt is not None:
                    agent = json.loads(copy_receipt.payload_json)['agent']
                    targets.add((agent['target_entity'], agent['target_property']))
            if len(targets) > 1:
                raise ValueError('supervision_target_collision')
            label = min(copies, key=lambda row: (row.available_ts, row.agent_id, row.feedback_id))
            receipt = decisions.get((label.agent_id, label.decision_ts))
            if receipt is None:
                raise ValueError('missing_historical_decision_receipt')
            receipt_bytes += len(receipt.payload_json.encode())
            if receipt_bytes > MAX_RECEIPT_BYTES:
                # Resource exhaustion invalidates the whole build, not a prefix.
                raise OverflowError('dataset receipt byte cap exceeded')
            sample = _eligible_sample(label, receipt, replay, coverage)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            rejected.append((event_id, str(exc)))
            continue
        token_references += len(sample.home_observation)
        action_references += sample.available_actions.count
        if token_references > MAX_TOKEN_REFERENCES or action_references > MAX_ACTION_REFERENCES:
            raise ValueError('dataset aggregate resource cap exceeded; reduce the explicit window')
        samples.append(sample)
    samples.sort(key=lambda sample: (sample.observation_timestamp, sample.sample_id))
    dataset_id = _digest(dict(version=CONTRACT_VERSION, supervision=revision,
                              samples=[_digest(asdict(sample)) for sample in samples]))
    return TrainingDataset(CONTRACT_VERSION, tuple(samples), tuple(rejected), revision, dataset_id)


def read_archive_observations(conn, entity_ids, *, at_ts, max_records=50000, since_ts=None):
    """Read native 0.14.232 archive receipts; legacy missing clocks never qualify."""
    entities = sorted(set(map(str, entity_ids)))
    if len(entities) > 10000 or not 1 <= max_records <= 100000:
        raise ValueError('archive query resource bounds exceeded')
    if any(e.partition('.')[0] not in ('sensor', 'binary_sensor') for e in entities):
        raise ValueError('archive adapter accepts sensors only')
    since = _number(since_ts) if since_ts is not None else None
    if since is not None and since > _number(at_ts):
        raise ValueError('reversed archive replay interval')
    rows = []
    retained_bytes = 0
    for offset in range(0, len(entities), 64):
        chunk = entities[offset:offset+64]
        marks = ','.join('?' for _ in chunk)
        interval = ' AND ts>=?' if since is not None else ''
        bounds = (_number(at_ts), _number(at_ts), since) if since is not None else (_number(at_ts), _number(at_ts))
        cursor = conn.execute(f'''SELECT id,entity_id,ts,received_ts,state,attributes_json,source
            FROM entity_history WHERE entity_id IN ({marks})
            AND ts<=? AND received_ts IS NOT NULL AND received_ts<=?
            {interval} ORDER BY ts,received_ts,id LIMIT ?''',
            (*chunk, *bounds, max_records-len(rows)+1))
        columns = [item[0] for item in cursor.description]
        for raw in cursor:
            if len(rows) >= max_records:
                raise ValueError('archive replay row cap exceeded')
            row = dict(zip(columns, raw))
            retained_bytes += sum(len(str(row.get(key) or '').encode())
                                  for key in ('entity_id','state','attributes_json','source'))
            if retained_bytes > MAX_SOURCE_BYTES:
                raise ValueError('archive replay byte cap exceeded')
            rows.append(journal_observation(dict(
                contract_version=1, event_key='archive-v1:'+str(row['id']),
                entity_id=row['entity_id'], event_time=row['ts'], received_time=row['received_ts'],
                state=row['state'], attributes_json=row['attributes_json'],
                source='archive:'+str(row['source']), quality=1.,
            )))
            TRAINING_BUDGET.checkpoint('universal_archive_read', thread_name=BUDGET_THREAD)
    return tuple(rows)


def prepare_dataset(conn, agent_ids, decisions, registry_timeline, *, as_of_ts,
                    coverage=None, max_samples=MAX_SAMPLES, max_observations=50000,
                    supervision_as_of_ts=None):
    """Bounded supervision, FeatureJournal and archive reads from one transaction.

    Missing historical registry/mask coverage is an explicit rejection. Caller
    supplies a stable transaction and the historic registry timeline; there is
    no connection to HA, Engine, runtime options or current device state.
    Lifecycle callers may read newer native supervision facts/tombstones with
    supervision_as_of_ts while keeping observation replay pinned to as_of_ts.
    Late corrections still obey label-availability cutoffs in chronological_split.
    """
    if not conn.in_transaction:
        raise ValueError('caller-owned consistent SQLite read transaction required')
    labels = read_supervision(conn, agent_ids,
                              as_of_ts=as_of_ts if supervision_as_of_ts is None else supervision_as_of_ts,
                              max_rows=max_samples)
    if coverage is None or coverage.complete is not True:
        return build_dataset(labels, decisions, CausalObservationReplay((), registry_timeline=registry_timeline),
                             coverage=coverage, max_samples=max_samples)
    entities = set()
    for label in labels:
        receipt = decisions.get((label.agent_id, label.decision_ts))
        if receipt is not None:
            entities.update(json.loads(receipt.payload_json)['required_sensor_ids'])
    if len(entities) > 10000 or not 1 <= max_observations <= 100000:
        raise ValueError('observation resource bounds exceeded')
    since = coverage.start_ts
    if coverage.version == 2:
        from universal_history_collector import verify_collection_coverage
        verify_collection_coverage(conn,coverage)
        seeds = coverage.seed_receipts
        if (not 1 <= len(seeds) <= 10000 or len({r[0] for r in seeds}) != len(seeds)
                or set(r[0] for r in seeds) != set(coverage.entity_ids)
                or any(not _number(r[1]) <= _number(r[2]) <= _number(coverage.start_ts) for r in seeds)):
            raise ValueError('invalid collection seed receipts')
        since = min(since, *(float(r[1]) for r in seeds))
    journal = read_feature_journal(
        conn, entities, at_ts=as_of_ts, max_records=max_observations,
        max_bytes=MAX_SOURCE_BYTES,
        since_ts=since,
        checkpoint=lambda: TRAINING_BUDGET.checkpoint('universal_feature_read', thread_name=BUDGET_THREAD),
    )
    # V2 attests the native FeatureJournal stream. Archive/import receipts must
    # not silently repair a missing collector event or enrich its exact payload.
    # Legacy V1 keeps its established strict journal/archive merge unchanged.
    archive = (() if coverage.version == 2 else read_archive_observations(
        conn, entities, at_ts=as_of_ts, max_records=max_observations, since_ts=since))
    # Do not mix later enriched attributes into an earlier receipt. Deduplicate
    # only exact-clock equivalents; inconsistent facts at identical clocks fail.
    observations = {}
    for row in (*archive, *journal):
        key = (row.entity_id, row.event_ts, row.received_ts)
        previous = observations.get(key)
        if previous is not None and (previous.state != row.state or previous.attributes != row.attributes):
            raise ValueError('ambiguous archive/FeatureJournal observation')
        observations[key] = row  # Native FeatureJournal wins equivalent-source ties.
        if len(observations) > max_observations:
            raise ValueError('combined observation row cap exceeded')
        TRAINING_BUDGET.checkpoint('universal_observation_merge', thread_name=BUDGET_THREAD)
    if coverage.version == 2 and not set(r for r in coverage.seed_receipts if r[0] in entities) <= observations.keys():
        raise ValueError('collection seeds expired or missing')
    replay = CausalObservationReplay(observations.values(), registry_timeline=registry_timeline)
    return build_dataset(labels, decisions, replay, coverage=coverage, max_samples=max_samples)


def chronological_split(dataset, *, train_until, holdout_from, holdout_until):
    """Future-period holdout with an embargo; never random-split one episode.

    Labels created after the training cut cannot enter training, even when
    correcting an old decision. Episodes crossing either partition are removed
    entirely. A later consumer must pin dataset_id and supervision_revision.
    """
    cut, start, end = map(_number, (train_until, holdout_from, holdout_until))
    if not cut < start <= end:
        raise ValueError('strictly future holdout and positive embargo required')
    train = [s for s in dataset.samples if s.observation_timestamp <= cut and s.label_available_ts <= cut]
    holdout = [s for s in dataset.samples if start <= s.observation_timestamp <= end]
    training_episodes = {s.episode_id for s in train if s.episode_id}
    holdout_episodes = {s.episode_id for s in holdout if s.episode_id}
    crossing = training_episodes & holdout_episodes
    return (tuple(s for s in train if s.episode_id not in crossing),
            tuple(s for s in holdout if s.episode_id not in crossing))
