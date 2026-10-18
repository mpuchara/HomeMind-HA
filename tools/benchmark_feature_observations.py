"""Compare exact raw-event protection with many retained overlapping windows.

This measures one SQL component, not agent or physical Home Assistant latency.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def one(root):
    with tempfile.TemporaryDirectory() as folder:
        os.environ['ADAPTIVE_AI_DATA'] = folder
        for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            os.environ[name] = '1'
        sys.path.insert(0, str(root/'adaptive_ai/src'))
        from storage import Store
        from observation_contract import FeatureJournal
        store = Store(Path(folder)/'protection.db')
        journal = FeatureJournal(store, clock=lambda: 1000.)
        windows = [dict(window_id=f'{agent}-{i}', agent_id=str(agent),
            entities=[f'sensor.{j}' for j in range(8)],
            anchor_time=200.+i*.05+agent*.001, before=30., after=10.)
            for agent in range(20) for i in range(512)]
        journal.open_windows_batch(windows)
        durations = []
        for batch in range(10):
            records = [dict(entity_id=f'sensor.{i%8}',
                state=dict(entity_id=f'sensor.{i%8}', state=str(i), attributes={}),
                event_time=201.+batch*.1+i*.01, received_time=900.+i, source='benchmark')
                for i in range(64)]
            start = time.perf_counter()
            journal.record_batch(records)
            durations.append((time.perf_counter()-start)*1000)
        with store.conn() as c:
            rows = [tuple(row) for row in c.execute(
                'SELECT * FROM feature_observation_events ORDER BY event_key')]
        return dict(batch_ms=durations, rows=len(rows), windows=len(windows),
            result_sha256=hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            source_sha256=hashlib.sha256((root/'adaptive_ai/src/observation_contract.py')
                .read_bytes().replace(b'\r\n', b'\n')).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(one(args.source_root)))
        return
    assert args.baseline_root is not None
    runs = []
    for pair in range(3):
        for kind in (('baseline', 'current') if pair % 2 == 0 else ('current', 'baseline')):
            root = args.baseline_root if kind == 'baseline' else args.source_root
            result = subprocess.run([sys.executable, __file__, '--worker', '--source-root', str(root)],
                capture_output=True, text=True, check=True)
            row = json.loads(result.stdout.strip().splitlines()[-1])
            row.update(pair=pair, kind=kind)
            runs.append(row)
    assert len({row['result_sha256'] for row in runs}) == 1
    print(json.dumps(dict(runs=runs, all_results_identical=True,
        limitation='Host SQL component with overlapping windows; not full runtime or physical HA latency.')))


if __name__ == '__main__':
    main()
