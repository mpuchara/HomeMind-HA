"""0.14.60 regression: one-click Correct learning debug export from Live/Candidate cards."""

from pathlib import Path
import unittest

from support import ROOT


STATIC = ROOT / "adaptive_ai" / "src" / "static"
SRC = ROOT / "adaptive_ai" / "src"


class Release060DebugExportUiTests(unittest.TestCase):
    def test_live_agent_card_has_export_debug_button(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        workflow = (STATIC / "agent_workflow_ui.js").read_text(encoding="utf-8")
        self.assertIn(r"exportCorrectLearningDebug('${a.id}',this)", app)
        self.assertIn(">Export debug</button>", app)
        self.assertIn('data-wf="debug">Export debug</button>', workflow)
        self.assertIn(
            "window.exportCorrectLearningDebug?.(a.id,e.currentTarget)", workflow
        )

    def test_candidate_card_exports_using_generation_reference(self):
        candidate = (STATIC / "candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn('data-wf="debug">Export debug</button>', candidate)
        self.assertIn(
            "window.exportCorrectLearningDebug?.(ref,e.currentTarget)", candidate
        )
        self.assertIn("const candidateRef=c=>String(c.generation_id||c.candidate_id||'')", candidate)

    def test_debug_helper_requests_full_bounded_export_and_downloads_json(self):
        helper = (STATIC / "debug_export_ui.js").read_text(encoding="utf-8")
        for marker in (
            "detail:'full'",
            "label_limit:'256'",
            "window_seconds:'120'",
            "raw_rows_per_label:'768'",
            "encodeURIComponent(ref)",
            "new Blob([JSON.stringify(payload,null,2)]",
            "anchor.download=filename",
            "debugExportBusy",
        ):
            self.assertIn(marker, helper)

    def test_candidate_generation_ref_is_url_decoded_by_backend(self):
        backend = (SRC / "correct_learning_debug.py").read_text(encoding="utf-8")
        self.assertIn("from urllib.parse import parse_qs, unquote, urlsplit", backend)
        self.assertIn('unquote(params["agent_id"])', backend)

    def test_index_loads_versioned_helper_before_candidate_ui(self):
        index = (STATIC / "index.html").read_text(encoding="utf-8")
        helper = '<script src="debug_export_ui.js?v=0.14.60"></script>'
        candidate = '<script src="candidate_ui.js?v=0.14.60"></script>'
        self.assertIn(helper, index)
        self.assertIn(candidate, index)
        self.assertLess(index.index(helper), index.index(candidate))


if __name__ == "__main__":
    unittest.main()
