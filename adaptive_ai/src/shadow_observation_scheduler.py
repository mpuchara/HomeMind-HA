"""Bound optional candidate work while keeping every observed target transition."""
from collections import OrderedDict
import math
import threading
import time

from context import target_value
from settings import OPTIONS

MAX_AGENTS = 256


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
        self.executed = self.skipped = self.target_forced = self.errors = 0

    @staticmethod
    def signature(agent, policy):
        schema = getattr(policy, 'schema', None)
        return (str(agent.get('target_entity') or ''), str(agent.get('target_property') or ''),
                id(policy), tuple(getattr(schema, 'entities', ()) or ()),
                getattr(schema, 'feature_contract_version', None),
                str(getattr(policy, 'tournament_revision', '') or ''))

    def admit(self, agent, states, changed_entities, policy):
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
            due = (previous is None or interval == 0 or forced or previous[2] != signature
                   or now < previous[0] or now - previous[0] >= interval)
            if not due:
                self.skipped += 1
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
                return
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
                        scope='Engine-dispatched candidate observations; target events always admitted')
