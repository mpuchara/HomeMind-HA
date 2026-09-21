"""0.14.61 regression: the debug-export helper must be reachable through shipped HTTP."""

import unittest

from support import ROOT


class Release061DebugExportStaticRouteTests(unittest.TestCase):
    def test_debug_export_asset_is_routed_before_runtime_gate(self):
        main = (ROOT / "adaptive_ai" / "src" / "main.py").read_text(encoding="utf-8")
        index = (ROOT / "adaptive_ai" / "src" / "static" / "index.html").read_text(encoding="utf-8")
        route = 'if path == "/debug_export_ui.js":'
        serve = 'return self.static("debug_export_ui.js", "application/javascript; charset=utf-8")'
        gate = "if not self.require_runtime():"
        self.assertIn('src="debug_export_ui.js?v=', index)
        self.assertIn(route, main)
        self.assertIn(serve, main)
        self.assertLess(main.index(route), main.index(gate))


if __name__ == "__main__":
    unittest.main()
