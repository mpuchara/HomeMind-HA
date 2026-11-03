"""U05b.3 explicit offline checkpoint authority; never installed in runtime.

SQLite is the trust boundary. Exported JSON is not authority. Acceptance and
complete bytes share one FULL-synchronous transaction in the native Store DB.
No triggers, workers, active model replacement, or device execution are added.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
import math
import secrets
import threading

from universal_ha_adapter import RegistryEpoch, RegistryTimeline, SemanticRegistry
from universal_training_data import (HistoricalDecision, ReplayCoverage, prepare_dataset,
                                     chronological_split, _digest)
from universal_scorer_training import (checkpoint_bytes, load_core_checkpoint,
                                      train_scorer, _yield)
from universal_scorer_validation import ValidationPolicy, validate_core_artifact

VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_ATTEMPTS = 256
MAX_HOLDOUT_CLAIMS = 65536
MAX_LEDGER_BYTES = 64 * 1024 * 1024
_ADMISSION = threading.BoundedSemaphore(1)


@contextmanager
def _admitted():
    if not _ADMISSION.acquire(blocking=False):
        raise ValueError('offline checkpoint lifecycle busy')
    try:
        _yield()
        yield
    finally:
        _ADMISSION.release()


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate checkpoint key')
        result[key] = value
    return result


def _decode(raw, limit=MAX_BYTES):
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise ValueError('checkpoint byte bound')
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


class NativeDatasetSource:
    """Detached historical receipts; current labels/history ALWAYS come from Store.

    Supply the historical registry epochs explicitly, never today's HA registry.
    The source bytes can be persisted by the caller for recovery; load requires
    this exact source again and fails closed without it. This is not Correct history.
    """
    def __init__(self, raw):
        doc = _decode(raw, MAX_SOURCE_BYTES)
        if doc['version'] != VERSION or doc['evidence'] not in ('offline', 'synthetic'):
            raise ValueError('unsupported source contract')
        if (not 1 <= len(doc['agent_ids']) <= 256 or len(doc['decisions']) > 2048
                or len(doc['epochs']) > 2048 or not math.isfinite(doc['as_of_ts'])):
            raise ValueError('source bounds')
        self.raw = _bytes(doc)
        self.source_id = hashlib.sha256(self.raw).hexdigest()

    @classmethod
    def create(cls, *, agent_ids, decisions, epochs, coverage, as_of_ts, evidence='offline'):
        if not 1 <= len(agent_ids) <= 256 or len(decisions) > 2048 or len(epochs) > 2048:
            raise ValueError('source bounds')
        rows = []
        for epoch in epochs:
            reg = epoch.registry
            rows.append(dict(effective_ts=epoch.effective_ts, received_ts=epoch.received_ts,
                             registry=dict(revision=reg.revision,
                                           entities={k: dict(v) for k, v in reg.entities.items()},
                                           devices={k: dict(v) for k, v in reg.devices.items()},
                                           calibrations={k: dict(v) for k, v in reg.calibrations.items()})))
        proof = asdict(coverage); proof['entity_ids'] = sorted(coverage.entity_ids)
        return cls(_bytes(dict(version=VERSION, evidence=evidence, agent_ids=sorted(set(agent_ids)),
                               decisions=[dict(agent_id=k[0], at=k[1], receipt=asdict(v))
                                          for k, v in sorted(decisions.items())],
                               epochs=rows, coverage=proof, as_of_ts=as_of_ts)))

    def dataset(self, conn):
        doc = _decode(self.raw, MAX_SOURCE_BYTES)
        decisions = {(r['agent_id'], r['at']): HistoricalDecision(**r['receipt'])
                     for r in doc['decisions']}
        epochs = [RegistryEpoch(r['effective_ts'], r['received_ts'],
                                SemanticRegistry.create(**r['registry'])) for r in doc['epochs']]
        proof = doc['coverage']; proof['entity_ids'] = frozenset(proof['entity_ids'])
        return prepare_dataset(conn, doc['agent_ids'], decisions, RegistryTimeline(epochs),
                               as_of_ts=doc['as_of_ts'], coverage=ReplayCoverage(**proof),
                               supervision_as_of_ts=1e300)

    def is_granular(self):
        return _decode(self.raw,MAX_SOURCE_BYTES)['coverage'].get('version')==3

    def guard(self,conn,data):
        """Bounded V3 publication CAS: verify retained scope and current labels.

        Native triggers prohibit an unnoticed change of required historical
        values. Unrelated writes need not revoke an unchanged scoped dataset.
        Caller owns the writer transaction; no concurrent COMMIT can pass it.
        """
        from universal_history_collector import verify_collection_coverage
        from universal_training_data import read_supervision
        doc=_decode(self.raw,MAX_SOURCE_BYTES)
        proof=doc['coverage'];proof['entity_ids']=frozenset(proof['entity_ids'])
        verify_collection_coverage(conn,ReplayCoverage(**proof))
        labels=read_supervision(conn,doc['agent_ids'],as_of_ts=1e300)
        revision=_digest(sorted((asdict(row) for row in labels),key=lambda row:json.dumps(row,sort_keys=True)))
        if revision!=data.supervision_revision:
            raise ValueError('native supervision changed before scoped acceptance')


@contextmanager
def _connection(store):
    # Reject runtime caches; restore per-connection durability configuration.
    if getattr(store._connection_session, 'connection', None) is not None:
        raise ValueError('dedicated offline connection required; no runtime SQLite session')
    with store.explicit_sqlite(), store.conn() as conn:
        previous = conn.execute('PRAGMA synchronous').fetchone()[0]
        conn.execute('PRAGMA synchronous=FULL')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute(f'PRAGMA synchronous={int(previous)}')


def _epoch(conn):
    return conn.execute('PRAGMA data_version').fetchone()[0]


def _snapshot(conn, source):
    before = _epoch(conn)
    conn.execute('BEGIN')
    try:
        data = source.dataset(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if _epoch(conn) != before and not source.is_granular():
        raise ValueError('native evidence changed during snapshot; retry explicitly')
    return data, before


class CheckpointLifecycle:
    """Explicit bounded offline service. The native DB is trusted, imports are not.

    Instantiate only after native U05a schemas exist. Key and sealed artifacts
    belong to this Store; copying a report or JSON to another Store grants nothing.
    Holdout reservations persist on failure/crash and cannot be deleted via API.
    """
    def __init__(self, store):
        self.store = store
        with _connection(store) as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('CREATE TABLE IF NOT EXISTS universal_checkpoint_issuer (id INTEGER PRIMARY KEY CHECK(id=1), key BLOB NOT NULL)')
            c.execute('INSERT OR IGNORE INTO universal_checkpoint_issuer VALUES (1,?)', (secrets.token_bytes(32),))
            c.execute('''CREATE TABLE IF NOT EXISTS universal_checkpoint_attempts (
                id TEXT PRIMARY KEY, core_id TEXT NOT NULL, policy_id TEXT NOT NULL,
                source_id TEXT NOT NULL, holdout_id TEXT NOT NULL, status TEXT NOT NULL,
                report BLOB, reproductions INTEGER NOT NULL DEFAULT 0)''')
            c.execute('''CREATE TABLE IF NOT EXISTS universal_checkpoint_holdouts (
                token TEXT PRIMARY KEY, attempt_id TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS universal_validated_checkpoints (
                id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                artifact BLOB NOT NULL, reason TEXT)''')

    def _key(self, c):
        return bytes(c.execute('SELECT key FROM universal_checkpoint_issuer WHERE id=1').fetchone()[0])

    def _reserve(self, c, core, data, source, policy):
        core_id = _digest(core); policy_id = _digest(dict(version=VERSION, **asdict(policy)))
        _, holdout = chronological_split(data, **core['split'])
        tokens = set()
        for s in holdout:
            a = s.available_actions.candidates[0]
            # Labels, dataset ids and policy names cannot rename an exposed case.
            tokens.update((_digest(['event', s.supervision_event_id]),
                           _digest(['episode', s.episode_id]),
                           _digest(['physical', a.entity_id, a.property_name, s.observation_timestamp]),
                           _digest(['time', math.floor(s.observation_timestamp / 60.)])))
            _yield()
        if not tokens:
            raise ValueError('empty holdout')
        holdout_id = _digest(sorted(tokens))
        attempt_id = _digest([VERSION, core_id, policy_id, source.source_id, holdout_id])
        c.execute('BEGIN IMMEDIATE')
        prior = c.execute('SELECT status FROM universal_checkpoint_attempts WHERE id=?', (attempt_id,)).fetchone()
        if prior:
            c.rollback()
            if prior[0] != 'published':
                raise ValueError('attempt already consumed or interrupted; use a fresh holdout')
            return attempt_id, True
        if c.execute('SELECT COUNT(*) FROM universal_checkpoint_attempts').fetchone()[0] >= MAX_ATTEMPTS:
            raise ValueError('attempt capacity reached; preserve ledger')
        if c.execute('SELECT COUNT(*) FROM universal_checkpoint_holdouts').fetchone()[0] + len(tokens) > MAX_HOLDOUT_CLAIMS:
            raise ValueError('holdout ledger capacity reached; preserve evidence')
        ordered = sorted(tokens)
        for offset in range(0, len(ordered), 64):
            chunk = ordered[offset:offset+64]
            marks = ','.join('?' for _ in chunk)
            if c.execute(f'SELECT 1 FROM universal_checkpoint_holdouts WHERE token IN ({marks}) LIMIT 1', chunk).fetchone():
                raise ValueError('holdout already exposed; independent future holdout required')
        c.execute('INSERT INTO universal_checkpoint_attempts (id,core_id,policy_id,source_id,holdout_id,status) VALUES (?,?,?,?,?,?)',
                  (attempt_id, core_id, policy_id, source.source_id, holdout_id, 'validating'))
        c.executemany('INSERT INTO universal_checkpoint_holdouts VALUES (?,?)',
                      [(t, attempt_id) for t in sorted(tokens)])
        c.commit()  # Durable BEFORE exposing holdout labels to the quality gate.
        return attempt_id, False

    def publish(self, checkpoint, source, *, policy=ValidationPolicy(), cancel=None):
        """Recompute evidence; caller-supplied passed reports are never accepted."""
        with _admitted():
            return self._publish(checkpoint, source, policy=policy, cancel=cancel)

    def _publish(self, checkpoint, source, *, policy, cancel):
        # Detach caller mutations and reject trained=true before any reservation.
        raw = checkpoint_bytes(checkpoint)
        load_core_checkpoint(raw)
        core = _decode(raw)
        if core.get('holdout_evaluated') is not False:
            raise ValueError('holdout metrics already disclosed; rebuild with a fresh holdout')
        source = NativeDatasetSource(source.raw)
        policy.validate()
        with _connection(self.store) as c:
            data, epoch = _snapshot(c, source)
            if (core['dataset_id'], core['supervision_revision']) != (data.dataset_id, data.supervision_revision):
                raise ValueError('dataset or supervision revision changed')
            attempt, repeat = self._reserve(c, core, data, source, policy)
            report = validate_core_artifact(core, data, policy=policy, cancel=cancel)
            _yield(cancel)
            c.execute('BEGIN IMMEDIATE')
            if source.is_granular():
                source.guard(c,data)
            elif _epoch(c) != epoch:
                raise ValueError('native evidence changed before publication')
            if repeat:
                row = c.execute('SELECT artifact,status FROM universal_validated_checkpoints WHERE attempt_id=?', (attempt,)).fetchone()
                if row is None or row[1] != 'qualified':
                    raise ValueError('artifact no longer qualified')
                artifact = self._verify(bytes(row[0]), c)
                if artifact['report'] != report:
                    raise ValueError('validation reproduction mismatch')
                c.execute('UPDATE universal_checkpoint_attempts SET reproductions=reproductions+1 WHERE id=?', (attempt,))
                return dict(status='reproduced', artifact_id=artifact['artifact_id'], independent_evidence=False)
            if report['status'] != 'passed':
                c.execute('UPDATE universal_checkpoint_attempts SET status=?,report=? WHERE id=?',
                          (report['status'], _bytes(report), attempt))
                return dict(status=report['status'], report=report, independent_evidence=False)
            holdout_id = c.execute('SELECT holdout_id FROM universal_checkpoint_attempts WHERE id=?', (attempt,)).fetchone()[0]
            artifact = dict(version=VERSION, contract='HomeMindValidatedNeuralArtifact', stage='U05b.3',
                            qualification='offline_shadow_only', trained=True, holdout_id=holdout_id,
                            control_eligible=False, promotion_eligible=False,
                            core=core, core_id=_digest(core), encoder_id=core['encoder_id'],
                            dataset_id=data.dataset_id, supervision_revision=data.supervision_revision,
                            source_id=source.source_id, policy=asdict(policy),
                            policy_id=_digest(dict(version=VERSION, **asdict(policy))),
                            report=report, report_id=_digest(report), attempt_id=attempt)
            artifact['artifact_id'] = _digest(artifact)
            artifact['seal'] = hmac.new(self._key(c), _bytes(artifact), hashlib.sha256).hexdigest()
            encoded = _bytes(artifact)
            if len(encoded) > MAX_BYTES:
                raise ValueError('validated artifact byte bound')
            retained = c.execute('SELECT COALESCE(SUM(LENGTH(artifact)),0) FROM universal_validated_checkpoints').fetchone()[0]
            if retained + len(encoded) > MAX_LEDGER_BYTES:
                raise ValueError('artifact ledger capacity reached; preserve evidence')
            c.execute('INSERT INTO universal_validated_checkpoints VALUES (?,?,?,?,NULL)',
                      (artifact['artifact_id'], attempt, 'qualified', encoded))
            c.execute('UPDATE universal_checkpoint_attempts SET status=?,report=? WHERE id=?',
                      ('published', _bytes(report), attempt))
            return dict(status='published', artifact_id=artifact['artifact_id'], independent_evidence=True)

    def _verify(self, raw, c):
        try:
            return self._verify_structure(raw, c)
        except (KeyError, TypeError, AttributeError, OverflowError, IndexError) as exc:
            raise ValueError('malformed validated artifact') from exc

    def _verify_structure(self, raw, c):
        artifact = _decode(raw)
        body = dict(artifact); seal = body.pop('seal')
        if not isinstance(seal, str) or not hmac.compare_digest(
                seal, hmac.new(self._key(c), _bytes(body), hashlib.sha256).hexdigest()):
            raise ValueError('invalid artifact seal')
        identity = body.pop('artifact_id')
        if _digest(body) != identity or body['version'] != VERSION:
            raise ValueError('invalid artifact identity or version')
        if (body['qualification'] != 'offline_shadow_only' or body['trained'] is not True
                or body['control_eligible'] is not False or body['promotion_eligible'] is not False):
            raise ValueError('invalid qualification boundary')
        if body['core_id'] != _digest(body['core']) or body['report_id'] != _digest(body['report']):
            raise ValueError('invalid artifact binding')
        load_core_checkpoint(checkpoint_bytes(body['core']))
        return artifact

    def _load(self, artifact_id, source):
        with _admitted():
            return self._load_admitted(artifact_id, source)

    def _load_admitted(self, artifact_id, source):
        source = NativeDatasetSource(source.raw)
        with _connection(self.store) as c:
            data, epoch = _snapshot(c, source)
            c.execute('BEGIN IMMEDIATE')
            if source.is_granular():
                source.guard(c,data)
            elif _epoch(c) != epoch:
                raise ValueError('native evidence changed during recovery')
            row = c.execute('SELECT artifact,status FROM universal_validated_checkpoints WHERE id=?', (artifact_id,)).fetchone()
            if row is None or row[1] != 'qualified':
                raise ValueError('artifact absent or revoked')
            try:
                artifact = self._verify(bytes(row[0]), c)
            except ValueError:
                c.execute("UPDATE universal_validated_checkpoints SET status='corrupt',reason='integrity check failed' WHERE id=?", (artifact_id,))
                c.commit()
                raise
            attempt = c.execute('SELECT status FROM universal_checkpoint_attempts WHERE id=?',
                                (artifact['attempt_id'],)).fetchone()
            if artifact['artifact_id'] != artifact_id or attempt is None or attempt[0] != 'published':
                raise ValueError('incomplete acceptance registry')
            if artifact['source_id'] != source.source_id:
                raise ValueError('wrong historical source')
            if (artifact['dataset_id'], artifact['supervision_revision']) != (data.dataset_id, data.supervision_revision):
                c.execute('UPDATE universal_validated_checkpoints SET status=?,reason=? WHERE id=?',
                          ('revoked', 'native dataset or supervision changed', artifact_id))
                c.commit()  # Revocation must survive the exception and restart.
                raise ValueError('artifact revoked; rebuild from a clean parent')
            return load_core_checkpoint(checkpoint_bytes(artifact['core']))

    def load(self, artifact_id, source):
        self._load(artifact_id, source)
        return QualifiedOfflineModel(self, artifact_id, source)

    def recover(self):
        """Explicit administrative recovery: burn pending evidence, never accept it.

        Also safe with a live validator: its final CAS observes this commit and
        refuses publication. Interrupted attempts require a new future holdout.
        """
        with _connection(self.store) as c:
            c.execute('BEGIN IMMEDIATE')
            return c.execute("UPDATE universal_checkpoint_attempts SET status='interrupted' WHERE status='validating'").rowcount

    def revoke(self, artifact_id, *, reason):
        """Permanent local disqualification; preserves bytes and exposure ledger."""
        if not isinstance(reason, str) or not 1 <= len(reason.encode()) <= 512:
            raise ValueError('bounded explicit revocation reason required')
        with _connection(self.store) as c:
            c.execute('BEGIN IMMEDIATE')
            return c.execute("UPDATE universal_validated_checkpoints SET status='revoked',reason=? WHERE id=? AND status='qualified'",
                             (reason, artifact_id)).rowcount == 1

    def rebuild(self, source, *, parent_seed=43, encoder_seed=41, predecessor_id=None, **training_options):
        """Train an isolated child from an explicitly clean U03 parent, never weights
        from an optimized or revoked CoreScorer. Publication is a separate gate.
        """
        with _admitted():
            return self._rebuild(source, parent_seed=parent_seed, encoder_seed=encoder_seed,
                                 predecessor_id=predecessor_id, **training_options)

    def _rebuild(self, source, *, parent_seed, encoder_seed, predecessor_id, **training_options):
        from universal_action_scorer import UniversalActionScorer
        from universal_neural_encoder import SharedSetEncoder
        # Construct from seeds, never accept caller tensors disguised as U03.
        parent = UniversalActionScorer(seed=parent_seed)
        encoder = SharedSetEncoder(seed=encoder_seed)
        source = NativeDatasetSource(source.raw)
        with _connection(self.store) as c:
            if predecessor_id is not None:
                row = c.execute('SELECT artifact FROM universal_validated_checkpoints WHERE id=?', (predecessor_id,)).fetchone()
                if row is None or self._verify(bytes(row[0]), c)['artifact_id'] != predecessor_id:
                    raise ValueError('unknown predecessor artifact')
            data, _ = _snapshot(c, source)
        if 'evaluate_holdout' in training_options:
            raise ValueError('lifecycle rebuild always withholds holdout metrics')
        result = train_scorer(parent, encoder, data, evaluate_holdout=False, **training_options)
        if result.get('checkpoint') is not None:
            core = result['checkpoint']
            core.pop('checksum')
            core['initialization'] = dict(parent_seed=parent_seed, encoder_seed=encoder_seed)
            core['rebuild_of'] = predecessor_id
            core['rebuild_strategy'] = 'fresh_seed_no_weight_reuse'
            core['checksum'] = _digest(core)
        return result


class QualifiedOfflineModel:
    """Freshness guard on every explicit offline projection; no cached authority."""
    control_eligible = False
    promotion_eligible = False

    def __init__(self, lifecycle, artifact_id, source):
        self._lifecycle, self.artifact_id, self._source = lifecycle, artifact_id, source

    def rank_context(self, context):
        model = self._lifecycle._load(self.artifact_id, self._source)
        result = model.rank_context(context)
        self._lifecycle._load(self.artifact_id, self._source)
        return result
