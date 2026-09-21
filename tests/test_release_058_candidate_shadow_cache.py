"""0.14.58 regression: a freshly built Candidate must leave the queued cache state."""

from pathlib import Path
import unittest

from support import ROOT


class Release058CandidateShadowCacheTests(unittest.TestCase):
    def test_direct_correct_start_build_invalidates_shadow_generation_cache(self):
        shadow = (ROOT / "adaptive_ai/src/agent_candidate_shadow_runtime.py").read_text(encoding="utf-8")
        wrapper = shadow.split("for method_name in (", 1)[1].split("    manager.invalidate_candidate_shadow_cache", 1)[0]
        self.assertIn('"_start_build"', wrapper)
        self.assertIn('"_finish_build_if_ready"', wrapper)
        self.assertIn("invalidate_generation_cache()", wrapper)

    def test_conservative_correct_can_finish_without_finish_build_if_ready(self):
        correct = (ROOT / "adaptive_ai/src/agent_candidate_conservative_correct.py").read_text(encoding="utf-8")
        block = correct.split("    def start_build(row):", 1)[1].split("    def finish_build_if_ready(row):", 1)[0]
        self.assertIn("_persist_gate(manager, fresh, gate)", block)
        self.assertIn("return True", block)
        self.assertIn("state='building'", block)


if __name__ == "__main__":
    unittest.main()
