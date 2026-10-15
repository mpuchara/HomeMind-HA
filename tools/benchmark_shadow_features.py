"""Independent-source Shadow feature, prediction and post-label learning parity."""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(baseline, passes=80, *, contended=False):
    series = []
    for repeat in range(3):
        pair = {}
        order = [('before', baseline), ('after', ROOT)]
        if repeat % 2:
            order.reverse()
        for name, source in order:
            command = [sys.executable, str(ROOT / 'tools/benchmark_runtime_hot_path.py'),
                       '--one', '--source-root', str(source), '--passes', str(passes)]
            if contended:
                command.append('--contended')
            pair[name] = json.loads(subprocess.check_output(command, text=True))
        before, after = pair['before'], pair['after']
        assert before['every_feature_label_meta_and_prediction_sha256'] == after['every_feature_label_meta_and_prediction_sha256']
        assert before['full_checksum_calls'] == after['full_checksum_calls'] == 4 * passes
        assert after['feature_observation_computations_per_pass'] < before['feature_observation_computations_per_pass']
        pair['observation_speedup'] = before['observation_total_ms'] / after['observation_total_ms']
        pair['feature_speedup'] = before['feature_total_ms'] / after['feature_total_ms']
        series.append(pair)
    learning = {}
    for name, source in [('before', baseline), ('after', ROOT)]:
        command = [sys.executable, str(ROOT / 'tools/benchmark_observation_census.py'),
                   '--worker-root', str(source), '--passes', str(passes)]
        learning[name] = json.loads(subprocess.check_output(command, text=True))
    for key in ('features_sha256', 'decisions_and_counts_sha256', 'trained_heads_sha256', 'labels', 'checksum_checks'):
        assert learning['before'][key] == learning['after'][key], key
    return dict(synthetic=True, independent_sources=True, background_cpu_thread=contended,
                passes=passes, series=series, learning=learning,
                feature_speedup_median=statistics.median(row['feature_speedup'] for row in series),
                observation_speedup_median=statistics.median(row['observation_speedup'] for row in series),
                full_features_metadata_predictions_checksums_and_trained_weights_identical=True,
                limit='Host component measurement with real candidate observer, production wrapper and post-label learning. HA ingress, microSD and concurrent checkpoint I/O excluded. Not a Raspberry Pi or end-to-end latency guarantee.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--passes', type=int, default=80)
    parser.add_argument('--contended', action='store_true')
    args = parser.parse_args()
    if args.passes < 1:
        parser.error('passes must be positive')
    print(json.dumps(run(args.baseline_root.resolve(), args.passes, contended=args.contended), indent=2))
