"""Bounded read-only entity records shared by private queued observations."""
from collections import OrderedDict
import pickle
import threading


class DetachedMappingSnapshots:
    def __init__(self, max_bytes=16*1024**2, max_entries=2048):
        self.max_bytes, self.max_entries = max(0, int(max_bytes)), int(max_entries)
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.bytes = self.hits = self.misses = 0

    def capture(self, mapping):
        result, charge = {}, 0
        # Producers replace state/registry records. Equality with the detached copy
        # additionally detects in-place journal/attribute corrections, at any depth.
        # Strong source references rule out object-id reuse; no revision assumption.
        with self.lock:
            for key, value in mapping.items():
                old = self.entries.get(key)
                if old is not None and old[0] is value and old[1] == value:
                    frozen, size = old[1:]
                    self.hits += 1
                    self.entries.move_to_end(key)
                else:
                    encoded = pickle.dumps(value, protocol=5)
                    frozen, size = pickle.loads(encoded), len(encoded)*8
                    self.misses += 1
                    if old is not None:
                        self.bytes -= old[2]
                        del self.entries[key]
                    if size <= self.max_bytes:
                        self.entries[key] = value, frozen, size
                        self.bytes += size
                        while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
                            _, removed = self.entries.popitem(last=False)
                            self.bytes -= removed[2]
                result[key] = frozen
                charge += size
        return result, charge

    def snapshot(self):
        with self.lock:
            return dict(bytes=self.bytes, max_bytes=self.max_bytes, entries=len(self.entries),
                        max_entries=self.max_entries, hits=self.hits, misses=self.misses)
