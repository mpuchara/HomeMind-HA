"""Bounded stdlib telemetry, opt-in runtime tracing and a shared heavy-job gate."""
from datetime import datetime, timezone
import threading
import time
from collections import Counter, deque
from pathlib import Path


def rss_mb():
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])/1024
    except OSError:
        return None


def _p95(values):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[min(len(ordered)-1, int(len(ordered)*.95))]


class RuntimeDebugTrace:
    """Small in-RAM execution trace enabled explicitly from Diagnostics.

    The normal runtime pays only one boolean check at instrumentation points.  No file I/O,
    SQLite writes or extra polling is performed while tracing.  The bounded ring is intended
    for short troubleshooting sessions on Raspberry Pi and is exported on demand.
    """

    MAX_ENTRIES = 4096

    def __init__(self):
        self.lock = threading.RLock()
        self.enabled = False
        self.entries = deque(maxlen=self.MAX_ENTRIES)
        self.active = {}
        self.sequence = 0
        self.total_entries = 0
        self.session_started_at = None

    @staticmethod
    def _wall_ts():
        return datetime.now(timezone.utc).isoformat()

    def _append_locked(self, kind, operation, fields=None, **extra):
        self.sequence += 1
        self.total_entries += 1
        row = {
            "seq": self.sequence,
            "ts": self._wall_ts(),
            "kind": str(kind),
            "operation": str(operation),
            "thread": threading.current_thread().name,
            **extra,
        }
        if fields:
            row["fields"] = dict(fields)
        self.entries.append(row)
        return row

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.active.clear()
            self.total_entries = 0
            self.sequence = 0
            self.session_started_at = self._wall_ts() if self.enabled else None

    def set_enabled(self, enabled, *, clear=False):
        enabled = bool(enabled)
        with self.lock:
            if clear:
                self.entries.clear()
                self.active.clear()
                self.total_entries = 0
                self.sequence = 0
            changed = enabled != self.enabled
            self.enabled = enabled
            if enabled and (changed or self.session_started_at is None):
                self.session_started_at = self._wall_ts()
                self._append_locked("state", "runtime_debug_enabled")
            elif not enabled and changed:
                self.active.clear()
        return self.enabled

    def begin(self, operation, **fields):
        if not self.enabled:
            return None
        with self.lock:
            if not self.enabled:
                return None
            started_mono = time.monotonic()
            self.sequence += 1
            token = f"{threading.get_ident()}:{self.sequence}:{started_mono:.6f}"
            row = {
                "seq": self.sequence,
                "ts": self._wall_ts(),
                "kind": "begin",
                "operation": str(operation),
                "thread": threading.current_thread().name,
                "fields": dict(fields),
                "token": token,
            }
            self.total_entries += 1
            self.entries.append(row)
            self.active[token] = {
                "token": token,
                "operation": str(operation),
                "thread": threading.current_thread().name,
                "started_at": row["ts"],
                "started_mono": started_mono,
                "fields": dict(fields),
            }
            return token

    def end(self, token, *, status="ok", **fields):
        if not token:
            return None
        with self.lock:
            active = self.active.pop(str(token), None)
            if active is None:
                return None
            duration_ms = max(0.0, (time.monotonic() - float(active["started_mono"])) * 1000.0)
            if not self.enabled:
                return duration_ms
            self._append_locked(
                "end",
                active["operation"],
                fields={**active.get("fields", {}), **fields},
                token=str(token),
                status=str(status),
                duration_ms=round(duration_ms, 3),
            )
            return duration_ms

    def instant(self, operation, **fields):
        if not self.enabled:
            return None
        with self.lock:
            if not self.enabled:
                return None
            return self._append_locked("event", operation, fields=fields)

    def summary(self, telemetry_snapshot=None):
        with self.lock:
            now = time.monotonic()
            active = [
                {
                    "operation": item["operation"],
                    "thread": item["thread"],
                    "started_at": item["started_at"],
                    "age_ms": round(max(0.0, now - float(item["started_mono"])) * 1000.0, 1),
                    "fields": dict(item.get("fields") or {}),
                }
                for item in self.active.values()
            ]
            active.sort(key=lambda item: -float(item["age_ms"]))
            entries_count = len(self.entries)
            dropped = max(0, self.total_entries - entries_count)
            enabled = bool(self.enabled)
            started = self.session_started_at
        telemetry_snapshot = telemetry_snapshot or {}
        event_metric = ((telemetry_snapshot.get("metrics") or {}).get("event_to_intent") or {})
        return {
            "enabled": enabled,
            "session_started_at": started,
            "entries": entries_count,
            "dropped_entries": dropped,
            "capacity": self.MAX_ENTRIES,
            "active": active[:12],
            "active_count": len(active),
            "event_to_intent": {
                "count": int(event_metric.get("count") or 0),
                "p95_ms": event_metric.get("p95_ms"),
                "recent_count": int(event_metric.get("recent_count") or 0),
                "recent_p95_ms": event_metric.get("recent_p95_ms"),
            },
        }

    def export(self):
        with self.lock:
            now = time.monotonic()
            active = [
                {
                    **{k: v for k, v in item.items() if k != "started_mono"},
                    "age_ms": round(max(0.0, now - float(item["started_mono"])) * 1000.0, 1),
                }
                for item in self.active.values()
            ]
            return {
                "enabled": bool(self.enabled),
                "session_started_at": self.session_started_at,
                "capacity": self.MAX_ENTRIES,
                "total_entries": int(self.total_entries),
                "dropped_entries": max(0, self.total_entries - len(self.entries)),
                "active": active,
                "entries": list(self.entries),
            }


class Telemetry:
    def __init__(self):
        self.lock = threading.RLock()
        self.samples = {}
        self.recent_samples = {}
        self.counts = Counter()
        self.sums = Counter()
        self.reasons = Counter()
        self.started = time.monotonic()
        self.cpu_start = time.process_time()
        self.last_wall = self.started
        self.last_cpu = self.cpu_start

    def observe(self, name, milliseconds):
        debug_latency = None
        with self.lock:
            value = float(milliseconds)
            self.samples.setdefault(name, deque(maxlen=512)).append(value)
            self.recent_samples.setdefault(name, deque(maxlen=512)).append(
                (time.monotonic(), value)
            )
            self.counts[name] += 1
            self.sums[name] += value
            if name == "event_to_intent":
                now_mono = time.monotonic()
                recent = [
                    sample for stamp, sample in self.recent_samples.get(name, ())
                    if now_mono - float(stamp) <= 60.0
                ]
                debug_latency = {
                    "sample_ms": round(value, 3),
                    "recent_p95_ms": _p95(recent),
                    "recent_count": len(recent),
                    "total_count": int(self.counts[name]),
                }
        debug = globals().get("RUNTIME_DEBUG")
        if debug_latency is not None and debug is not None and debug.enabled:
            debug.instant("event_to_intent", **debug_latency)

    def intent(self, status, reason):
        with self.lock:
            self.counts['intent_' + status.lower()] += 1
            code = reason.split(':', 1)[0]
            known = {'disabled','target','qualification','paused','model','state','context','unavailable',
                     'confidence','support','novelty','manual','takeover','settling','limits','duplicate',
                     'acknowledgement','retry','cooldown','expired','settings','service'}
            self.reasons[code if code in known else status.lower()] += 1

    def snapshot(self):
        with self.lock:
            metrics = {}
            now_mono = time.monotonic()
            for name, values in self.samples.items():
                recent = [
                    value for stamp, value in self.recent_samples.get(name, ())
                    if now_mono - float(stamp) <= 60.0
                ]
                metrics[name] = {
                    'count': self.counts[name],
                    'avg_ms': self.sums[name]/self.counts[name],
                    'p95_ms': _p95(values),
                    'recent_count': len(recent),
                    'recent_p95_ms': _p95(recent),
                }
            wall,cpu = time.monotonic(),time.process_time()
            recent = 100*(cpu-self.last_cpu)/max(.001,wall-self.last_wall)
            self.last_cpu,self.last_wall = cpu,wall
            return {'rss_mb': rss_mb(), 'cpu_percent_recent': recent, 'cpu_percent_since_start':
                    100*(time.process_time()-self.cpu_start)/max(.001, time.monotonic()-self.started),
                    'metrics': metrics, 'counts': dict(self.counts), 'intent_reasons': dict(self.reasons)}


class HeavyJobGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None
        self._debug_token = None

    def acquire(self, owner):
        with self.lock:
            if self.owner is not None:
                return False
            self.owner = owner
            debug = globals().get("RUNTIME_DEBUG")
            self._debug_token = (
                debug.begin("heavy_job", owner=str(owner))
                if debug is not None and debug.enabled else None
            )
            return True

    def release(self, owner):
        token = None
        with self.lock:
            if self.owner == owner:
                self.owner = None
                token = self._debug_token
                self._debug_token = None
        debug = globals().get("RUNTIME_DEBUG")
        if token and debug is not None:
            debug.end(token, status="released")


RUNTIME_DEBUG = RuntimeDebugTrace()
TELEMETRY = Telemetry()
HEAVY_JOBS = HeavyJobGate()
