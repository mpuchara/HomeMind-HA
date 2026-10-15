"""Bound optional candidate work while keeping every observed target transition."""
from collections import OrderedDict
import math
import threading
import time

from context import target_value
from settings import OPTIONS

MAX_AGENTS = 256


def realtime_pending(engine):
    """Read only existing engine backpressure; do not consume any trigger."""
    with engine.lock:
        if any(getattr(engine, 'pending_target_changes', {}).values()):
            return True
        if any(getattr(engine, 'ready_target_changes', {}).values()):
            return True
        dependencies = getattr(engine, 'dependency_agents', {})
        return any(dependencies.get(eid) for eid in getattr(engine, 'dirty_entities', ()))


def max_deferral_seconds():
    try:
        value = float(OPTIONS.get('runtime_shadow_max_deferral_ms', 5000))
        return min(30000., max(0., value)) / 1000. if math.isfinite(value) else 5.
    except (TypeError, ValueError):
        return 5.


def interval_seconds():
    try:
        value = float(OPTIONS.get('runtime_shadow_observation_interval_ms', 1000))
        if not math.isfinite(value):
            raise ValueError('non-finite interval')
        return min(5000., max(0., value)) / 1000.
    except (TypeError, ValueError):
        return 1.


class ShadowObservationScheduler:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.completed = OrderedDict()
        self.retry_due = OrderedDict()
        self.executed = self.skipped = self.target_forced = self.errors = 0
        self.deferred_for_realtime = 0

    @staticmethod
    def signature(agent, policy):
        schema = getattr(policy, 'schema', None)
        return (str(agent.get('target_entity') or ''), str(agent.get('target_property') or ''),
                id(policy), tuple(getattr(schema, 'entities', ()) or ()),
                getattr(schema, 'feature_contract_version', None),
                str(getattr(policy, 'tournament_revision', '') or ''))

    def admit(self, agent, states, changed_entities, policy, *, pending_realtime=False):
        aid = str(agent['id'])
        now = self.clock()
        target = target_value(states.get(agent['target_entity']), agent['target_property'])
        signature = self.signature(agent, policy)
        interval = interval_seconds()
        with self.lock:
            previous = self.completed.get(aid)
            target_event = str(agent['target_entity']) in (changed_entities or ())
            target_changed = previous is not None and previous[1] != target
            forced = target_event or target_changed
            retry = aid in self.retry_due
            due = (previous is None or interval == 0 or forced or retry or previous[2] != signature
                   or now < previous[0] or now - previous[0] >= interval)
            if not due:
                self.skipped += 1
                return None
            if (previous is not None and interval > 0 and pending_realtime and not forced and not retry
                    and previous[2] == signature and now >= previous[0]
                    and now - previous[0] < max(interval, max_deferral_seconds())):
                self.skipped += 1
                self.deferred_for_realtime += 1
                return None
            self.target_forced += int(forced)
        # Publication happens only after successful observation. A failed attempt
        # never suppresses the next label or starts a retry cooldown.
        return aid, now, target, signature

    def finish(self, ticket, *, success):
        if ticket is None:
            return
        aid, now, target, signature = ticket
        with self.lock:
            if not success:
                self.errors += 1
                self.retry_due[aid] = True
                self.retry_due.move_to_end(aid)
                while len(self.retry_due) > MAX_AGENTS:
                    self.retry_due.popitem(last=False)
                return
            self.retry_due.pop(aid, None)
            self.executed += 1
            self.completed[aid] = now, target, signature
            self.completed.move_to_end(aid)
            while len(self.completed) > MAX_AGENTS:
                self.completed.popitem(last=False)

    def snapshot(self):
        with self.lock:
            return dict(interval_ms=interval_seconds() * 1000, executed=self.executed,
                        skipped=self.skipped, target_forced=self.target_forced, errors=self.errors,
                        tracked_agents=len(self.completed), max_agents=MAX_AGENTS,
                        deferred_for_realtime=self.deferred_for_realtime,
                        max_deferral_ms=max_deferral_seconds() * 1000,
                        retry_agents=len(self.retry_due),
                        scope='Engine-dispatched candidate observations; target events always admitted')
