"""Real quota recovery plus exact retained Candidate evidence, without HA access."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def run():
    with tempfile.TemporaryDirectory(prefix='automatic-storage-recovery-') as directory:
        os.environ['ADAPTIVE_AI_DATA'] = directory
        sys.path[:0] = [str(ROOT/'adaptive_ai/src'), str(ROOT/'tests')]
        from test_candidate_shadow_context import CandidateShadowExactContextTests
        from observation_contract import FeatureJournal
        from storage_recovery import StorageRecovery
        from settings import OPTIONS
        f = CandidateShadowExactContextTests()
        with patch.dict(OPTIONS, storage_auto_use_percent=60):
            f.setUp()
        try:
            store = f.store
            FeatureJournal(store)
            stamp = time.time()
            with store.conn() as c:
                c.executemany('INSERT INTO feature_observation_events '
                    '(event_key,contract_version,entity_id,event_time,received_time,attributes_json,source,quality,protected_until) '
                    'VALUES(?,1,?,?,?,?,?,1,?)', [(f'disposable-{i}','sensor.extra',stamp-7200,stamp-7200,
                    json.dumps({'trace':'x'*8192}),'ha',stamp+86400) for i in range(512)])
                c.execute("INSERT INTO app_meta VALUES('durable-preference-proof','keep')")
                store.storage_budget.observe_pages(c)
            budget = store.storage_budget
            budget.db_limit = int(budget.snapshot()['database_used_bytes'] / .91)
            limit = budget.db_limit
            assert budget.sample()['reason'] == 'database_budget'
            def observe(index):
                ts = 20 + index * .1
                states = f.states('on' if index % 2 else 'off')
                f.engine.runtime[f.root['id']] = dict(last_prediction=float(index%2),last_confidence=.88,last_inference_ts=ts-.01)
                f.manager.before_live_process(f.root,states)
                f.engine.runtime[f.root['id']]['last_inference_ts'] = ts
                f.engine.policy(f.root).features(states,None,at_ts=ts)
                f.manager.after_live_process(f.root,states)
                f.manager.drain_deferred_candidate_shadow(max_roots=1)
            for index in range(128):
                observe(index)
            assert f.manager.candidate_shadow_persistence_snapshot()['pending'] == 128
            before = budget.snapshot()
            recovery = StorageRecovery(store, threading.Event())
            times = []
            for _ in range(50):
                started = time.perf_counter()
                recovery.step(stamp)
                times.append((time.perf_counter()-started)*1000)
                if budget.snapshot()['level'] == 'normal':
                    break
            assert budget.db_limit == limit and budget.snapshot()['level'] == 'normal'
            with patch.object(f.engine,'policy',side_effect=AssertionError('replayed policy')):
                flushed = f.manager.flush_candidate_shadow_decisions(force=True)
            with store.conn() as c:
                rows = [tuple(row) for row in c.execute('SELECT ts,current,desired FROM candidate_generation_decisions WHERE generation_id=? ORDER BY ts',(f.generation_id,))]
                assert c.execute("SELECT value FROM app_meta WHERE key='durable-preference-proof'").fetchone()[0] == 'keep'
                assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            expected = [(20+i*.1,0.,float(i%2)) for i in range(128)]
            assert flushed == 128 and rows == expected
            f.executor.service.assert_not_called()
            return dict(synthetic=True,real_sqlite=True,before=before,after=budget.snapshot(),
                recovery=recovery.snapshot(),slice_times_ms=times,quota_unchanged=True,
                exact_retained_rows=len(rows),retained_rows_sha256=hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
                preferences_preserved=True,policy_replay=False,ha_service_calls=0,integrity_check='ok',
                limitation='Host fixture with real page pressure and production GC/Candidate persistence. No user database, Pi I/O or physical HA acknowledgment.',pass_all=True)
        finally:
            f.tearDown()


if __name__ == '__main__':
    print(json.dumps(run()))
