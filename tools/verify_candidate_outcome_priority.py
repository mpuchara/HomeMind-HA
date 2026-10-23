"""Reproduce before-Live blocking on a held Store mutex using real Candidate hooks.

--source can point at a frozen baseline checkout. Host fixture only, no HA/device
latency claim. Release the mutex before joining even when the baseline blocks.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.source.resolve()
    sys.path[:0] = [str(root / 'tests'), str(root / 'adaptive_ai/src')]
    import support
    from test_candidate_shadow_context import CandidateShadowExactContextTests
    f = CandidateShadowExactContextTests()
    f.setUp()
    try:
        f.manager.request_candidate_shadow_persistence = Mock()
        states = f.states('on')
        f.engine.runtime[f.root['id']] = dict(last_prediction=1., last_confidence=.88, last_inference_ts=10.)
        f.manager.before_live_process(f.root, states)
        f.engine.runtime[f.root['id']]['last_inference_ts'] = 20.
        f.engine.policy(f.root).features(states, None, at_ts=20.25)
        f.manager.after_live_process(f.root, states)
        assert f.manager.drain_deferred_candidate_shadow(max_roots=1) == 1
        f.manager.flush_candidate_shadow_decisions(force=True)
        states['light.exact'].update(state='on',last_updated='2026-10-23T10:00:00+00:00')
        done, errors, elapsed = threading.Event(), [], []
        def call():
            start = time.perf_counter()
            try:
                f.manager.before_live_process(f.root, states)
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                elapsed.append((time.perf_counter()-start)*1000)
                done.set()
        with f.store.lock:
            thread = threading.Thread(target=call, daemon=True)
            thread.start()
            completed_while_locked = done.wait(.25)
        thread.join(10.)
        assert not thread.is_alive() and not errors, errors
        f.manager.flush_candidate_shadow_decisions(force=True)
        with f.store.conn() as c:
            pairs = c.execute('SELECT count(*) FROM candidate_generation_pairs').fetchone()[0]
        assert pairs == 1
        f.executor.service.assert_not_called()
        print(json.dumps(dict(completed_while_store_locked=completed_while_locked,
            hook_ms=elapsed[0],committed_pairs=pairs,ha_calls=0,
            semantics='controlled host mutex fixture; not physical HA latency')))
    finally:
        f.tearDown()


if __name__ == '__main__':
    main()
