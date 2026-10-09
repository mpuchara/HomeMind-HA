"""Shadow JSON persistence: reuse content-matched encodings, retain every field.

The model body shares the bounded encoding cache with checksum generation. This
is serialization only: it cannot qualify a candidate or authorize an action.
Checksum and top-level bookkeeping are restored from the current snapshot.
Their insertion order may differ; JSON data and the canonical model SHA do not.
"""
import json
import threading

from model_encoding_cache import MODEL_ENCODING_CACHE


_lock = threading.RLock()
_stats = dict(calls=0, hits=0, encodes=0, fallbacks=0)


def _json(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True)


def snapshot():
    with _lock:
        return dict(_stats)


def shadow_model_json(model):
    """Serialize the detached internal snapshot in the same JSON data format."""
    with _lock:
        _stats['calls'] += 1
    if (type(model) is not dict or not all(type(key) is str for key in model)
            or type(model.get('candidate_policy')) is not dict):
        with _lock:
            _stats['fallbacks'] += 1
        return _json(model)

    policy = model['candidate_policy']
    body = {key: value for key, value in policy.items()
            if key != 'model_checksum' and not str(key).startswith('_')}
    extra = {key: value for key, value in policy.items()
             if key == 'model_checksum' or str(key).startswith('_')}
    encoded = False

    def strict_encoder(value):
        nonlocal encoded
        encoded = True
        # For cacheable builtins this is byte-identical to policy_backend._canonical.
        # Custom objects bypass the cache and must retain legacy JSON's TypeError,
        # rather than checksum's default=str. NaN/Inf use the legacy fallback.
        return json.dumps(value, separators=(',', ':'), sort_keys=True, allow_nan=False)

    try:
        policy_json = MODEL_ENCODING_CACHE.encode(body, strict_encoder).decode('utf-8')
    except (TypeError, ValueError):
        with _lock:
            _stats['fallbacks'] += 1
        return _json(model)
    with _lock:
        _stats['encodes' if encoded else 'hits'] += 1

    if extra:
        # Never reuse the expected checksum or ignored bookkeeping from the cache.
        # JSON key order has no semantic role; verification still sorts the body.
        suffix = _json(extra)[1:]
        policy_json = policy_json[:-1] + (',' if body else '') + suffix
    return '{' + ','.join(
        _json(key) + ':' + (policy_json if key == 'candidate_policy' else _json(value))
        for key, value in sorted(model.items())
    ) + '}'
