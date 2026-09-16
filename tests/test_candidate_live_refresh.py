from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CandidateLiveRefreshContractTests(unittest.TestCase):
    def test_backend_exposes_lightweight_candidate_live_route(self):
        text = (ROOT / "adaptive_ai/src/agent_candidate_card_summary.py").read_text(encoding="utf-8")
        self.assertIn('def live_candidate_snapshots(manager):', text)
        self.assertIn('path == "/api/candidate-live"', text)
        self.assertIn('state_map = dict(manager.engine.state_map)', text)
        self.assertIn('"parent_desired": row.get("parent_desired") if fresh else None', text)
        self.assertIn('"candidate_desired": row.get("child_desired") if fresh else None', text)
        self.assertIn('manager.live_snapshots = lambda: live_candidate_snapshots(manager)', text)

    def test_candidate_decision_tiles_have_250ms_lightweight_refresh(self):
        text = (ROOT / "adaptive_ai/src/static/candidate_preference_ui.js").read_text(encoding="utf-8")
        self.assertIn("fetch('api/candidate-live'", text)
        self.assertIn('setTimeout(liveLoop,250)', text)
        self.assertIn('Fast refresh changes text only', text)
        self.assertIn('ensureDecisionStrip(card,merged)', text)

    def test_heavy_refresh_cannot_rewind_fresher_live_snapshot(self):
        text = (ROOT / "adaptive_ai/src/static/candidate_preference_ui.js").read_text(encoding="utf-8")
        self.assertIn('previous._liveSnapshotTs', text)
        self.assertIn('merged.shadow_current=previous.shadow_current', text)
        self.assertIn('previousDecisionTs>incomingDecisionTs', text)

    def test_current_chart_trace_is_rendered_last_with_outline(self):
        text = (ROOT / "adaptive_ai/src/static/agent_workflow_ui.js").read_text(encoding="utf-8")
        parent = text.index('data-series="parent_desired"')
        candidate = text.index('data-series="candidate_desired"')
        outline = text.index('data-series="current-outline"')
        current = text.index('data-series="current"', outline + 1)
        self.assertLess(parent, outline)
        self.assertLess(candidate, outline)
        self.assertLess(outline, current)
        self.assertIn('stroke-width="3"', text[current:current + 300])


if __name__ == "__main__":
    unittest.main()
