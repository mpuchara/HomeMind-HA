"""Same million-row, 100-entity full-worker fixture on 0.15.8 and candidate.

Reference source is extracted from its pinned commit into a disposable directory.
RSS/CPU are child rusage on Linux; unsupported host counters remain null.
"""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
REFERENCE='bc60087a2085165ecd6d273a31c1ee25f907d8b6'


def run(source, rows):
    code="""
import json,sys
sys.path.insert(0,sys.argv[1]+'/tests')
import support
sys.path.insert(0,sys.argv[2]+'/adaptive_ai/src')
from test_release_080_training_process import ActualWorkerSmoke
r=ActualWorkerSmoke().run_worker_fixture(samples=480,corruption_recovery=True,qualify=True,large_noise_rows=int(sys.argv[3]))
print(json.dumps(r,sort_keys=True))
"""
    process=subprocess.run([sys.executable,'-c',code,str(ROOT),str(source),str(rows)],cwd=ROOT,
                           capture_output=True,text=True,timeout=900)
    if process.returncode:
        raise RuntimeError(process.stdout+process.stderr)
    return json.loads(process.stdout.strip().splitlines()[-1])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--rows',type=int,default=1_000_000)
    parser.add_argument('--candidate-only',action='store_true')
    args=parser.parse_args()
    if args.rows<1_000_000:
        parser.error('acceptance fixture requires at least one million history rows')
    results={}
    if not args.candidate_only:
        try:
            raw=subprocess.check_output(['git','archive','--format=tar',REFERENCE,'adaptive_ai/src'],cwd=ROOT,stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            subprocess.run(['git','fetch','--depth=1','origin',REFERENCE],cwd=ROOT,check=True)
            raw=subprocess.check_output(['git','archive','--format=tar',REFERENCE,'adaptive_ai/src'],cwd=ROOT)
        with tempfile.TemporaryDirectory(prefix='hm-reference-0158-') as directory:
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                # Pinned git archive, regular files below the one requested tree.
                for member in archive.getmembers():
                    target=(Path(directory)/member.name).resolve()
                    if not target.is_relative_to(Path(directory).resolve()) or member.issym() or member.islnk():
                        raise RuntimeError('Invalid reference archive path')
                archive.extractall(directory)
            results['reference']=run(Path(directory),args.rows)
    results['candidate']=run(ROOT,args.rows)
    candidate=results['candidate']
    assert candidate['agent_mode']=='shadow' and candidate['agent_progress']==1.
    tables=candidate['history_recovery']['copy_tables']
    assert tables['events']['rows_copied']==0,tables['events']
    assert tables['historical_experiences']['rows_copied']==0,tables['historical_experiences']
    assert tables['feature_observation_events']['rows_copied']==0,tables['feature_observation_events']
    assert tables['entity_history']['source_rows_scanned']==960,tables['entity_history']
    if 'reference' in results:
        reference=results['reference']
        assert candidate['learning_hash']==reference['learning_hash'], 'healthy model parity failure'
        results['comparison']=dict(
            reference_source_rows=sum(reference['source_table_rows'].values()),
            candidate_source_rows=sum(row['source_rows_scanned'] for row in tables.values()),
            reference_correct_guard_rows=sum(reference['source_table_rows'].get(name,0) for name in (
                'rl_feedback','teaching_labels','teaching_rl_labels','manual_context_feedback','manual_feedback_journal',
                'manual_feedback_effects','agent_correct_operations','tiny_mlp_correct_batches','tiny_mlp_correct_samples')),
            candidate_correct_guard_rows=sum(row.get('version_guard_rows_scanned',0) for row in tables.values()),
            reference_worker_wall_seconds=reference['worker_wall_seconds'],
            candidate_worker_wall_seconds=candidate['worker_wall_seconds'],
            wall_speedup=reference['worker_wall_seconds']/candidate['worker_wall_seconds'],
            learning_hash_identical=True,
            reference_resources=reference['worker_resource_metrics'],
            candidate_resources=candidate['worker_resource_metrics'])
    path=ROOT/'docs/benchmarks/generation-preparation-0159.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results.get('comparison',dict(candidate_rows=tables['entity_history']['source_rows_scanned'])),indent=2))
    print(path)


if __name__=='__main__':
    main()
