from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CorrectChartDragZoomContract(unittest.TestCase):
    def test_correct_chart_supports_drag_selection_and_cursor_anchored_zoom(self):
        text = (ROOT / "adaptive_ai/src/static/agent_workflow_ui.js").read_text(encoding="utf-8")
        self.assertIn("przeciągnij poziomo po wykresie", text)
        self.assertIn("data-selection", text)
        self.assertIn("svg.onwheel=", text)
        self.assertIn("hit.onpointerdown=", text)
        self.assertIn("hit.onpointermove=", text)
        self.assertIn("hit.onpointerup=", text)
        self.assertIn("Math.abs(endFraction-startFraction)>.015", text)
        self.assertIn("range={start:start+Math.min(startFraction,endFraction)*width,end:start+Math.max(startFraction,endFraction)*width}", text)
        self.assertIn("else inspect(start+endFraction*width)", text)
        self.assertIn("function zoom(factor,anchor=.5)", text)


if __name__ == "__main__":
    unittest.main()
