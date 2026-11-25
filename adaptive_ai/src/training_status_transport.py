"""Optional volatile status mailbox. Only telemetry moves to RAM, never job state."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time

STATUS_ENV = "ADAPTIVE_AI_VOLATILE_TRAINING_STATUS"
ROOT = Path("/dev/shm")
PREFIX = "homemind-status-"
MAX_RAM_PAYLOAD = 1024 * 1024
DURABLE_INTERVAL = 30.


def _verified_tmpfs():
    try:
        # The directory must be a real tmpfs mount, not a disk-backed fallback.
        if ROOT.resolve() != ROOT:
            return False
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, sep, right = line.partition(" - ")
            fields = left.split()
            if sep and len(fields) > 4 and fields[4] == str(ROOT):
                if right.split()[0] == "tmpfs":
                    return True
    except (OSError, ValueError, IndexError):
        pass
    return False


@contextmanager
def volatile_status_path():
    directory = None
    if _verified_tmpfs():
        try:
            directory = tempfile.TemporaryDirectory(prefix=PREFIX, dir=ROOT,
                                                     ignore_cleanup_errors=True)
        except OSError:
            pass
    try:
        yield Path(directory.name) / "status.json" if directory else None
    finally:
        if directory is not None:
            directory.cleanup()


def worker_status_path():
    value = os.environ.get(STATUS_ENV)
    if not value or not _verified_tmpfs():
        return None
    path = Path(value)
    try:
        parent = path.parent
        if (path.name == "status.json" and parent.parent == ROOT
                and parent.name.startswith(PREFIX) and parent.resolve() == parent
                and parent.is_dir() and parent.stat().st_uid == os.getuid()
                and parent.stat().st_mode & 0o077 == 0):
            return path
    except (OSError, AttributeError):
        pass
    return None


def latest_status(durable, volatile=None, *, previous=None):
    """Return (path/mtime token, payload), or no update. Read no unbounded files."""
    candidates = []
    for priority, raw in enumerate((durable, volatile)):
        if raw is None:
            continue
        path = Path(raw)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((stamp, priority, path))
    for stamp, _, path in sorted(candidates, reverse=True):
        token = (str(path), stamp)
        if token == previous:
            return previous, None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return token, payload
        except (OSError, ValueError):
            continue
    return previous, None


class StatusWriter:
    """One writer per worker; a stage/periodic durable checkpoint plus latest RAM view."""
    def __init__(self, durable, volatile, durable_writer, encode, *, clock=time.monotonic):
        self.durable = durable
        self.volatile = Path(volatile) if volatile is not None else None
        self.write_durable = durable_writer
        self.encode = encode
        self.clock = clock
        self.last_durable = None
        self.last_token = None
        self.pending = None

    def _persist(self, payload, token, now):
        self.write_durable(self.durable, payload)
        self.last_durable = now
        self.last_token = token
        self.pending = None

    def _write_ram(self, payload):
        data = self.encode(payload).encode("utf-8")
        if len(data) > MAX_RAM_PAYLOAD:
            raise OSError("volatile status exceeds 1 MiB")
        temp = self.volatile.with_suffix(".tmp")
        try:
            with temp.open("xb") as stream:
                stream.write(data)
            os.replace(temp, self.volatile)
        finally:
            temp.unlink(missing_ok=True)

    def publish(self, payload, token, *, force=False):
        now = self.clock()
        if self.volatile is None:
            self._persist(payload, token, now)
            return
        due = (force or self.last_durable is None or token != self.last_token
               or now-self.last_durable >= DURABLE_INTERVAL)
        if due:
            self._persist(payload, token, now)
        try:
            self._write_ram(payload)
        except OSError:
            # Retire the volatile path after the first failure; no repeated
            # allocation attempts under RAM pressure. Disk admission remains in force.
            old, self.volatile = self.volatile, None
            if not due:
                self._persist(payload, token, now)
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass
            return
        if not due:
            self.pending = (payload, token)

    def flush(self):
        if self.pending is not None:
            payload, token = self.pending
            self._persist(payload, token, self.clock())
