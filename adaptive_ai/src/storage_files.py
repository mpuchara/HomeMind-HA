"""Bounded worker transport files and logs; stdout is drained after its disk cap."""
import io
import os
import re
import shutil
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from settings import OPTIONS, DB_PATH
from storage_budget import MIB, MAX_WRITE_BYTES, StorageBudget, StoragePressure

DIRECTORY_LIMIT_BYTES = 128 * MIB
LOG_LIMIT_BYTES = MIB
_fallback_budget = None


def worker_storage_budget():
    # Reuse the parent's/worker's frozen adaptive reserve without importing Store
    # during bootstrap (which would introduce a cycle and initialize the schema).
    store = getattr(sys.modules.get('storage'), 'STORE', None)
    if store is not None and getattr(store, 'storage_budget', None) is not None:
        return store.storage_budget
    global _fallback_budget
    if _fallback_budget is None:
        _fallback_budget = StorageBudget(DB_PATH, OPTIONS)
    return _fallback_budget


def worker_storage_environment():
    return worker_storage_budget().worker_environment()


@contextmanager
def _directory_lock(root):
    # Cross-process admission prevents parallel training workers oversubscribing
    # the same file budget. Only a fixed lock file inside the owned job directory.
    with (root / '.storage.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if lock.read(1) == b'':
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _check(root, incoming):
    budget = worker_storage_budget()
    if budget.sample().get('sample_error') is not None:
        raise budget.pressure()
    reserve = budget.reserve
    if shutil.disk_usage(root).free < reserve + incoming:
        raise StoragePressure('Storage protection: worker file would consume disk reserve')
    total = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and not p.is_symlink())
    if total + incoming > DIRECTORY_LIMIT_BYTES:
        prune_completed(root, needed=incoming)
        total = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and not p.is_symlink())
        if total + incoming > DIRECTORY_LIMIT_BYTES:
            raise StoragePressure('Storage protection: worker directory exceeds 128 MiB budget')


def prune_completed(root, *, needed=0, now=None):
    import time
    root = Path(root).resolve()
    now = time.time() if now is None else now
    files = [p for p in root.iterdir() if p.is_file() and not p.is_symlink()
             and re.fullmatch(r'[a-f0-9]{8,32}\.(?:job|status|result|rollback)\.json(?:\.tmp)?|[a-f0-9]{8,32}\.log', p.name)]
    groups = {}
    for path in files:
        groups.setdefault(path.name.split('.')[0], []).append(path)
    for key, group in sorted(groups.items(), key=lambda item: max(p.stat().st_mtime for p in item[1])):
        age = now - max(p.stat().st_mtime for p in group)
        completed = (root / (key + '.result.json')).exists()
        expired = age > 10 * 86400
        if not expired and not (completed and age > 300 and needed):
            continue
        for path in group:
            if path.resolve().parent == root:
                path.unlink()
        if needed:
            total = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and not p.is_symlink())
            if total + needed <= DIRECTORY_LIMIT_BYTES:
                break


def atomic_text(path, text):
    path = Path(path)
    payload = text.encode('utf-8')
    if len(payload) > MAX_WRITE_BYTES:
        raise StoragePressure('Storage protection: worker JSON exceeds 16 MiB budget')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with _directory_lock(path.parent):
        _check(path.parent, len(payload))
        try:
            temp.write_bytes(payload)
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()


def drain_log(stream, path):
    """Discard excess diagnostics while continuing to drain the child pipe."""
    path = Path(path)
    written = path.stat().st_size if path.exists() else 0
    try:
        while True:
            chunk = getattr(stream, 'read1', stream.read)(32768)
            if not chunk:
                break
            if written >= LOG_LIMIT_BYTES:
                continue
            chunk = chunk[:LOG_LIMIT_BYTES - written]
            try:
                with _directory_lock(path.parent):
                    _check(path.parent, len(chunk))
                    with path.open('ab') as output:
                        output.write(chunk)
                written += len(chunk)
            except (OSError, StoragePressure):
                # A log cannot halt training or repeatedly retry disk writes when
                # the limit is reached. Child status/result files remain separate.
                written = LOG_LIMIT_BYTES
    finally:
        stream.close()


def start_log_drain(process, path):
    stream = getattr(process, 'stdout', None)
    if not isinstance(stream, io.IOBase):
        return None  # Popen test doubles do not own a real pipe.
    thread = threading.Thread(target=drain_log, args=(stream, path),
                              daemon=True, name='adaptive-ai-bounded-worker-log')
    thread.start()
    return thread
