#!/usr/bin/env python3
"""Bounded integration benchmark for HA websocket acknowledgement ingest.

The benchmark deliberately holds Store.lock in another thread, then delivers a real
own-command acknowledgement through the composed Engine.on_state_changed path.  The
event receive path must finish without waiting for SQLite; an explicit provenance read
after releasing the writer lock must still observe the durable acknowledgement.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
TESTS = ROOT / "tests"
for path in (str(SRC), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from provenance_runtime import install as install_provenance
from support import state
import test_executor as executor_fixture


def stamped(st):
    stamp = datetime.now(timezone.utc).isoformat()
    st["last_changed"] = stamp
    st["last_updated"] = stamp
    return st


def run(max_ingest_ms=400.0):
    fixture = executor_fixture.ExecutorTests()
    fixture.setUp()
    try:
        core = SimpleNamespace(STORE=fixture.store, ENGINE=fixture.e)
        install_provenance(core)

        intent = fixture.intent()
        result = fixture.e.executor.submit(intent, {0: 1.0}, 1)
        if result.get("status") != "ACCEPTED":
            raise AssertionError(result)

        ack = stamped(state("light.kitchen", "on"))
        ack["context"] = {
            "id": "benchmark-own-command-ack",
            "parent_id": None,
            "user_id": None,
        }

        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_writer():
            with fixture.store.lock:
                lock_held.set()
                release_lock.wait(2.0)

        holder = threading.Thread(target=hold_writer, daemon=True)
        holder.start()
        if not lock_held.wait(1.0):
            raise AssertionError("failed to acquire synthetic writer lock")

        started = time.perf_counter()
        try:
            fixture.e.on_state_changed({
                "entity_id": "light.kitchen",
                "new_state": ack,
            })
            ingest_ms = (time.perf_counter() - started) * 1000.0
        finally:
            release_lock.set()
            holder.join(1.0)

        if ingest_ms > float(max_ingest_ms):
            raise AssertionError(
                f"HA acknowledgement ingest blocked for {ingest_ms:.1f} ms"
            )

        decision = fixture.e.provenance.decision(intent.intent_id)
        if not decision or decision.get("ack_event_id") is None or decision.get("ack_time") is None:
            raise AssertionError("deferred acknowledgement was not durable on explicit read")

        snapshot = fixture.e.provenance_deferred_snapshot()
        return {
            "contract": "ha_ingress_sqlite_decoupling_v1",
            "ingest_ms_with_store_lock_held": round(ingest_ms, 3),
            "max_ingest_ms": float(max_ingest_ms),
            "ack_durable_after_release": True,
            "ack_pending_after_explicit_read": int(
                (snapshot.get("acknowledgements") or {}).get("pending") or 0
            ),
            "pass": True,
        }
    finally:
        fixture.tearDown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-ingest-ms", type=float, default=400.0)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    print(json.dumps(
        run(args.max_ingest_ms),
        ensure_ascii=False,
        indent=None if args.compact else 2,
    ))


if __name__ == "__main__":
    main()
