"""Periodic PASSIVE checkpoints, outside Store.lock and transaction commit threads."""
import sqlite3
import threading
import time
from pathlib import Path

from telemetry import RUNTIME_DEBUG, TELEMETRY


class WALCheckpointWorker:
    POLL_SECONDS = 5.0
    MAX_IDLE_SECONDS = 30.0
    THRESHOLD_PAGES = 1000

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.active = False
        self.stats = dict(runs=0, errors=0, busy_results=0, last_busy=0, last_error=None,
                          wal_bytes=0, log_frames=0, checkpointed_frames=0,
                          remaining_frames=0, last_duration_ms=0.0,
                          max_duration_ms=0.0, last_checkpoint_at=None)

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False
            self.stop_event.clear()
            self.ready.clear()
            self.thread = threading.Thread(target=self._run,
                name="adaptive-ai-sqlite-checkpoint", daemon=True)
            try:
                self.thread.start()
            except Exception as exc:
                self.thread = None
                self._error(exc)
                self.ready.set()
                return False
        # A slow setup keeps the SQLite default policy until the worker is ready.
        self.ready.wait(1.0)
        return True

    def stop(self):
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            # Do not abandon an in-flight disk operation or release the keeper under it.
            thread.join()

    def snapshot(self):
        with self.lock:
            return {**self.stats, "active": bool(self.active),
                    "alive": bool(self.thread and self.thread.is_alive()),
                    "mode": "PASSIVE", "poll_seconds": self.POLL_SECONDS,
                    "max_idle_seconds": self.MAX_IDLE_SECONDS,
                    "threshold_pages": self.THRESHOLD_PAGES}

    def _error(self, exc):
        with self.lock:
            # New foreground connections fall back to ordinary auto-checkpoints.
            self.active = False
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{type(exc).__name__}: {exc}"

    def checkpoint(self, connection, *, reason="periodic"):
        trace = RUNTIME_DEBUG.begin("sqlite_wal_checkpoint", mode="PASSIVE", reason=reason)
        started = time.perf_counter()
        status, fields = "ok", {}
        try:
            # No Store lock. PASSIVE never waits for readers/writers to finish and
            # leaves incomplete work for the next turn. Disk I/O itself can be slow.
            busy, frames, copied = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            frames, copied = max(0, int(frames)), max(0, int(copied))
            fields = dict(busy=int(busy), log_frames=frames,
                          checkpointed_frames=copied, remaining_frames=max(0, frames - copied))
            with self.lock:
                self.active = True
                self.stats.update({key: value for key, value in fields.items() if key != "busy"})
                self.stats["last_busy"] = int(busy)
                self.stats["runs"] += 1
                self.stats["busy_results"] += int(bool(busy))
                self.stats["last_checkpoint_at"] = time.time()
            return fields
        except Exception as exc:
            status = "error"
            fields = dict(error_type=type(exc).__name__,
                          sqlite_errorcode=getattr(exc, "sqlite_errorcode", None),
                          sqlite_errorname=getattr(exc, "sqlite_errorname", None))
            self._error(exc)
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            with self.lock:
                self.stats["last_duration_ms"] = elapsed
                self.stats["max_duration_ms"] = max(self.stats["max_duration_ms"], elapsed)
            TELEMETRY.observe("sqlite_wal_checkpoint", elapsed)
            RUNTIME_DEBUG.end(trace, status=status, **fields)

    def _connect(self):
        return sqlite3.connect(self.store.path, timeout=0, isolation_level=None)

    def _run(self):
        connection = None
        try:
            connection = self._connect()
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                raise RuntimeError("Checkpoint worker requires WAL")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            threshold = 32 + self.THRESHOLD_PAGES * (page_size + 24)
            with self.lock:
                self.active = True
            self.ready.set()
            last_attempt = time.monotonic()
            wal_path = Path(self.store.path + "-wal")
            while not self.stop_event.wait(self.POLL_SECONDS):
                try:
                    try:
                        size = wal_path.stat().st_size
                    except FileNotFoundError:
                        size = 0
                    with self.lock:
                        self.stats["wal_bytes"] = size
                    age = time.monotonic() - last_attempt
                except Exception as exc:
                    self._error(exc)
                    continue
                if size > 32 and (size >= threshold or age >= self.MAX_IDLE_SECONDS):
                    try:
                        self.checkpoint(connection)
                    except Exception:
                        pass  # checkpoint() records failure and enables the fallback.
                    finally:
                        # Bound retries even on failure; never spin on a busy disk.
                        last_attempt = time.monotonic()
            try:
                self.checkpoint(connection, reason="shutdown")
            except Exception:
                pass  # Keeper's final close still performs SQLite's native cleanup.
        except Exception as exc:
            self._error(exc)
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                with self.lock:
                    self.active = False
                self.ready.set()
