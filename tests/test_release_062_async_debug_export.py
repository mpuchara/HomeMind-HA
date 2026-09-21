"""0.14.62 regression: long Correct debug exports run as bounded async jobs."""

import unittest

from support import ROOT


STATIC = ROOT / "adaptive_ai" / "src" / "static"
SRC = ROOT / "adaptive_ai" / "src"


class Release062AsyncDebugExportTests(unittest.TestCase):
    def test_ui_starts_post_job_and_polls_small_status_reads(self):
        helper = (STATIC / "debug_export_ui.js").read_text(encoding="utf-8")
        self.assertIn("method:'POST'", helper)
        self.assertIn("/debug/correct-learning/export", helper)
        self.assertIn("api/debug/correct-learning/jobs/", helper)
        self.assertIn("adaptiveAiTimeoutMs:5000", helper)
        self.assertIn("Exporting… ${pct}%", helper)
        self.assertNotIn("debug/correct-learning?detail=", helper)

    def test_download_is_direct_anchor_navigation_not_long_fetch(self):
        helper = (STATIC / "debug_export_ui.js").read_text(encoding="utf-8")
        self.assertIn("/download", helper)
        self.assertIn("anchor.href=", helper)
        self.assertIn("anchor.download=filename", helper)
        self.assertNotIn("adaptiveAiTimeoutMs:0", helper)

    def test_backend_job_is_single_flight_ttl_bounded_and_runtime_independent_after_start(self):
        backend = (SRC / "correct_learning_debug.py").read_text(encoding="utf-8")
        self.assertIn("EXPORT_JOB_TTL_SECONDS = 600.0", backend)
        self.assertIn("MAX_CONCURRENT_EXPORT_JOBS = 1", backend)
        self.assertIn("another debug export is already running", backend)
        self.assertIn('"debug.correct_learning.job_status"', backend)
        self.assertIn('"debug.correct_learning.job_download"', backend)
        self.assertIn("require_runtime=False", backend)
        home = (STATIC / "home.js").read_text(encoding="utf-8")
        self.assertIn(":12000", home)


if __name__ == "__main__":
    unittest.main()
