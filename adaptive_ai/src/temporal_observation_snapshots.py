"""RAM reuse of private detached temporal rows, invalidated per changed entity."""
from collections import OrderedDict
import pickle
import threading


class TemporalObservationSnapshots:
    def __init__(self, max_bytes=64*1024**2, max_entities=2048):
        self.max_bytes, self.max_entities = int(max_bytes), int(max_entities)
        self.lock = threading.RLock()
        self.entries = OrderedDict()
        self.bytes = self.hits = self.misses = 0

    def capture(self, temporal, timestamp):
        result, charge = {}, 0
        # HA appends/replaces records. Materialize membership/rows before copying;
        # never retain a live deque in a pending observation.
        sources = dict(getattr(temporal, 'samples', {}) or {})
        with self.lock:
            for eid, source in sources.items():
                if not source:
                    continue
                first, last = source[0], source[-1]
                attrs = last[1].get('attributes') or {}
                # Journal metadata is amended after ingestion; include its late receipt
                # fields rather than assuming that a state revision alone proves reuse.
                signature = (id(source), len(source), id(first), id(last),
                             attrs.get('__hm_event_time'), attrs.get('__hm_received_time'),
                             attrs.get('__hm_feature_source'), attrs.get('__hm_quality'))
                old = self.entries.get(eid)
                if old is not None and old[0] == signature:
                    frozen, size = old[1], old[2]
                    self.hits += 1
                    self.entries.move_to_end(eid)
                else:
                    # The live rows are private builtin observations. Native
                    # encode/decode detaches only the changed entity, avoiding a
                    # Python deepcopy walk across thousands of numeric leaves.
                    rows = tuple(source)
                    encoded = pickle.dumps(rows, protocol=5)
                    frozen = pickle.loads(encoded)
                    size = len(encoded)*8
                    self.misses += 1
                    if old is not None:
                        self.bytes -= old[2]
                        del self.entries[eid]
                    if size <= self.max_bytes:
                        # Keep strong identity witnesses: a collected row/deque id
                        # must never be reused as proof of unchanged history.
                        self.entries[eid] = (signature, frozen, size, source, first, last)
                        self.bytes += size
                        while self.bytes > self.max_bytes or len(self.entries) > self.max_entities:
                            _, evicted = self.entries.popitem(last=False)
                            self.bytes -= evicted[2]
                # Cached rows remain private and are consumed only as read-only inputs
                # by Tournament's feature builders. Pending jobs retain their own view.
                result[eid] = frozen if float(frozen[-1][0]) <= timestamp else tuple(
                    row for row in frozen if float(row[0]) <= timestamp)
                charge += size
        return result, charge

    def snapshot(self):
        with self.lock:
            return dict(hits=self.hits, misses=self.misses, bytes=self.bytes,
                        max_bytes=self.max_bytes, entities=len(self.entries), max_entities=self.max_entities)
