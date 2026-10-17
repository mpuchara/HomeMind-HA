"""Private read-only JSON policy snapshots with bounded encodings in RAM.

Admission verifies a detached builtin tree. Ordinary nested mutation is refused;
each use still hashes its exact canonical bytes. Copies and internal pickle snapshots
thaw to plain JSON containers, so persistence formats and mutable learners stay intact.
No external pickle is loaded. A missing checksum/custom value/quota uses the old path.
"""
import copy
import hashlib
import json
import sys
import threading
import weakref

from policy_backend import _canonical
from model_encoding_cache import MODEL_ENCODING_CACHE


class _ReadOnly:
    __slots__ = ()

    def _refuse(self, *args, **kwargs):
        raise TypeError("Sealed policy snapshot is read-only; replace it with a new serialized model")

    __setattr__ = __delattr__ = _refuse


class _SealedDict(_ReadOnly, dict):
    __slots__ = ()
    __init__ = __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _ReadOnly._refuse

    def __deepcopy__(self, memo):
        return _thaw(self, memo)

    def __reduce__(self):
        return dict, (_thaw(self, {}),)


class _SealedList(_ReadOnly, list):
    __slots__ = ()
    __init__ = __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = reverse = sort = __iadd__ = __imul__ = _ReadOnly._refuse

    def __deepcopy__(self, memo):
        return _thaw(self, memo)

    def __reduce__(self):
        return list, (_thaw(self, {}),)


class SealedPolicy(_SealedDict):
    __slots__ = ('_canonical_bytes', '_persistence_json', '__weakref__')

    def verify(self):
        # SHA is fresh on every call; an immutable source avoids repeated encoding.
        result = hashlib.sha256(self._canonical_bytes).hexdigest() == str(self.get('model_checksum'))
        with _BUDGET.lock:
            _BUDGET.checks += 1
        return result


def _thaw(value, memo):
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if isinstance(value, dict):
        result = {}
        memo[identity] = result
        result.update((key, _thaw(item, memo)) for key, item in value.items())
        return result
    if isinstance(value, list):
        result = []
        memo[identity] = result
        result.extend(_thaw(item, memo) for item in value)
        return result
    return value


def _builtins_only(raw):
    pending, seen = [raw], set()
    while pending:
        value = pending.pop()
        kind = type(value)
        if kind in (str, int, float, bool, type(None)):
            continue
        if kind not in (dict, list):
            return False
        if id(value) in seen:
            continue
        seen.add(id(value))
        if kind is dict:
            if any(type(key) not in (str, int, float, bool, type(None)) for key in value):
                return False
            pending.extend(value.values())
        else:
            pending.extend(value)
    return True


def _freeze(value, memo, *, root=False):
    if type(value) not in (dict, list):
        return value
    if id(value) in memo:
        return memo[id(value)]
    if type(value) is dict:
        result = dict.__new__(SealedPolicy if root else _SealedDict)
        memo[id(value)] = result
        for key, item in value.items():
            dict.__setitem__(result, key, _freeze(item, memo))
    else:
        result = list.__new__(_SealedList)
        memo[id(value)] = result
        for item in value:
            list.append(result, _freeze(item, memo))
    return result


class SnapshotBudget:
    def __init__(self, max_bytes=64 * 1024 * 1024, max_entries=64, max_policy_bytes=2 * 1024 * 1024):
        self.max_bytes, self.max_entries, self.max_policy_bytes = max_bytes, max_entries, max_policy_bytes
        self.lock = threading.RLock()
        self.refs = {}
        self.bytes = self.seals = self.fallbacks = self.checks = 0
        self.canonical_bytes = self.persistence_json_bytes = self.persistence_fallbacks = 0

    def fallback(self):
        with self.lock:
            self.fallbacks += 1

    def admit(self, value, size, *, json_size=0):
        with self.lock:
            if size > self.max_policy_bytes or self.bytes + size > self.max_bytes or len(self.refs) >= self.max_entries:
                self.fallbacks += 1
                return False
            # Preserve canonical admission even when the optional JSON fragment
            # cannot fit. Large sources keep their fast, fresh checksum path.
            if json_size > self.max_policy_bytes or self.bytes + size + json_size > self.max_bytes:
                object.__setattr__(value, '_persistence_json', None)
                json_size = 0
                self.persistence_fallbacks += 1
            charge = size + json_size
            token = id(value)
            def release(_ref):
                with self.lock:
                    if self.refs.pop(token, None) is not None:
                        self.bytes -= charge
                        self.canonical_bytes -= size
                        self.persistence_json_bytes -= json_size
            self.refs[token] = weakref.ref(value, release)
            self.bytes += charge
            self.canonical_bytes += size
            self.persistence_json_bytes += json_size
            self.seals += 1
            return True

    def snapshot(self):
        with self.lock:
            return dict(bytes=self.bytes, entries=len(self.refs), seals=self.seals,
                        fallbacks=self.fallbacks, checks=self.checks, max_bytes=self.max_bytes,
                        max_entries=self.max_entries, max_policy_bytes=self.max_policy_bytes,
                        canonical_bytes=self.canonical_bytes, persistence_json_bytes=self.persistence_json_bytes,
                        persistence_fallbacks=self.persistence_fallbacks)


_BUDGET = SnapshotBudget()


def seal_policy(raw):
    if type(raw) is not dict or not raw.get('model_checksum') or not _builtins_only(raw):
        _BUDGET.fallback()
        return None
    detached = copy.deepcopy(raw)
    clean = {key: value for key, value in detached.items()
             if key != 'model_checksum' and not str(key).startswith('_')}
    canonical = MODEL_ENCODING_CACHE.encode(clean, _canonical)
    if hashlib.sha256(canonical).hexdigest() != str(detached['model_checksum']):
        raise ValueError('Candidate source changed or failed checksum during RAM admission')
    if len(canonical) > _BUDGET.max_policy_bytes:
        _BUDGET.fallback()
        return None
    # Body bytes already have the exact builtin JSON encoding. Preserve checksum
    # and ignored bookkeeping too, once, for later non-controlling persistence.
    body_json = canonical.decode('utf-8')
    extra = {key: value for key, value in detached.items()
             if key == 'model_checksum' or str(key).startswith('_')}
    source_json = body_json[:-1] + (',' if clean else '') + json.dumps(
        extra, separators=(',', ':'), sort_keys=True)[1:]
    sealed = _freeze(detached, {}, root=True)
    object.__setattr__(sealed, '_canonical_bytes', canonical)
    object.__setattr__(sealed, '_persistence_json', source_json)
    return sealed if _BUDGET.admit(sealed, len(canonical), json_size=sys.getsizeof(source_json)) else None


def snapshot():
    return _BUDGET.snapshot()
