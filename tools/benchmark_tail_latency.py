"""Independent-source cold SQLite comparisons with large synthetic audit JSON."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def one(source, episodes, audit_kib, passes):
    with tempfile.TemporaryDirectory(prefix='tail-latency-') as directory:
        os.environ['ADAPTIVE_AI_DATA'] = directory
        sys.path[:0] = [str(source / 'adaptive_ai/src'), str(source)]
        from storage import Store
        from episode_evaluator import EpisodeEvaluator
        store = Store(Path(directory) / 'runtime.db')
        try:
            evaluator = EpisodeEvaluator(store)
            audit = json.dumps({'audit': 'x' * (audit_kib * 1024)})
            metrics = json.dumps({'meaningful': True, 'harmful': False,
                'meaningful_proxy': True, 'proxy_harmful': False,
                'needed_on_delay_seconds': 1., 'off_while_needed_seconds': 0.,
                'unnecessary_on_seconds': 0., 'false_arrival_prediction': 0,
                'retrigger_count': 0, 'chatter_count': 0, 'manual_correction_count': 0,
                'automation_disagreement_seconds': 1.})
            with store.conn() as connection:
                for i in range(episodes):
                    eid = f'episode-{i:06}'
                    connection.execute("INSERT INTO episode_evaluator_episodes VALUES(?,1,'light_power','a',?,?,?, ?,?,NULL,NULL,NULL,?,?)",
                        (eid, float(i), float(i + 60), '{}', '{"light_need":"true"}', audit, eid, float(i)))
                    for key in ('p', 'c'):
                        connection.execute('INSERT INTO episode_evaluator_policy_results VALUES(?,?,?,0,1,?,?)',
                            (eid, key, 'candidate', metrics, audit))
                # Measure the one-time upgrade separately from the query cost.
                connection.execute('DROP INDEX IF EXISTS idx_episode_compare_meta_206')
                connection.execute('DROP INDEX IF EXISTS idx_episode_compare_pair_206')
            started = time.perf_counter()
            evaluator = EpisodeEvaluator(store)
            migration_ms = (time.perf_counter() - started) * 1000
            wall, cpu, digests = [], [], []
            for _ in range(passes):
                evaluator._comparison_cache.entries.clear()
                evaluator._comparison_cache.bytes = 0
                started, used_cpu = time.perf_counter(), time.process_time()
                result = evaluator.compare_policies('a', 'p', 'c')
                wall.append((time.perf_counter() - started) * 1000)
                cpu.append((time.process_time() - used_cpu) * 1000)
                assert result['matched_episodes'] == episodes, result
                digests.append(hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest())
            assert len(set(digests)) == 1
            with store.connection_session() as connection:
                indexes = connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'idx_episode_compare_%206'").fetchall()
                index_bytes = None
                try:
                    index_bytes = connection.execute("SELECT SUM(pgsize) FROM dbstat WHERE name LIKE 'idx_episode_compare_%206'").fetchone()[0]
                except Exception:
                    pass  # dbstat is optional on some Python SQLite builds.
            return dict(episodes=episodes, audit_bytes_per_row=len(audit.encode()), passes=passes,
                cold_comparison_median_ms=statistics.median(wall), cold_comparison_max_ms=max(wall),
                comparison_cpu_median_ms=statistics.median(cpu), comparison_sha256=digests[0],
                index_initialization_ms=migration_ms, compact_indexes=len(indexes), compact_index_bytes=index_bytes,
                all_results_identical=True, comparison_cache=evaluator.comparison_cache_snapshot())
        finally:
            store.stop_runtime_cache()


def paired(baseline, episodes, audit_kib, passes):
    series = []
    for repeat in range(3):
        pair = {}
        for label in (('before', 'after') if repeat % 2 == 0 else ('after', 'before')):
            output = subprocess.check_output([sys.executable, str(Path(__file__).resolve()), '--one',
                '--source-root', str(baseline if label == 'before' else ROOT), '--episodes', str(episodes),
                '--audit-kib', str(audit_kib), '--passes', str(passes)], text=True)
            pair[label] = json.loads(output.strip().splitlines()[-1])
        assert pair['before']['comparison_sha256'] == pair['after']['comparison_sha256']
        pair['cold_comparison_speedup'] = pair['before']['cold_comparison_median_ms'] / pair['after']['cold_comparison_median_ms']
        series.append(pair)
    return dict(synthetic=True, independent_sources=True, series=series,
        median_cold_comparison_speedup=statistics.median(p['cold_comparison_speedup'] for p in series),
        limit='Large synthetic valid audits, real SQLite and production comparison. Cache cleared deliberately. Index build is reported separately. Host measurements exclude Pi storage, other workers, HA network and physical actuation.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--one', action='store_true')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--episodes', type=int, default=96)
    parser.add_argument('--audit-kib', type=int, default=128)
    parser.add_argument('--passes', type=int, default=8)
    args = parser.parse_args()
    if min(args.episodes, args.audit_kib, args.passes) < 1 or (not args.one and args.baseline_root is None):
        parser.error('positive counts and baseline-root required (except --one)')
    print(json.dumps(one(args.source_root.resolve(), args.episodes, args.audit_kib, args.passes)
        if args.one else paired(args.baseline_root.resolve(), args.episodes, args.audit_kib, args.passes), indent=None if args.one else 2))
