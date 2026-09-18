from contextlib import ExitStack
from pathlib import Path
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


class FinalCompositionManualHoldTests(unittest.TestCase):
    def test_explicit_user_target_change_enters_runtime_manual_hold(self):
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

                hold_calls = []
                original_set_manual_hold = engine.set_manual_hold

                def traced_set_manual_hold(subject, rt, timestamp):
                    result = original_set_manual_hold(subject, rt, timestamp)
                    hold_calls.append({
                        "timestamp": timestamp,
                        "same_runtime_object": rt is engine.runtime.get(subject["id"]),
                        "runtime_until": rt.get("manual_override_until"),
                        "engine_runtime_until": (
                            engine.runtime.get(subject["id"], {}).get("manual_override_until")
                        ),
                        "hold_source": store.meta_get(
                            "manual_hold_source:" + subject["id"], ""
                        ),
                        "hold_meta": store.meta_get(
                            "manual_hold:" + subject["id"], "0"
                        ),
                    })
                    return result

                engine.set_manual_hold = traced_set_manual_hold

                base = 1_750_000_000.0
                episode = 991
                action = 0
                diagnostics = {}
                for tick in range(26):
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
                    before = dict(engine.runtime.get(agent["id"], {}))
                    target = states[core.TARGET]
                    current = 1.0 if target["state"] == "on" else 0.0
                    own_echo = engine.own_command_echo(agent, target, current)
                    controller.decide(
                        states, tick, ts, scenario="manual_change", phase="future"
                    )
                    after = dict(engine.runtime.get(agent["id"], {}))
                    diagnostics[tick] = {
                        "before_previous_target": before.get("previous_target"),
                        "before_pending": before.get("pending"),
                        "context": target.get("context"),
                        "own_echo": own_echo,
                        "current": current,
                        "after_previous_target": after.get("previous_target"),
                        "after_manual_override_until": after.get("manual_override_until"),
                        "after_last_change_origin": after.get("last_change_origin"),
                        "hold_source": store.meta_get(
                            "manual_hold_source:" + agent["id"], ""
                        ),
                        "hold_meta": store.meta_get(
                            "manual_hold:" + agent["id"], "0"
                        ),
                    }
                    action = controller.action

                rt = engine.runtime.get(agent["id"], {})
                self.assertGreater(
                    float(rt.get("manual_override_until") or 0.0),
                    base + 25,
                    msg=(
                        "manual hold missing; "
                        f"diagnostics={{24: {diagnostics[24]}, 25: {diagnostics[25]}}}; "
                        f"set_manual_hold_calls={hold_calls}"
                    ),
                )
                self.assertEqual(
                    store.meta_get("manual_hold_source:" + agent["id"], ""),
                    "explicit_user_v8",
                )
                self.assertEqual(rt.get("last_change_origin"), "manual_user")
        finally:
            if engine is not None:
                runner._close_shipped_runtime(engine)
            if shipped_core is not None:
                shipped_core.ENGINE = None
                shipped_core.STORE = None
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
