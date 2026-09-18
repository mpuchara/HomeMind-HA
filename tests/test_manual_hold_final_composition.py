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
    """Exercise the shipped final composition in-process; caller isolates us in a subprocess."""
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
            built = core.build_training_data(11, replicas=1)
            current_policy = built["runtime"].policy
            created = store.create_agent({
                k: v for k, v in built["agent"].items()
                if k not in ("id", "mode", "training_state")
            })
            agent = dict(created)
            score = built["validation_balanced_accuracy"]
            shadow_qualified = bool(
                score > float(core.OPTIONS.get("candidate_benchmark_threshold", 0.78))
                and all(
                    v["samples"] >= 12
                    for v in built["validation_detail"]["counts"]["per_action"].values()
                )
            )
            store.set_training_state(
                agent["id"], "qualified" if shadow_qualified else "paused",
                score=score,
                samples=built["validation_detail"]["counts"]["samples"],
                source="manual_hold_final_composition_regression",
                detail=built["validation_detail"],
            )
            store.update_agent(agent["id"], {"mode": "shadow"})
            agent = store.get_agent_config(agent["id"])
            agent["training_state"] = "qualified" if shadow_qualified else "paused"
            current_policy.agent = agent
            store.save_model(agent["id"], current_policy.serialize())

            runtime = built["runtime"]
            runtime.agent = agent
            runtime.policy.agent = agent
            controller = runner.FinalRuntimeShadow(engine, agent, current_policy, runtime)

            base = 1_750_000_000.0
            episode = 991
            action = 0
            active_ticks = 0
            violations = 0
            for tick in range(36):
                ts = base + tick
                if tick == 24:
                    action = 1
                manual_event = tick == 25
                if manual_event:
                    action = 0
                states = core.observation(
                    11, episode, "manual_change", "future", tick, action,
                    manual_user=manual_event,
                )
                new_action, meta = controller.decide(
                    states, tick, ts, scenario="manual_change", phase="future"
                )
                new_action = int(bool(new_action))
                if 25 <= tick < 36:
                    active_ticks += int(bool(meta.get("manual_override_active")))
                    violations += int(new_action != 0)
                action = new_action

            rt = engine.runtime.get(agent["id"], {})
            return {
                "active_ticks": active_ticks,
                "violations": violations,
                "manual_override_until": float(rt.get("manual_override_until") or 0.0),
                "hold_source": store.meta_get(
                    "manual_hold_source:" + agent["id"], ""
                ),
                "last_change_origin": rt.get("last_change_origin"),
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
        # Full runtime composition installs many process-global compatibility overlays.
        # Run the focused probe in a child process so no installer can leak into another
        # unittest. The unchanged 3-seed F24 run remains the product-level CI oracle.
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
        self.assertEqual(result["active_ticks"], 11)
        self.assertEqual(result["violations"], 0)
        self.assertGreater(result["manual_override_until"], 1_750_000_025.0)
        self.assertEqual(result["hold_source"], "explicit_user_v8")
        self.assertEqual(result["last_change_origin"], "manual_user")


if __name__ == "__main__" and "--manual-hold-probe" in sys.argv:
    print("MANUAL_HOLD_PROBE=" + json.dumps(_manual_hold_probe(), sort_keys=True))
elif __name__ == "__main__":
    unittest.main()
