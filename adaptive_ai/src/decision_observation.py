"""Owned feature handoff during one synchronous decision composition."""
from contextlib import contextmanager
import threading

_tls = threading.local()


@contextmanager
def decision_observation(policy, states, temporal, timestamp, observation):
    previous = getattr(_tls, 'frame', None)
    _tls.frame = (policy, states, temporal, float(timestamp), policy.schema,
                  getattr(policy, 'model_revision', None), observation)
    try:
        yield
    finally:
        _tls.frame = previous


def signature_features(policy, states, temporal, timestamp):
    frame = getattr(_tls, 'frame', None)
    if (frame is not None and frame[0] is policy and frame[1] is states
            and frame[2] is temporal and frame[3] == float(timestamp)
            and frame[4] is policy.schema
            and frame[5] == getattr(policy, 'model_revision', None)):
        # Signature builders only read this tuple and build their own result dict.
        return frame[6]
    return policy.features(states, temporal, at_ts=timestamp)
