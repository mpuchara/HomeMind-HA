"""Bounded read-only entity records shared by private queued observations."""
from collections import OrderedDict
import pickle
import sys
import threading


class DetachedMappingSnapshots:
    def __init__(self, max_bytes=16*1024**2, max_entries=2048, *, reuse_complete=False):
        self.max_bytes, self.max_entries = max(0, int(max_bytes)), int(max_entries)
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.bytes = self.hits = self.misses = 0
        self.reuse_complete = bool(reuse_complete)
        self.complete = None
        self.complete_parts = ()
        self.complete_keys = self.complete_ids = None
        self.complete_bytes = self.complete_charge = self.complete_hits = 0

    def capture(self, mapping, *, shared=False):
        result, charge, parts = {}, 0, []
        # Producers replace state/registry records. Equality with the detached copy
        # additionally detects in-place journal/attribute corrections, at any depth.
        # Strong source references rule out object-id reuse; no revision assumption.
        with self.lock:
            # Registry membership usually stays unchanged for thousands of frames.
            # Native dictionary equality validates every detached nested value;
            # identity/revision alone would miss in-place attribute corrections.
            # Return a new container so callers cannot alter the retained membership.
            if (self.complete is not None and type(mapping) is dict
                    and self.bytes <= self.max_bytes and len(self.entries) <= self.max_entries
                    and len(mapping) == len(self.complete)
                    and tuple(mapping) == self.complete_keys
                    and tuple(map(id, mapping.values())) == self.complete_ids
                    and self.complete == mapping):
                self.hits += len(mapping)
                self.complete_hits += 1
                return ((dict(self.complete), self.complete_charge, self.complete_parts) if shared
                        else (dict(self.complete), self.complete_charge))
            self.complete = None
            self.complete_parts = ()
            self.complete_keys = self.complete_ids = None
            self.bytes -= self.complete_bytes
            self.complete_bytes = self.complete_charge = 0
            while self.entries and (self.bytes > self.max_bytes or len(self.entries) > self.max_entries):
                _, removed = self.entries.popitem(last=False)
                self.bytes -= removed[2]
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
                parts.append((frozen, size))
                charge += size
            if self.reuse_complete and type(mapping) is dict:
                container_bytes = sys.getsizeof(result)
                # Every value must already belong to the bounded record cache.
                # Retaining the complete map must not extend evicted records' lives.
                if (self.bytes + container_bytes <= self.max_bytes
                        and len(result) <= self.max_entries
                        and all(key in self.entries and self.entries[key][1] is value
                                for key, value in result.items())):
                    self.complete = dict(result)
                    self.complete_parts = tuple(parts)
                    # Cached records retain strong source references. Native id
                    # tuples preserve replacement semantics (e.g. True vs 1),
                    # without another Python loop over every registry record.
                    self.complete_keys = tuple(mapping)
                    self.complete_ids = tuple(map(id, mapping.values()))
                    self.complete_bytes = (sys.getsizeof(self.complete)
                        + sys.getsizeof(self.complete_keys) + sys.getsizeof(self.complete_ids)
                        + sum(map(sys.getsizeof, self.complete_ids))
                        + sys.getsizeof(self.complete_parts) + sum(map(sys.getsizeof, self.complete_parts)))
                    if self.bytes + self.complete_bytes <= self.max_bytes:
                        self.bytes += self.complete_bytes
                        self.complete_charge = charge
                    else:
                        self.complete = None
                        self.complete_parts = ()
                        self.complete_keys = self.complete_ids = None
                        self.complete_bytes = 0
        return (result, charge, tuple(parts)) if shared else (result, charge)

    def snapshot(self):
        with self.lock:
            return dict(bytes=self.bytes, max_bytes=self.max_bytes, entries=len(self.entries),
                        max_entries=self.max_entries, hits=self.hits, misses=self.misses,
                        complete_hits=self.complete_hits, complete_bytes=self.complete_bytes)
