"""Bounded observed pre-ON illumination, tied to a proven HA state-change epoch."""
from collections import OrderedDict
import copy
import math
import threading

MAX_ENTRIES = 256


class PhotometricBaselines:
    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.Lock()
        self.hits = self.misses = 0

    def __deepcopy__(self, memo):
        result = type(self)()
        memo[id(self)] = result
        with self.lock:
            result.entries = copy.deepcopy(self.entries, memo)
            result.hits, result.misses = self.hits, self.misses
        return result

    def get(self, key, changed, query_ts, knowledge_ts):
        with self.lock:
            row = self.entries.get(key)
            if (row is None or changed is None or changed != row['changed']
                    or query_ts < row['on_start'] or knowledge_ts < row['known_at']):
                self.misses += 1
                return None
            self.hits += 1
            self.entries.move_to_end(key)
            return dict(row['observation']), row['on_start']

    def put(self, key, changed, on_start, baseline_ts, knowledge_ts, observation):
        # No marker, startup already ON, future evidence or ambiguous edge means
        # no retained baseline. A fresh OFF/ON changes HA last_changed, so another
        # run cannot borrow this observation even after the raw deque rolls over.
        if (changed is None or baseline_ts is None or not observation.get('valid')
                or not all(math.isfinite(float(v)) for v in (changed, on_start, baseline_ts, knowledge_ts))
                or not baseline_ts < changed <= on_start <= knowledge_ts):
            return
        row = dict(changed=changed, on_start=on_start, known_at=knowledge_ts,
                   observation=dict(observation))
        with self.lock:
            self.entries[key] = row
            self.entries.move_to_end(key)
            while len(self.entries) > MAX_ENTRIES:
                self.entries.popitem(last=False)

    def snapshot(self):
        with self.lock:
            return dict(entries=len(self.entries), max_entries=MAX_ENTRIES,
                        hits=self.hits, misses=self.misses,
                        semantics='observed pre-ON value, same HA last_changed epoch only')
