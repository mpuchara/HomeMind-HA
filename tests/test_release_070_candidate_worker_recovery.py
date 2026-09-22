from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CandidateWorkerRecoveryContractTests(unittest.TestCase):
    def source(self):
        return (ROOT / "adaptive_ai" / "src" / "agent_candidates.py").read_text(encoding="utf-8")

    def test_candidate_scheduler_survives_one_row_exception(self):
        source = self.source()
        self.assertIn("def _worker_loop(self):", source)
        self.assertIn('self._record_worker_error("candidate_lifecycle", exc, row)', source)
        self.assertIn("for row in rows:", source)
        self.assertIn("except Exception as exc:", source)

    def test_candidate_reads_self_heal_dead_scheduler(self):
        source = self.source()
        self.assertIn("def _ensure_worker_alive(self):", source)
        self.assertIn("self._ensure_worker_alive()\n        return self.status(parent_id)", source)
        self.assertIn("def status(self, parent_id):\n        self._ensure_worker_alive()", source)
        self.assertIn("def list_status(self):\n        self._ensure_worker_alive()", source)

    def test_recovery_worker_is_single_flight_and_daemonized(self):
        source = self.source()
        self.assertIn("if self.is_alive() or (recovery is not None and recovery.is_alive()):", source)
        self.assertIn("target=self._worker_loop", source)
        self.assertIn("adaptive-ai-agent-candidates-recovery-", source)
        self.assertIn("daemon=True", source)

    def test_candidate_status_exposes_worker_health(self):
        source = self.source()
        self.assertIn('result["worker"] = self._worker_health()', source)
        self.assertIn('"heartbeat_age_seconds"', source)
        self.assertIn('"error_count"', source)
        self.assertIn('"restart_count"', source)


if __name__ == "__main__":
    unittest.main()
