"""Bounded, causal Sensor Tournament work outside Engine target workers.

Private pickle bytes are produced only from our own in-process objects. No file,
network or user payload is unpickled. One worker owns observation order. Every
retained target transition is FIFO; a discarded interval explicitly breaks paired
evidence. Promotion/probation mutations run on a fresh synchronous Root pass.
"""
from collections import OrderedDict, deque
from contextlib import contextmanager, nullcontext
import pickle
import threading
import time

from context import TemporalHistory, target_value
from shadow_observation_scheduler import interval_seconds
from runtime_sqlite_cache import memory_headroom
from settings import OPTIONS
from storage_budget import storage_status
from telemetry import TELEMETRY
from tournament_observation_clock import observation_clock
from runtime_background_compute import COMPUTE, background_compute_scope
from temporal_observation_snapshots import TemporalObservationSnapshots
from detached_mapping_snapshots import DetachedMappingSnapshots


class EngineObservationView:
    """Installed before decorators capture service.engine; binding is thread local."""
    def __init__(self, engine):
        object.__setattr__(self, '_engine', engine)
        object.__setattr__(self, '_local', threading.local())

    def __getattr__(self, name):
        frame = getattr(self._local, 'frame', None)
        if frame is not None and name in frame:
            return frame[name]
        return getattr(self._engine, name)

    def __setattr__(self, name, value):
        setattr(self._engine, name, value)

    @contextmanager
    def bind(self, frame):
        previous = getattr(self._local, 'frame', None)
        self._local.frame = frame
        try:
            yield
        finally:
            self._local.frame = previous


class _FrozenContext:
    def __init__(self, forecast, excluded):
        self._forecast = forecast
        self.excluded = excluded

    def forecast(self, target_entity, timestamp):
        return dict(self._forecast)


class DeferredTournamentObservations:
    def __init__(self, service, *, max_jobs=64, max_bytes=None):
        self.service = service
        self.engine = service.engine._engine
        self.lock = threading.RLock()
        self.observation_lock = threading.Lock()
        self.wake = threading.Event()
        self.pending = deque()
        self.gaps = OrderedDict()
        self.sync_required = OrderedDict()
        self.local = threading.local()
        self.max_jobs = max(1, min(256, int(max_jobs)))
        configured = max(1, min(256, int(OPTIONS.get('runtime_tournament_ram_mb', 64)))) * 1024**2
        self.configured_bytes = configured if max_bytes is None else int(max_bytes)
        self.max_bytes = self.configured_bytes
        self.used_bytes = 0
        self.active = False
        self.closed = False
        self.thread = None
        self.memory_sample_ts = 0.
        self.stats = dict(queued=0, processed=0, dropped=0, errors=0, gaps=0,
                          stale=0, max_depth=0, last_queue_lag_ms=None,
                          sync_observations=0, storage_skips=0, last_error=None,
                          last_capture_ms=None, peak_bytes=0, coalesced=0, pending_interval_skips=0)
        self._sample_memory()
        self.temporal_snapshots = TemporalObservationSnapshots(max_bytes=min(64*1024**2, self.max_bytes))
        self.state_snapshots = DetachedMappingSnapshots(max_bytes=min(16*1024**2, self.max_bytes))
        self.registry_snapshots = DetachedMappingSnapshots(
            max_bytes=min(8*1024**2, self.max_bytes), reuse_complete=True)

    def is_deferred(self):
        return bool(getattr(self.local, 'deferred', False))

    def require_sync(self, aid):
        with self.lock:
            self.sync_required[str(aid)] = True
            self.sync_required.move_to_end(str(aid))
            while len(self.sync_required) > 256:
                self.sync_required.popitem(last=False)

    def _gap(self, aid):
        # A gap belongs after every frame already captured for this agent. Drop
        # those pending frames too, rather than resetting before an older frame
        # and later accidentally scoring across the missing interval.
        retained = deque()
        for job in self.pending:
            if job['aid'] == str(aid):
                self.used_bytes -= job['charge']
                self.stats['dropped'] += 1
            else:
                retained.append(job)
        self.pending = retained
        self.gaps[str(aid)] = True
        self.gaps.move_to_end(str(aid))
        while len(self.gaps) > 256:
            self.gaps.popitem(last=False)

    def invalidate(self, aid):
        """Only the serialized observer calls transient gap handlers."""
        with self.service.lock:
            runtime = self.service._shadow_runtime.get(aid)
            if runtime is not None:
                runtime['pending'] = {}
                runtime['last_target'] = None
            callbacks = tuple(self.service._observation_gap_handlers.values())
        for callback in callbacks:
            callback(aid, 'deferred_gap')
        self.stats['gaps'] += 1

    def _sample_memory(self):
        now = time.monotonic()
        if now - self.memory_sample_ts < 5:
            return
        self.memory_sample_ts = now
        total, available = memory_headroom()
        limit = self.configured_bytes
        if total is not None:
            limit = min(limit, max(0, int(total * .05 * 1024**2)))
        if available is not None:
            limit = min(limit, max(0, int((available - 128) * .25 * 1024**2)))
        with self.lock:
            self.max_bytes = limit

    def _capture(self, agent, states, changed, ticket, *, timestamp=None):
        aid = str(agent['id'])
        engine = self.engine
        stamp = time.time() if timestamp is None else float(timestamp)
        with engine.lock:
            policy = (getattr(engine, 'models', {}) or {}).get(aid)
            if policy is None:
                raise ValueError('Tournament requires the observed Root policy')
            # HA state records are replacement snapshots. Freeze container membership
            # under the Engine lock before encoding a detached private byte tree.
            runtime = (getattr(engine, 'runtime', {}) or {}).get(aid) or {}
            if timestamp is None:
                stamp = float(runtime.get('last_inference_ts') or stamp)
            observed = {key: runtime[key] for key in
                        ('last_prediction', 'last_change_origin', 'last_intent', 'last_inference_ts',
                         'prediction_horizon', 'context_meta', 'automation_priors') if key in runtime}
            registry = dict(getattr(engine, 'entity_registry', {}) or {})
            relevance = dict((getattr(engine, 'context_relevance', {}) or {}).get(aid) or {})
            temporal = getattr(engine, 'temporal_history', None)
            revision = getattr(getattr(engine, '_inference_tls', None), 'state_revision',
                               getattr(engine, 'state_revision', 0))
            generation = int(getattr(self.service.store, '_provenance_generation_revision', 0) or 0)
        # No history traversal, deepcopy or encoding under the global Engine lock.
        stage = time.perf_counter()
        samples, history_charge = self.temporal_snapshots.capture(temporal, stamp)
        TELEMETRY.observe('context_capture_history', (time.perf_counter()-stage)*1000)
        stage = time.perf_counter()
        frozen_states, state_charge = self.state_snapshots.capture(states)
        frozen_registry, registry_charge = self.registry_snapshots.capture(registry)
        TELEMETRY.observe('context_capture_entities', (time.perf_counter()-stage)*1000)
        stage = time.perf_counter()
        context = getattr(engine, 'context', None)
        provider = getattr(temporal, 'home_context', None) or context
        captured_forecast = ((observed.get('context_meta') or {}).get('home_forecast')
                             if timestamp is None else None)
        forecast = (dict(captured_forecast) if captured_forecast is not None else
                    dict(provider.forecast(agent['target_entity'], stamp) or {}) if provider else {})
        baselines = getattr(temporal, '_photometric_baselines', None)
        anchors = None
        if baselines is not None:
            with baselines.lock:
                anchors = dict(baselines.entries)
        TELEMETRY.observe('context_capture_forecast', (time.perf_counter()-stage)*1000)
        stage = time.perf_counter()
        # Native arrays and Python numeric rows are copied without canonical JSON,
        # checksum encoding, policy construction or fitting on the reaction path.
        with getattr(policy, 'lock', nullcontext()):
            excluded_attrs = {'lock', 'context_engine', 'registry', 'agent'}
            if getattr(policy, '_candidate_shadow_exact_context', False):
                # This instance closure only captures Root diagnostics. The clone
                # uses the same installed class feature contract without retaining
                # a callback into a live manager or recursively pickling its locks.
                excluded_attrs.update(('features', '_candidate_shadow_exact_context'))
            attrs = {key: value for key, value in vars(policy).items()
                     if key not in excluded_attrs}
            blob = pickle.dumps(dict(agent=dict(agent), changed=tuple(changed or ()),
                runtime=observed, relevance=relevance,
                anchors=anchors, revision=revision, generation=generation, forecast=forecast,
                has_context=provider is not None or captured_forecast is not None,
                excluded=set(getattr(context, 'excluded', ()) or ()),
                policy=attrs, timestamp=stamp), protocol=pickle.HIGHEST_PROTOCOL)
            identity = (id(policy), getattr(policy, 'tournament_revision', None),
                        tuple(getattr(getattr(policy, 'schema', None), 'entities', ()) or ()))
        TELEMETRY.observe('context_capture_policy', (time.perf_counter()-stage)*1000)
        # Budget includes conservative decoded working space as well as retained
        # bytes. One active frame remains charged until completion.
        return dict(aid=aid, blob=blob, history=samples, states=frozen_states, registry=frozen_registry,
                    charge=len(blob)*8+history_charge+state_charge+registry_charge, identity=identity,
                    policy_type=type(policy), ticket=ticket, queued=time.monotonic(),
                    target=target_value(states.get(agent['target_entity']), agent['target_property']),
                    target_event=agent['target_entity'] in (changed or ()))

    def submit(self, agent, states, changed, ticket, *, timestamp=None):
        aid = str(agent['id'])
        with self.lock:
            if self.closed:
                return True
            if aid in self.sync_required:
                return False
            if storage_status(self.service.store).get('level', 'normal') != 'normal':
                self.stats['storage_skips'] += 1
                self._gap(aid)
                return True
            latest = next((row for row in reversed(self.pending) if row['aid'] == aid), None)
            if (latest is not None and not latest['target_event']
                    and agent['target_entity'] not in (changed or ())
                    and latest['target'] == target_value(states.get(agent['target_entity']), agent['target_property'])
                    and time.monotonic()-latest['queued'] < interval_seconds()):
                self.stats['pending_interval_skips'] += 1
                return True
        try:
            capture_started = time.perf_counter()
            job = self._capture(agent, states, changed, ticket, timestamp=timestamp)
            self.stats['last_capture_ms'] = (time.perf_counter()-capture_started)*1000
            TELEMETRY.observe('context_shadow_capture', self.stats['last_capture_ms'])
        except Exception as exc:
            self.stats['errors'] += 1
            self.stats['last_error'] = type(exc).__name__ + ': ' + str(exc)
            with self.lock:
                self._gap(aid)
            return True  # Never turn capture failure into foreground training.
        with self.lock:
            latest = next((row for row in reversed(self.pending) if row['aid'] == aid), None)
            if (latest is not None and latest['identity'] == job['identity']
                    and not latest['target_event'] and not job['target_event']
                    and latest['target'] == job['target']):
                # Only redundant sensor refreshes at the same target value can
                # coalesce. Never erase an observed target edge or cross one.
                self.pending.remove(latest)
                self.used_bytes -= latest['charge']
                self.stats['coalesced'] += 1
            while self.pending and (len(self.pending) >= self.max_jobs or
                                    self.used_bytes + job['charge'] > self.max_bytes):
                old = self.pending.popleft()
                self.used_bytes -= old['charge']
                self.stats['dropped'] += 1
                self._gap(old['aid'])
            if self.closed or self.used_bytes + job['charge'] > self.max_bytes:
                self.stats['dropped'] += 1
                self._gap(aid)
                return True
            self.pending.append(job)
            self.used_bytes += job['charge']
            self.stats['peak_bytes'] = max(self.stats['peak_bytes'], self.used_bytes)
            self.stats['queued'] += 1
            self.stats['max_depth'] = max(self.stats['max_depth'], len(self.pending))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run,
                    name='adaptive-ai-tournament-observer', daemon=True)
                self.thread.start()
            self.wake.set()
        return True

    def _current(self, job):
        policy = (getattr(self.engine, 'models', {}) or {}).get(job['aid'])
        identity = (id(policy), getattr(policy, 'tournament_revision', None),
                    tuple(getattr(getattr(policy, 'schema', None), 'entities', ()) or ()))
        return identity == job['identity']

    def _execute(self, job):
        aid = job['aid']
        with self.lock:
            gap = self.gaps.pop(aid, None)
            synchronous = aid in self.sync_required
        if gap:
            self.invalidate(aid)
        if synchronous or not self._current(job):
            self.stats['stale'] += 1
            self.invalidate(aid)
            return
        # This is the only loads call: bytes came from _capture, never external data.
        packet = pickle.loads(job['blob'])
        packet['states'], packet['registry'] = job['states'], job['registry']
        if packet['generation'] != int(getattr(self.service.store, '_provenance_generation_revision', 0) or 0):
            self.stats['stale'] += 1
            self.invalidate(aid)
            return
        probation = getattr(self.service, 'schema_probation_active', None)
        if callable(probation) and probation(aid):
            self.require_sync(aid)
            self.invalidate(aid)
            return
        frozen = _FrozenContext(packet['forecast'], packet['excluded']) if packet['has_context'] else None
        temporal = TemporalHistory()
        temporal.samples = job['history']
        temporal.home_context = frozen
        if packet['anchors'] is not None:
            temporal.photometric_baselines().entries.update(packet['anchors'])
        policy = job['policy_type'].__new__(job['policy_type'])
        vars(policy).update(packet['policy'])
        policy.lock = threading.RLock()
        policy._tournament_source_identity = job['identity']
        policy.agent, policy.registry, policy.context_engine = packet['agent'], packet['registry'], frozen
        frame = dict(models={aid: policy}, runtime={aid: packet['runtime']},
                     state_map=packet['states'], entity_registry=packet['registry'],
                     context_relevance={aid: packet['relevance']}, temporal_history=temporal,
                     state_revision=packet['revision'], context=frozen)
        self.local.deferred = True
        try:
            with self.service.engine.bind(frame), observation_clock(packet['timestamp']):
                success = self.service._execute_observation(packet['agent'], packet['states'],
                                                           packet['changed'], job['ticket'])
                if success is False:
                    with self.lock:
                        self._gap(aid)
        finally:
            self.local.deferred = False
        self.stats['processed'] += 1

    def _run(self):
        while True:
            self._sample_memory()
            with self.lock:
                stop = getattr(self.engine, 'stop_event', None)
                if self.closed or (stop is not None and stop.is_set()):
                    while self.pending:
                        job = self.pending.popleft()
                        self.used_bytes -= job['charge']
                        self.stats['dropped'] += 1
                    return
                job = self.pending.popleft() if self.pending else None
                self.active = job is not None
                if job is None:
                    self.wake.clear()
            if job is None:
                self.wake.wait(.5)
                continue
            try:
                self.stats['last_queue_lag_ms'] = max(0., (time.monotonic()-job['queued'])*1000)
                with self.observation_lock:
                    session = getattr(self.service.store, 'inference_connection_session', None)
                    with (session() if callable(session) else nullcontext(), background_compute_scope()):
                        self._execute(job)
            except Exception as exc:
                self.stats['errors'] += 1
                self.stats['last_error'] = type(exc).__name__ + ': ' + str(exc)
                with self.lock:
                    self._gap(job['aid'])
                self.service.report_runtime_error(exc)
            finally:
                with self.lock:
                    self.used_bytes -= job['charge']
                    self.active = False

    def run_synchronous(self, agent, states, changed, ticket):
        """Rare lifecycle work never waits behind a long background fit."""
        aid = str(agent['id'])
        if not self.observation_lock.acquire(blocking=False):
            with self.lock:
                self._gap(aid)
            return
        try:
            with self.lock:
                retained = deque()
                for job in self.pending:
                    if job['aid'] == aid:
                        self.used_bytes -= job['charge']
                        self.stats['dropped'] += 1
                        self.gaps[aid] = True
                    else:
                        retained.append(job)
                self.pending = retained
                gap = self.gaps.pop(aid, None)
            if gap:
                self.invalidate(aid)
            success = self.service._execute_observation(agent, states, changed, ticket)
            if success is False:
                with self.lock:
                    self._gap(aid)
            probation = getattr(self.service, 'schema_probation_active', None)
            if not callable(probation) or not probation(aid):
                with self.lock:
                    self.sync_required.pop(aid, None)
            self.stats['sync_observations'] += 1
        except Exception as exc:
            self.stats['errors'] += 1
            self.stats['last_error'] = type(exc).__name__ + ': ' + str(exc)
            with self.lock:
                self._gap(aid)
            self.service.report_runtime_error(exc)
        finally:
            self.observation_lock.release()

    def close(self, timeout=6.):
        with self.lock:
            self.closed = True
            self.wake.set()
            worker = self.thread
        if worker is not None:
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    def snapshot(self):
        with self.lock:
            return dict(self.stats, pending=len(self.pending), active=self.active,
                        bytes=self.used_bytes, max_bytes=self.max_bytes,
                        configured_bytes=self.configured_bytes, max_jobs=self.max_jobs,
                        sync_required=len(self.sync_required), gap_agents=len(self.gaps),
                        temporal_snapshots=self.temporal_snapshots.snapshot(),
                        state_snapshots=self.state_snapshots.snapshot(),
                        registry_snapshots=self.registry_snapshots.snapshot(),
                        isolated_compute=COMPUTE.snapshot(),
                        semantics='FIFO causal frames; explicit evidence gaps on discard')
