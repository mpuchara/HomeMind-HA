"""Bounded canonical JSON cache with complete, fresh builtin-content witnesses.

Never cache a checksum verification result. Every call still hashes canonical JSON.
Only reuse encoding after matching newly captured bytes of the entire JSON tree.
No external pickle is decoded; unsupported/custom objects always use the encoder.
"""
import hashlib
import pickle
import threading
from collections import OrderedDict


_SCALARS = (str, int, float, bool, type(None))
_CONTAINERS = (dict, list, tuple)


def builtin_tree(value):
    pending, visited = [value], set()
    while pending:
        current = pending.pop()
        kind = type(current)
        if kind in _SCALARS:
            continue
        if kind not in _CONTAINERS:
            return False
        if id(current) in visited:
            continue
        visited.add(id(current))
        if kind is dict:
            if any(type(key) not in _SCALARS for key in current):
                return False
            items = current.values()
        else:
            items = current
        for item in items:
            item_type = type(item)
            if item_type in _SCALARS:
                continue
            if item_type not in _CONTAINERS:
                return False
            pending.append(item)
    return True


class ModelEncodingCache:
    def __init__(self, max_bytes=16 * 1024 * 1024, max_entries=16, min_bytes=16384):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.min_bytes = min_bytes
        self.lock = threading.RLock()
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = self.bypasses = 0

    def encode(self, value, encoder):
        if not builtin_tree(value):
            with self.lock:
                self.bypasses += 1
            return encoder(value).encode('utf-8')
        # Fresh full content, including nested numeric matrices and signed zero.
        # Identity, revision or a previous expected checksum cannot establish a hit.
        witness = pickle.dumps(value, protocol=5)
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
