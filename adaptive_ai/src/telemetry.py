"""Bounded stdlib telemetry and a shared single-heavy-job admission gate."""
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


class Telemetry:
    def __init__(self):
        self.lock = threading.RLock()
        self.samples = {}
        self.counts = Counter()
        self.sums = Counter()
        self.reasons = Counter()
        self.started = time.monotonic()
        self.cpu_start = time.process_time()
        self.last_wall = self.started
        self.last_cpu = self.cpu_start

    def observe(self, name, milliseconds):
        with self.lock:
            self.samples.setdefault(name, deque(maxlen=512)).append(milliseconds)
            self.counts[name] += 1
            self.sums[name] += milliseconds

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
            for name, values in self.samples.items():
                ordered = sorted(values)
                metrics[name] = {'count': self.counts[name], 'avg_ms': self.sums[name]/self.counts[name],
                                 'p95_ms': ordered[min(len(ordered)-1, int(len(ordered)*.95))]}
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

    def acquire(self, owner):
        with self.lock:
            if self.owner is not None:
                return False
            self.owner = owner
            return True

    def release(self, owner):
        with self.lock:
            if self.owner == owner:
                self.owner = None


TELEMETRY = Telemetry()
HEAVY_JOBS = HeavyJobGate()
