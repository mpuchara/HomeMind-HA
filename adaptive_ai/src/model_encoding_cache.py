"""Bounded canonical JSON cache with complete, fresh builtin-content witnesses.

Never cache a checksum verification result. Every call still hashes canonical JSON.
Only reuse encoding after matching newly captured bytes of the entire JSON tree.
No external pickle is decoded; unsupported/custom objects always use the encoder.
"""
import hashlib
import io
import pickle
import threading
from collections import OrderedDict


_SCALARS = (str, int, float, bool, type(None))
_CONTAINERS = (dict, list, tuple)
_BUILTIN_TYPE_IDS = frozenset(map(id, _SCALARS + _CONTAINERS))
_SCALAR_TYPE_IDS = frozenset(map(id, _SCALARS))


class _UnsupportedBuiltin(Exception):
    pass


class _BuiltinPickler(pickle.Pickler):
    def reducer_override(self, value):
        # Never run a custom object's reduction, including builtin subclasses.
        # Exact builtins use the native dispatch and do not need this hook.
        raise _UnsupportedBuiltin


def _reject_buffer(value):
    # PickleBuffer has a native dispatch too; it is not a JSON model value.
    raise _UnsupportedBuiltin


def builtin_witness(value):
    """Freeze only builtin trees, without a Python visit to every numeric leaf.

    Native dispatch handles exact scalars and containers; reducer_override blocks
    custom reductions. Native non-JSON types (bytes/sets/bytearray) are memoized
    and rejected before any bytes can be decoded or published in the cache.
    Only this function's private in-memory bytes are ever decoded.
    """
    stream = io.BytesIO()
    pickler = _BuiltinPickler(stream, protocol=5, buffer_callback=_reject_buffer)
    try:
        pickler.dump(value)
    except _UnsupportedBuiltin:
        return None
    objects = [obj for _, obj in pickler.memo.copy().values()]
    if any(id(type(obj)) not in _BUILTIN_TYPE_IDS for obj in objects):
        return None
    has_tuple = any(type(obj) is tuple for obj in objects)
    for obj in objects:
        if type(obj) is dict:
            # Containers cannot be keys except tuples. Nonempty tuples appear in
            # the memo; empty tuples do not. Avoid rescanning every string key in
            # normal JSON-loaded models, while retaining scalar-only dict keys.
            if has_tuple:
                if any(id(type(key)) not in _SCALAR_TYPE_IDS for key in obj):
                    return None
            elif () in obj:
                return None
    return stream.getvalue()


class ModelEncodingCache:
    def __init__(self, max_bytes=16 * 1024 * 1024, max_entries=16, min_bytes=16384):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.min_bytes = min_bytes
        self.lock = threading.RLock()
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = self.bypasses = 0

    def encode(self, value, encoder):
        witness = builtin_witness(value)
        if witness is None:
            with self.lock:
                self.bypasses += 1
            return encoder(value).encode('utf-8')
        # Fresh full content, including nested numeric matrices and signed zero.
        # Identity, revision or a previous expected checksum cannot establish a hit.
        if len(witness) < self.min_bytes:
            with self.lock:
                self.bypasses += 1
            return encoder(pickle.loads(witness)).encode('utf-8')
        key = hashlib.sha256(witness).digest()
        with self.lock:
            cached = self.entries.get(key)
            if cached is not None and cached[0] == witness:
                self.hits += 1
                self.entries.move_to_end(key)
                return cached[1]
            self.misses += 1
        # Encoding can be expensive: never hold the RAM cache lock during it.
        # Encode our own detached snapshot, not a caller's concurrently mutable
        # containers. Only bytes produced above from internal builtin model data
        # are decoded; never load a file, DB blob, request or supplied pickle.
        encoded = encoder(pickle.loads(witness)).encode('utf-8')
        size = len(witness) + len(encoded)
        if size <= self.max_bytes and self.max_entries > 0:
            with self.lock:
                old = self.entries.pop(key, None)
                if old is not None:
                    self.bytes -= len(old[0]) + len(old[1])
                self.entries[key] = (witness, encoded)
                self.bytes += size
                while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
                    _, removed = self.entries.popitem(last=False)
                    self.bytes -= len(removed[0]) + len(removed[1])
        return encoded

    def snapshot(self):
        with self.lock:
            return dict(hits=self.hits, misses=self.misses, bypasses=self.bypasses,
                        entries=len(self.entries), bytes=self.bytes,
                        max_bytes=self.max_bytes, max_entries=self.max_entries,
                        min_bytes=self.min_bytes)


MODEL_ENCODING_CACHE = ModelEncodingCache()
