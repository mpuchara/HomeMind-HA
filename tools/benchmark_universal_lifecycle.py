"""Disposable native SQLite U05b.3 lifecycle benchmark; HOST ONLY, never HA/Pi acceptance."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '1'
# storage imports its module-level Store: isolate even that bootstrap database.
_bootstrap = tempfile.TemporaryDirectory(prefix='hm-offline-lifecycle-bootstrap-')
os.environ['ADAPTIVE_AI_DATA'] = _bootstrap.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adaptive_ai/src'))
from storage import Store
from manual_feedback_contract import ManualFeedbackJournal
from universal_checkpoint_lifecycle import CheckpointLifecycle, _connection, _snapshot
from universal_scorer_training import TrainingConfig, context_from_sample
from universal_training_fixture import make_native_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    timings = {}

    def measured(name, fn):
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            return fn()
        finally:
            timings[name] = dict(wall_seconds=time.perf_counter()-wall, cpu_seconds=time.process_time()-cpu)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/'offline.db'
        store = Store(path)
        source, split = measured('synthetic_native_setup', lambda: make_native_source(store))
        service = CheckpointLifecycle(store)
        child = measured('clean_child_training', lambda: service.rebuild(
            source, parent_seed=1, **split, config=TrainingConfig(epochs=24)))
        assert child['status'] == 'optimization_complete', child
        accepted = measured('validation_atomic_publication', lambda: service.publish(child['checkpoint'], source))
        assert accepted['status'] == 'published', accepted
        aid = accepted['artifact_id']
        restored = measured('restart_restore', lambda: CheckpointLifecycle(Store(path)).load(aid, source))
        with _connection(store) as c:
            data, _ = _snapshot(c, source)
        projection = measured('guarded_offline_projection', lambda: restored.rank_context(context_from_sample(data.samples[-1])))
        assert projection['control_eligible'] is False and projection['promotion_eligible'] is False
        repeat = measured('reproduce_same_evidence', lambda: service.publish(child['checkpoint'], source))
        assert repeat['status'] == 'reproduced' and repeat['independent_evidence'] is False
        with store.conn() as c:
            artifact_bytes = c.execute('SELECT LENGTH(artifact) FROM universal_validated_checkpoints').fetchone()[0]
            claims = c.execute('SELECT COUNT(*) FROM universal_checkpoint_holdouts').fetchone()[0]
        ManualFeedbackJournal(store, clock=lambda:999999.).undo('synthetic-feedback:0')
        try:
            measured('undo_invalidation', lambda: service.load(aid, source))
        except ValueError as exc:
            assert 'revoked' in str(exc), exc
        else:
            raise AssertionError('revoked Correct still qualified')
        sizes = {p.name: p.stat().st_size for p in Path(folder).glob('offline.db*')}
        result = dict(scope='synthetic native Store; host only; no HA/Pi/control acceptance',
                      passed=True, timings=timings, artifact_bytes=artifact_bytes,
                      holdout_claims=claims, database_bytes=sizes,
                      feature_bytes=child['feature_bytes'], control_eligible=False, promotion_eligible=False)
    encoded = json.dumps(result, indent=2, allow_nan=False)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded)


if __name__ == '__main__':
    main()
