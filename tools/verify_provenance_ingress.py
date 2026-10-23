"""Controlled actual HA delivery under corrupt raw-audit backpressure; no HA services."""
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tests'))
import support
import test_executor as fixtures
from provenance_runtime import install


def main():
    f=fixtures.ExecutorTests();f.setUp()
    worker=None
    try:
        install(SimpleNamespace(STORE=f.store,ENGINE=f.e))
        f.e.stop_event.set();f.e.provenance_writer_event.set()
        f.e.provenance_writer_thread.join(timeout=2.)
        journal=f.e.provenance;journal._pending_event_limit=1
        journal.record_event('sensor.old',support.state('sensor.old','1'),event_time=1.,event_id='old')
        failure=sqlite3.DatabaseError('database disk image is malformed')
        failure.sqlite_errorcode=sqlite3.SQLITE_CORRUPT;failure.sqlite_errorname='SQLITE_CORRUPT'
        with patch('provenance.insert_bounded_rows',side_effect=failure):
            try:journal.flush_events_batch()
            except sqlite3.DatabaseError:pass
            done=threading.Event();errors=[];started=time.perf_counter()
            def deliver():
                try:
                    f.e.on_state_changed({'entity_id':'light.kitchen','new_state':support.state('light.kitchen','on')})
                    f.e._queue_archive_state(support.state('sensor.poll','7'),force=True)
                except Exception as exc:errors.append(str(exc))
                finally:done.set()
            with f.store.lock:
                worker=threading.Thread(target=deliver,daemon=True);worker.start()
                completed_with_lock_held=done.wait(.5)
                elapsed_ms=(time.perf_counter()-started)*1000.
            worker.join(timeout=3.)
        assert not worker.is_alive() and not errors,errors
        assert f.e.state_map['light.kitchen']['state']=='on'
        assert any(row[0]=='sensor.poll' for row in f.e.pending_archive)
        f.service.assert_not_called()
        result=dict(completed_with_store_writer_held=completed_with_lock_held,
            host_deadline_ms=500,host_elapsed_ms=round(elapsed_ms,3),current_delivered=True,
            poll_archive_delivered=True,ha_service_calls=0,
            controlled_fixture=True,physical_pi_latency_claim=False)
        print(json.dumps(result));return 0 if completed_with_lock_held else 1
    finally:
        if worker is not None:worker.join(timeout=3.)
        f.tearDown()


if __name__=='__main__':raise SystemExit(main())
