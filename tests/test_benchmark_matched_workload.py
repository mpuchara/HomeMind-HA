import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MatchedWorkloadTests(unittest.TestCase):
    def run_fixture(self, script):
        command = [sys.executable, str(ROOT / 'tools' / script), '--source-root', str(ROOT),
                   '--agents', '1', '--sensor-count', '5', '--warmup', '1', '--events', '2',
                   '--candidates', '--correct', '--training', '--collector',
                   '--matched-updates', '4', '--matched-writes', '2']
        completed = subprocess.run(command, capture_output=True, text=True, timeout=90)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        line = next(line for line in completed.stdout.splitlines() if line.startswith('SCALING_JSON='))
        return json.loads(line.partition('=')[2])

    def test_matched_fit_preserves_schedule_counts_and_decisions_with_timeline(self):
        plain = self.run_fixture('benchmark_runtime_scaling.py')
        traced = self.run_fixture('benchmark_steady_state_timeline.py')
        for raw in (plain, traced):
            self.assertEqual(raw['ha_calls'], 0)
            self.assertEqual(raw['candidate_count'], 1)
            self.assertEqual(raw['background_work']['correct_writes'], 1)
            self.assertEqual(raw['background_work']['updates'], 12)
            self.assertEqual(raw['background_work']['model_writes'], 6)
            workload = raw['matched_workload']
            self.assertEqual(workload['queue_capacity'], 1)
            batches = workload['batches']
            self.assertEqual([batch['phase'] for batch in batches], ['warmup', 'steady', 'steady'])
            latest = 0
            for step, batch in enumerate(batches):
                self.assertEqual(batch['step'], step)
                self.assertLessEqual(latest, batch['release_ns'])
                self.assertLessEqual(batch['release_ns'], batch['trainer_start_ns'])
                self.assertLessEqual(batch['trainer_start_ns'], batch['trainer_end_ns'])
                self.assertLessEqual(batch['release_ns'], batch['inference_observed_ns'])
                latest = max(batch['trainer_end_ns'], batch['inference_observed_ns'])
        for key in ('input_sha256', 'decisions_sha256'):
            self.assertEqual(plain[key], traced[key])
        self.assertEqual(traced['collector']['dropped'], 0)
        self.assertEqual(traced['collector_native_rows']['decisions'], 3)
        self.assertEqual(traced['collector_native_rows']['observations'], 3)
        rows = [row for thread in traced['probe']['threads'] for row in thread['rows']]
        kinds = {row[0] for row in rows}
        self.assertTrue({'ingress', 'submit_call', 'executor_queue', 'worker_active', 'agent',
                         'context_features', 'prediction', 'intent_submit', 'training_update',
                         'sqlite_execute', 'store_lock_wait'} <= kinds)
        self.assertTrue(all(start <= end for _, start, end, _ in rows))


if __name__ == '__main__':
    unittest.main()
