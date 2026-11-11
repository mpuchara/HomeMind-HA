"""Real production child, damaged table pages, two agents and durable Shadow model."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tests'))
import support
from test_release_080_training_process import ActualWorkerSmoke

def main():
    results = {}
    fixture = ActualWorkerSmoke()
    for name, kwargs in (
        ('healthy', {}),
        ('index', dict(corrupt_archive_indexes=True)),
        ('table_two_agents', dict(corrupt_table_page=True,two_agents=True,concurrent_writes=True)),
        ('http_and_table', dict(corrupt_table_page=True,http_gap=True)),
        ('kill_and_restart', dict(corrupt_table_page=True,two_agents=True,kill_and_restart=True)),
    ):
        started = time.monotonic()
        results[name] = fixture.run_worker_fixture(samples=480,corruption_recovery=True,qualify=True,**kwargs)
        results[name]['validation_wall_seconds'] = time.monotonic()-started
        assert results[name]['agent_mode'] == 'shadow', results[name]
        assert results[name]['agent_progress'] == 1.0, results[name]
        print(name, results[name]['learning_hash'],flush=True)
    assert results['healthy']['learning_hash']==results['index']['learning_hash']
    assert results['table_two_agents']['history_recovery']['gaps']
    results['insufficient'] = fixture.run_worker_fixture(samples=2,corruption_recovery=True,qualify=True,expect_unavailable=True)
    path = ROOT/'docs/benchmarks/corruption-recovery-0156.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(path)

if __name__=='__main__':
    main()
