"""U04b opt-in, single-writer neural observer for actual HA changes.

NO inference-path hook, model training, HA service call, ActionIntent, Executor,
SQLite read/write or physical control. Default-disabled; observer results are
diagnostic only. Websocket submission is O(1), bounded and coalescing.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from math import isfinite
import threading
import time

MAX_PENDING = 2048
MAX_SNAPSHOT = 10000
BATCH_SIZE = 16
PAUSE_SECONDS = 0.025


def _sensor(entity_id):
    return str(entity_id or "").partition(".")[0] in ("sensor", "binary_sensor")


def _compact(state):
    if state is None:
        return None
    if not isinstance(state, dict):
        raise ValueError("HA state must be a dict or null")
    attrs = state.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    return {
        "state": state.get("state"),
        "last_updated": state.get("last_updated"),
        "last_changed": state.get("last_changed"),
        "attributes": {
            key: attrs[key] for key in ("device_class", "unit_of_measurement")
            if key in attrs
        },
    }


class UniversalRuntimeObserver(threading.Thread):
    """Bounded mailbox and single-writer RAM-only neural mirror.

    Queue overflow invalidates published representation until a full REST
    reconciliation. Never silently publish incomplete embeddings as healthy.
    """

    daemon = True

    def __init__(self, *, max_pending=MAX_PENDING, batch_size=BATCH_SIZE,
                 pause_seconds=PAUSE_SECONDS, max_snapshot=MAX_SNAPSHOT):
        super().__init__(name="homemind-universal-observer")
        self.max_pending = int(max_pending)
        self.batch_size = int(batch_size)
        self.pause_seconds = float(pause_seconds)
        self.max_snapshot = int(max_snapshot)
        if not (1 <= self.max_pending <= 65536 and 1 <= self.batch_size <= 64
                and 1 <= self.max_snapshot <= 100000 and 0 <= self.pause_seconds <= 1):
            raise ValueError("invalid observer bounds")
        self._cv = threading.Condition()
        self._stopping = False
        self._stop_signal = threading.Event()
        self._pending = OrderedDict()
        self._snapshot = None
        self._registry_pending = {}
        self._overflow = False
        self._healthy = False
        self._rebuild = deque()
        self._published = {}
        self._stats = {
            "submitted": 0, "coalesced": 0, "processed": 0,
            "snapshots": 0, "registry_rebuilds": 0,
            "overflow": 0, "errors": 0, "last_error": None,
            "max_worker_batch_ms": 0.0,
        }
        # Single-writer only. Access outside run() is limited to diagnostics.
        self._raw = {}
        self._registries = {"entity": (), "device": ()}
        self._semantic = None
        self._neural = None

    def submit_event(self, entity_id, state, *, event_ts, received_ts):
        if not _sensor(entity_id):
            return False
        event_ts, received_ts = float(event_ts), float(received_ts)
        if not (isfinite(event_ts) and isfinite(received_ts)):
            return False
        row = (str(entity_id), _compact(state), event_ts, received_ts)
        with self._cv:
            if self._stopping or self._overflow:
                return False
            self._stats["submitted"] += 1
            previous = self._pending.get(row[0])
            if previous is not None:
                self._stats["coalesced"] += 1
                if (event_ts, received_ts) <= (previous[2], previous[3]):
                    return True
                self._pending[row[0]] = row
            elif len(self._pending) >= self.max_pending:
                self._overflow = True
                self._healthy = False
                self._pending.clear()
                self._stats["overflow"] += 1
                self._cv.notify()
                return False
            else:
                self._pending[row[0]] = row
            self._healthy = False
            self._cv.notify()
        return True

    def submit_registry(self, kind, entries):
        if kind not in self._registries:
            return False
        with self._cv:
            if self._stopping:
                return False
            self._registry_pending[kind] = entries
            self._healthy = False
            self._cv.notify()
        return True

    def submit_snapshot(self, state_map, *, received_ts):
        """Complete REST reconciled snapshot, posted from low-priority poll worker."""
        received_ts = float(received_ts)
        if not isfinite(received_ts):
            raise ValueError("invalid snapshot timestamp")
        rows = tuple(
            (eid, _compact(st), received_ts)
            for eid, st in state_map.items() if _sensor(eid) and st is not None
        )
        with self._cv:
            if self._stopping:
                return False
            if len(rows) > self.max_snapshot:
                self._snapshot = None
                self._pending.clear()
                self._overflow = True
                self._healthy = False
                self._stats["overflow"] += 1
                self._cv.notify()
                return False
            self._snapshot = (rows, received_ts)
            # Engine has already merged all states received before this watermark.
            self._pending = OrderedDict(
                (eid, row) for eid, row in self._pending.items()
                if row[3] > received_ts
            )
            self._overflow = False
            self._healthy = False
            self._cv.notify()
        return True

    def stop(self, timeout=2.0):
        with self._cv:
            self._stopping = True
            self._stop_signal.set()
            self._pending.clear()
            self._snapshot = None
            self._cv.notify_all()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=timeout)

    def _reset_neural(self):
        from universal_neural_encoder import IncrementalNeuralHome
        self._neural = IncrementalNeuralHome()
        self._rebuild = deque(self._raw[eid] for eid in sorted(self._raw))
        self._healthy = False

    def _update_registry(self, updates):
        from universal_ha_adapter import SemanticRegistry
        self._registries.update(updates)
        self._semantic = SemanticRegistry.create(
            entities=self._registries["entity"], devices=self._registries["device"],
            revision="observer-live",
        )
        self._reset_neural()
        self._stats["registry_rebuilds"] += 1

    def _apply(self, row, *, rebuilding=False):
        from universal_ha_adapter import parse_ha_timestamp
        eid, state, event_ts, received_ts = row
        if not rebuilding:
            old = self._raw.get(eid)
            if old is not None and (event_ts, received_ts) <= (old[2], old[3]):
                return
            if state is None:
                self._raw.pop(eid, None)
            else:
                self._raw[eid] = row
        if state is None:
            self._neural.remove(eid)
            return
        try:
            stamp = parse_ha_timestamp(state.get("last_updated") or state.get("last_changed"))
            if abs(stamp - event_ts) > 1e-3:
                raise ValueError("event timestamp mismatch")
        except ValueError:
            self._stats["errors"] += 1
            self._stats["last_error"] = "missing_or_mismatched_HA_event_timestamp"
            self._neural.remove(eid)
            return
        now = max(time.time(), self._neural.index.now)
        if event_ts > now or received_ts > now:
            self._stats["errors"] += 1
            self._stats["last_error"] = "future_observation_rejected"
            self._neural.remove(eid)
            return
        token = self._semantic.encode(
            eid, state, at_ts=now, event_ts=event_ts,
            received_ts=received_ts, max_age_seconds=900.0,
        )
        self._neural.upsert(token, now=now)

    def _snapshot_rows(self, rows, received_ts):
        from universal_ha_adapter import parse_ha_timestamp
        self._raw.clear()
        for eid, state, _ in rows:
            try:
                event_ts = parse_ha_timestamp(state.get("last_updated") or state.get("last_changed"))
                if event_ts <= received_ts:
                    self._raw[eid] = (eid, state, event_ts, received_ts)
            except ValueError:
                self._stats["errors"] += 1
        self._reset_neural()
        self._stats["snapshots"] += 1

    def diagnostics(self):
        with self._cv:
            return {
                "enabled": True, "mode": "observer_only",
                "healthy": bool(self._healthy and not self._overflow),
                "reconcile_required": self._overflow,
                "pending_events": len(self._pending),
                "pending_rebuild": len(self._rebuild),
                "pending_snapshot": self._snapshot is not None,
                "worker_alive": self.is_alive(), "queue_limit": self.max_pending,
                "physical_actions": 0, "trained": False,
                "stats": dict(self._stats), "neural": dict(self._published),
            }

    def run(self):
        from universal_ha_adapter import SemanticRegistry
        from universal_neural_encoder import IncrementalNeuralHome
        self._semantic = SemanticRegistry.create(revision="observer-init")
        self._neural = IncrementalNeuralHome()
        while True:
            with self._cv:
                if self._stopping:
                    break
                if self._registry_pending:
                    action, payload = "registry", dict(self._registry_pending)
                    self._registry_pending.clear()
                elif self._snapshot is not None:
                    action, payload = "snapshot", self._snapshot
                    self._snapshot = None
                elif self._rebuild:
                    action, payload = "rebuild", [
                        self._rebuild.popleft() for _ in range(min(len(self._rebuild), self.batch_size))
                    ]
                elif self._pending and not self._overflow:
                    action, payload = "events", [
                        self._pending.popitem(last=False)[1]
                        for _ in range(min(len(self._pending), self.batch_size))
                    ]
                else:
                    self._cv.wait(timeout=0.5)
                    if self._stopping:
                        break
                    action, payload = "tick", None
            started = time.perf_counter()
            try:
                if action == "registry":
                    self._update_registry(payload)
                elif action == "snapshot":
                    self._snapshot_rows(*payload)
                elif action in ("rebuild", "events"):
                    for row in payload:
                        self._apply(row, rebuilding=action == "rebuild")
                        self._stats["processed"] += 1
                elif action == "tick":
                    self._neural.advance(max(time.time(), self._neural.index.now))
                if self._neural is not None:
                    self._neural.home()
                with self._cv:
                    if (not self._overflow and self._stats["snapshots"]
                            and not self._rebuild and self._snapshot is None
                            and not self._registry_pending and not self._pending):
                        self._healthy = True
            except Exception as exc:
                with self._cv:
                    self._stats["errors"] += 1
                    self._stats["last_error"] = f"{type(exc).__name__}: {exc}"[:250]
                    self._overflow = True
                    self._healthy = False
                    self._pending.clear()
            finally:
                elapsed = (time.perf_counter() - started) * 1000.0
                neural = self._neural.diagnostics() if self._neural is not None else {}
                with self._cv:
                    self._stats["max_worker_batch_ms"] = max(
                        self._stats["max_worker_batch_ms"], elapsed
                    )
                    self._published = neural
            if action in ("rebuild", "events"):
                # Cooperative limit: <=20% of a single CPU core under
                # sustained traffic, plus a minimum scheduling pause.
                self._stop_signal.wait(max(self.pause_seconds, elapsed / 1000.0 * 4.0))


def install(engine):
    """Lazy optional installation: zero NumPy imports when disabled."""
    from settings import OPTIONS
    if not bool(OPTIONS.get("universal_observer_enabled", False)):
        return None
    existing = getattr(engine, "universal_observer", None)
    if existing is not None:
        return existing
    observer = UniversalRuntimeObserver()
    engine.universal_observer = observer
    observer.start()
    return observer
