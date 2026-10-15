"""Bounded entity descriptors owned by one synchronous Shadow observation.

The caller owns immutable causal inputs for the duration of this scope. Each
policy still assembles its own vector, labels and metadata and validates its
own model. No computed descriptor survives an observation or crosses threads.
"""
from contextlib import contextmanager
import threading

_tls = threading.local()
MAX_ENTRIES = 256


def _agent_semantics(agent):
    from context import is_fast_reactive_agent
    return (str((agent or {}).get('target_entity') or ''),
            str((agent or {}).get('target_property') or ''),
            bool(agent and is_fast_reactive_agent(agent)))


@contextmanager
def shared_entity_observations(agent, states, temporal, timestamp):
    previous = getattr(_tls, 'frame', None)
    _tls.frame = (_agent_semantics(agent), states, temporal, float(timestamp), {})
    try:
        yield _tls.frame[4]
    finally:
        _tls.frame = previous


def entity_observation_cache(agent, states, temporal, timestamp):
    frame = getattr(_tls, 'frame', None)
    if (frame is not None and frame[0] == _agent_semantics(agent) and frame[1] is states
            and frame[2] is temporal and frame[3] == float(timestamp)):
        return frame[4]
    return None
