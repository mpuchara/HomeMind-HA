from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
SRC = ROOT / "adaptive_ai" / "src"
for path in (str(TOOLS), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

import benchmark_product_runtime as core
import run_product_runtime_benchmark as runner


def _manual_hold_probe():
    """Exercise one explicit-user event through the shipped final composition."""
    tmp = tempfile.TemporaryDirectory(prefix="homemind-manual-hold-final-")
    store = core.Store(Path(tmp.name) / "adaptive_ai.sqlite")
    shipped_core = None
    engine = None
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(core.engine_module, "STORE", store))
            stack.enter_context(patch.object(core.executor_module, "STORE", store))
            stack.enter_context(patch.object(
                core.executor_module.HA, "service",
                side_effect=AssertionError("Shadow must never dispatch HA service"),
            ))
            stack.enter_context(patch.object(
                core.AUTOMATION_KNOWLEDGE, "hints_for_target", return_value=(set(), [])
            ))

            shipped_core, engine, _ = runner._prepare_shipped_runtime(store)
            template = core.agent_template()
            created = store.create_agent({
                k: v for k, v in template.items()
                if k not in ("id", "mode", "training_state")
            })
            agent = dict(created)
            store.set_training_state(
                agent["id"], "qualified", score=1.0, samples=80,
                source="manual_hold_final_composition_regression",
                detail={
                    "balanced": True,
                    "counts": {
                        "samples": 80,
                        "correct": 80,
                        "per_action": {
                            "0": {"samples": 40, "correct": 40},
                            "1": {"samples": 40, "correct": 40},
                        },
                    },
                },
            )
            store.update_agent(agent["id"], {"mode": "shadow"})
            agent = store.get_agent_config(agent["id"])
            agent["training_state"] = "qualified"

            # The explicit-user branch is evaluated before policy inference. Seed only the
            # immediately previous physical target value so ON -> user OFF is a real change.
            engine.runtime[agent["id"]] = {
                "previous_target": 1.0,
                "manual_override_until": 0.0,
                "pending": None,
                "restored": True,
            }
            ts = 1_750_000_025.0
            states = core.observation(
                11, 991, "manual_change", "future", 25, 0, manual_user=True
            )
            engine.state_map = dict(states)
            with patch.object(core.engine_module, "now_ts", return_value=ts):
                engine.process_agent(agent, engine.state_map, {core.TARGET})

            rt = engine.runtime.get(agent["id"], {})
            return {
                "manual_override_until": float(rt.get("manual_override_until") or 0.0),
                "hold_source": store.meta_get(
                    "manual_hold_source:" + agent["id"], ""
                ),
                "last_change_origin": rt.get("last_change_origin"),
                "previous_target": rt.get("previous_target"),
            }
    finally:
        if engine is not None:
            runner._close_shipped_runtime(engine)
        if shipped_core is not None:
            shipped_core.ENGINE = None
            shipped_core.STORE = None
        tmp.cleanup()


class FinalCompositionManualHoldTests(unittest.TestCase):
    def test_explicit_user_target_change_enters_runtime_manual_hold(self):
        # Runtime composition installs process-global compatibility overlays. Isolate the
        # probe so no installer can leak into another unittest. F24 remains unchanged and
        # independently verifies the complete 11-tick manual window on all three seeds.
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--manual-hold-probe"],
            check=True,
            capture_output=True,
            text=True,
        )
        marker = "MANUAL_HOLD_PROBE="
        rows = [
            line[len(marker):]
            for line in proc.stdout.splitlines()
            if line.startswith(marker)
        ]
        self.assertTrue(rows, msg=proc.stdout[-2000:] + proc.stderr[-2000:])
        result = json.loads(rows[-1])
        self.assertGreater(result["manual_override_until"], 1_750_000_025.0)
        self.assertEqual(result["hold_source"], "explicit_user_v8")
        self.assertEqual(result["last_change_origin"], "manual_user")
        self.assertEqual(result["previous_target"], 0.0)


if __name__ == "__main__" and "--manual-hold-probe" in sys.argv:
    print("MANUAL_HOLD_PROBE=" + json.dumps(_manual_hold_probe(), sort_keys=True))
elif __name__ == "__main__":
    unittest.main()
