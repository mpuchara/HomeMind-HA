"""Bounded, observable admission before the existing training mutation routes.

Receipt of a request is RAM-only, not a durable training queue acknowledgement.
The worker dispatches the original routes, including Candidate guards, Control
release and durable waiting-state writes. No replay/reset runs on the HTTP thread.
"""
from collections import deque
from contextlib import nullcontext
import re
import threading
import time
from urllib.parse import quote
import uuid

from telemetry import RUNTIME_DEBUG


class AdmissionConflict(ValueError):
    pass


class _AdmissionHttp:
    def __init__(self, owner, agent_id, action):
        self.owner = owner
        self.path = '/api/agents/' + quote(agent_id, safe='') + ('/resume' if action == 'resume' else '/learning')
        self.result = None

    def require_trusted_client(self):
        # The public intake route already authenticated this exact immutable action.
        return True

    def require_runtime(self):
        if not self.owner.closed and self.owner.runtime_ready():
            return True
        self.send_json(503, {'error': 'Runtime restarted or is stopping; confirm training again when ready'})
        return False

    def send_json(self, status, payload):
        self.result = (int(status), payload)


class TrainingAdmission:
    def __init__(self, queue, registry, runtime_ready, *, capacity=128, retention_seconds=900):
        self.queue, self.registry, self.runtime_ready = queue, registry, runtime_ready
        self.capacity = max(1, int(capacity))
        self.retention_seconds = max(60., float(retention_seconds))
        self.boot_id = uuid.uuid4().hex
        self.cv = threading.Condition()
        self.records, self.aliases, self.by_agent, self.pending = {}, {}, {}, deque()
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='adaptive-ai-training-admission', daemon=True)
        self.thread.start()

    def _view(self, row):
        return {**{key: value for key, value in row.items() if not key.startswith('_')},
                'boot_id': self.boot_id,
                'elapsed_seconds': round(max(0., time.monotonic()-row['_monotonic']), 2)}

    def submit(self, request_id, agent_id, action):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
            raise ValueError('Invalid training request id')
        if action not in ('resume', 'rebuild'):
            raise ValueError('Training action must be resume or rebuild')
        agent_id = str(agent_id)
        with self.cv:
            if self.closed:
                raise RuntimeError('Training admission is stopping')
            now = time.monotonic()
            for key, row in list(self.records.items()):
                if row['state'] in ('accepted', 'failed') and now-row.get('_finished_monotonic', now) > self.retention_seconds:
                    del self.records[key]
                    self.aliases = {alias: target for alias, target in self.aliases.items() if target != key}
            canonical_id = self.aliases.get(request_id, request_id)
            existing = self.records.get(canonical_id)
            if existing:
                if existing['agent_id'] != agent_id or existing['action'] != action:
                    raise AdmissionConflict('Request id belongs to a different training action')
                return self._view(existing)
            previous = self.records.get(self.by_agent.get(agent_id))
            if previous:
                if previous['action'] != action:
                    raise AdmissionConflict('Another training request for this agent is still being admitted')
                if len(self.records)+len(self.aliases) >= self.capacity:
                    raise RuntimeError('Training request capacity reached; try again later')
                # Remember the caller's id too. If its acknowledgement is lost,
                # lookup/retry of that id must resolve to this same operation even
                # after the canonical request has completed.
                self.aliases[request_id] = previous['request_id']
                return self._view(previous)
            # Do not evict recent results: a retried request id must not replay a
            # destructive mutation just because the bounded ledger became full.
            if len(self.records)+len(self.aliases) >= self.capacity:
                raise RuntimeError('Training request capacity reached; try again later')
            row = dict(request_id=request_id, agent_id=agent_id, action=action,
                       state='pending', phase='waiting', created_at=time.time(),
                       message='Request received; waiting to enter the training queue', _monotonic=now)
            self.records[request_id] = row
            self.by_agent[agent_id] = request_id
            self.pending.append(request_id)
            self.cv.notify_all()
            return self._view(row)

    def get(self, request_id):
        with self.cv:
            row = self.records.get(self.aliases.get(request_id, request_id))
            return self._view(row) if row else None

    def snapshot(self):
        with self.cv:
            return dict(boot_id=self.boot_id, retained=len(self.records)+len(self.aliases),
                        operations=len(self.records), aliases=len(self.aliases), capacity=self.capacity,
                        retention_seconds=self.retention_seconds,
                        pending=[self._view(row) for row in self.records.values() if row['state']=='pending'],
                        worker_alive=self.thread.is_alive(),
                        semantics='RAM receipt only; accepted means original queue route completed')

    def _phase(self, row, phase):
        messages = {'validating': 'Checking training request',
                    'agent_state': 'Reading the saved training state',
                    'control_release': 'Releasing Control before training',
                    'waiting_state': 'Waiting for database access to save queue admission',
                    'queue': 'Adding training to the queue'}
        with self.cv:
            row['phase'] = phase
            row['message'] = messages.get(phase, phase)

    def _run(self):
        while True:
            with self.cv:
                self.cv.wait_for(lambda: self.closed or self.pending)
                if self.closed:
                    return
                row = self.records[self.pending.popleft()]
            trace = RUNTIME_DEBUG.begin('training_admission', request_id=row['request_id'],
                                        agent_id=row['agent_id'], action=row['action'])
            status, payload = 500, {'error': 'Training admission did not complete'}
            try:
                self._phase(row, 'validating')
                priority = getattr(self.queue.store, 'training_admission_priority', None)
                with priority() if callable(priority) else nullcontext():
                    self.queue._admission_local.progress = lambda phase: self._phase(row, phase)
                    http = _AdmissionHttp(self, row['agent_id'], row['action'])
                    if not self.registry.dispatch('POST' if row['action']=='resume' else 'DELETE', http):
                        http.send_json(404, {'error': 'Training action route is unavailable'})
                    if http.result is not None:
                        status, payload = http.result
            except Exception as exc:
                payload = {'error': f'Training admission failed: {type(exc).__name__}: {exc}'}
            finally:
                self.queue._admission_local.progress = None
                RUNTIME_DEBUG.end(trace, status='ok' if status==202 else 'error', response_status=status)
            # A callback may return 5xx or raise after appending its job (for
            # example while emitting a later diagnostic). Recover an existing
            # acknowledgement, but never override an explicit 4xx guard rejection.
            if status >= 500 and not self.closed:
                status_for = getattr(self.queue, 'status_for', None)
                try:
                    queued = status_for(row['agent_id']) if callable(status_for) else None
                except Exception:
                    queued = None
                if queued and queued.get('state') in ('queued', 'active'):
                    status, payload = 202, {
                        'state': queued['state'], 'rebuild': bool(queued.get('rebuild')),
                        'queue_position': queued.get('position', 0),
                        'message': 'Training is already registered in the queue; acknowledgement recovered'}
            with self.cv:
                if not isinstance(payload, dict):
                    status, payload = 500, {'error': 'Invalid training admission response'}
                payload = {key: value for key, value in payload.items() if key in (
                    'message', 'error', 'state', 'queue_position', 'ahead', 'rebuild',
                    'resumed', 'learning_path', 'rebuild_reason')}
                for key in ('message', 'error'):
                    if key in payload:
                        payload[key] = str(payload[key])[:4096]
                row.update(state='accepted' if status==202 else 'failed', phase='complete',
                           _finished_monotonic=time.monotonic(),
                           response_status=status, response=payload,
                           message=str(payload.get('message') or payload.get('error') or 'Training request completed'))
                if self.by_agent.get(row['agent_id']) == row['request_id']:
                    self.by_agent.pop(row['agent_id'], None)

    def stop(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()
