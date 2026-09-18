from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import run_product_runtime_benchmark as runner


class FinalCompositionManualHoldTests(unittest.TestCase):
    def test_explicit_user_target_change_enters_runtime_manual_hold(self):
        # run_seed intentionally executes the fully composed shipped runtime in a fresh
        # subprocess. This keeps the many compatibility installers from leaking into the
        # rest of the unittest process while using F24 unchanged as the regression oracle.
        row = runner.run_seed(11, replicas=1)
        metrics = row["metrics"]["production_current"]
        self.assertEqual(metrics["manual_override_events"], 1)
        self.assertGreater(metrics["runtime_manual_hold_ticks"], 0)
        self.assertEqual(metrics["manual_override_violations"], 0)


if __name__ == "__main__":
    unittest.main()
