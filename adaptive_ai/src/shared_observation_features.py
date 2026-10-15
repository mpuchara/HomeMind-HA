"""Bounded entity descriptors owned by one synchronous Shadow observation.

The caller owns immutable causal inputs for the duration of this scope. Each
policy still assembles its own vector, labels and metadata and validates its
own model. No computed descriptor survives an observation or crosses threads.
"""
from contextlib import contextmanager
import threading

_tls = threading.local()
MAX_ENTRIES = 256


@contextmanager
def shared_entity_observations(agent, states, temporal, timestamp):
    previous = getattr(_tls, 'frame', None)
    _tls.frame = (agent, states, temporal, float(timestamp), {})
    try:
        yield
    finally:
        _tls.frame = previous


def entity_observation_cache(agent, states, temporal, timestamp):
    frame = getattr(_tls, 'frame', None)
    if (frame is not None and frame[0] is agent and frame[1] is states
            and frame[2] is temporal and frame[3] == float(timestamp)):
        return frame[4]
    return None
