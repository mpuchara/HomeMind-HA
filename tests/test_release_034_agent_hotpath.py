import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import *
import engine as engine_module
import executor as executor_module
from engine import Engine
from intent import ActionIntent


class CountingStore:
    def __init__(self, rows):
        self.rows = {str(row["id"]): dict(row) for row in rows}
        self.list_calls = 0
        self.get_calls = 0
        self.events = []

    def list_agent_configs(self):
        self.list_calls += 1
        return [dict(row) for row in self.rows.values()]

    def get_agent_config(self, agent_id):
        self.get_calls += 1
        row = self.rows.get(str(agent_id))
        return dict(row) if row else None

    def event(self, *args):
        self.events.append(args)


class ExplodingStore:
    def get_agent_config(self, *args, **kwargs):
        raise AssertionError("Shadow executor hot path must not read durable agent config")


class AgentHotPathTests(unittest.TestCase):
    def make_engine(self):
        runtime = Engine()
        self.addCleanup(
            lambda: runtime.control_workers.shutdown(wait=False, cancel_futures=True)
        )
        self.addCleanup(
            lambda: runtime.poll_worker.shutdown(wait=False, cancel_futures=True)
        )
        return runtime

    def configured_agent(self, aid="a", target="light.kitchen"):
        row = agent(id=aid, target_entity=target)
        row.update({
            "enabled": True,
            "mode": "shadow",
            "training_state": "qualified",
            "input_entities": [],
        })
        return row

    def test_repeated_events_use_in_memory_dependency_index(self):
        runtime = self.make_engine()
        configured = self.configured_agent()
        runtime.models[configured["id"]] = SimpleNamespace(
            schema=SimpleNamespace(entities=("binary_sensor.kitchen_motion",))
        )
        runtime.experiments = SimpleNamespace(
            watches=lambda aid: set(),
            cancel=lambda *args, **kwargs: None,
        )
        fake = CountingStore([configured])

        with patch.object(engine_module, "STORE", fake):
            first = runtime._active_agents_for_changes({"binary_sensor.kitchen_motion"})
            second = runtime._active_agents_for_changes({"binary_sensor.kitchen_motion"})

        self.assertEqual([row["id"] for row in first], [configured["id"]])
        self.assertEqual([row["id"] for row in second], [configured["id"]])
        self.assertEqual(fake.list_calls, 1)

    def test_process_target_uses_routed_agent_snapshot_without_second_config_read(self):
        runtime = self.make_engine()
        configured = self.configured_agent()
        runtime.process_agent = Mock(return_value=None)
        fake = CountingStore([configured])
        snapshot = (
            {"light.kitchen": {"entity_id": "light.kitchen", "state": "off", "attributes": {}}},
            1,
            {"light.kitchen": 1},
            runtime.context.home.revision,
        )

        with patch.object(engine_module, "STORE", fake):
            runtime.process_target([configured], {"binary_sensor.kitchen_motion"}, snapshot)

        runtime.process_agent.assert_called_once()
        self.assertEqual(fake.get_calls, 0)

    def test_shadow_executor_returns_without_durable_config_read(self):
        runtime = self.make_engine()
        configured = self.configured_agent()
        runtime.agent_configs = {configured["id"]: dict(configured)}
        runtime.models[configured["id"]] = SimpleNamespace(
            VERSION=12,
            model_revision="model-r1",
            heads={1: object()},
        )
        runtime.state_map = {
            configured["target_entity"]: {
                "entity_id": configured["target_entity"],
                "state": "off",
                "attributes": {},
                "context": {},
            }
        }
        runtime.entity_revisions = {configured["target_entity"]: 7}
        intent = ActionIntent.create(
            agent_id=configured["id"],
            target_entity=configured["target_entity"],
            target_property=configured["target_property"],
            desired_value=1.0,
            confidence=.9,
            support=.9,
            novelty=.1,
            prediction_horizon=1,
            policy_head=1,
            created_at=time.time(),
            ttl=2.0,
            policy_version=12,
            model_revision="model-r1",
            context_revision=runtime.context.home.revision,
            target_revision=7,
            teaching_id=0,
            teaching_revision=0,
            decision_source="historical_policy_bootstrap",
            reason="shadow test",
            experiment_token="",
            contributors=(),
            context_dependencies=(),
        )

        with patch.object(executor_module, "STORE", ExplodingStore()):
            result = runtime.executor.submit(intent)

        self.assertEqual(result["status"], "SHADOW")
        self.assertEqual(runtime.runtime[configured["id"]]["decision_state"], "shadow")

    def test_final_observation_wrapper_keeps_sql_off_agent_hot_path(self):
        source = (
            ROOT / "adaptive_ai/src/observation_contract.py"
        ).read_text(encoding="utf-8")
        submit = source.split("original_submit = engine.executor.submit", 1)[1].split(
            "original_process_agent = engine.process_agent", 1
        )[0]
        process = source.split("original_process_agent = engine.process_agent", 1)[1].split(
            "migrated = _migrate_models(core)", 1
        )[0]
        self.assertNotIn("store.get_agent_config", submit)
        self.assertNotIn("journal.open_window(", submit)
        self.assertIn("queue_window(", submit)
        self.assertNotIn("engine.provenance.event", process)
        self.assertNotIn("engine.policy(agent)", process)
        self.assertIn("queue_window(", process)

    def test_provenance_wrapper_uses_in_memory_target_origin(self):
        source = (
            ROOT / "adaptive_ai/src/provenance_runtime.py"
        ).read_text(encoding="utf-8")
        process = source.split("original_process_agent = engine.process_agent", 1)[1].split(
            "# --- Experiment idempotency", 1
        )[0]
        self.assertNotIn("journal.event(event_id)", process)
        self.assertIn("event_origin", process)

    def test_inactive_teach_rebenchmark_short_circuits_before_config_read(self):
        source = (
            ROOT / "adaptive_ai/src/teach_rl_rebenchmark.py"
        ).read_text(encoding="utf-8")
        before = source.split("def before_process(self, agent, state_map):", 1)[1].split(
            "def after_process(self, agent):", 1
        )[0]
        after = source.split("def after_process(self, agent):", 1)[1].split(
            "def install_teach_rl_rebenchmark", 1
        )[0]
        self.assertLess(before.index("if not self.active(agent)"), before.index("self.store.get_agent_config"))
        self.assertLess(after.index("if not self.active(agent)"), after.index("self.store.get_agent_config"))

    def test_candidate_empty_lineage_cache_is_invalidation_driven(self):
        source = (
            ROOT / "adaptive_ai/src/agent_candidate_shadow_runtime.py"
        ).read_text(encoding="utf-8")
        cached = source.split("def _cached_generations(root_id):", 1)[1].split(
            "def _decorate_result", 1
        )[0]
        self.assertNotIn("< 30.0", cached)
        self.assertIn('root_rt.get("shadow_generations") is not None', cached)
        self.assertIn("invalidate_generation_cache", source)

    def test_command_echo_matching_is_memory_only_after_journal_startup(self):
        source = (
            ROOT / "adaptive_ai/src/provenance.py"
        ).read_text(encoding="utf-8")
        match = source.split("def match_command_state(self, state):", 1)[1].split(
            "def experience_exists", 1
        )[0]
        self.assertNotIn("self.store.conn()", match)
        self.assertIn("_command_cache", match)
        self.assertIn("_load_active_command_cache()", source)

    def test_hot_status_includes_realtime_telemetry_snapshot(self):
        source = (
            ROOT / "adaptive_ai/src/release_017_ui_lifeline.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"telemetry": TELEMETRY.snapshot()', source)


if __name__ == "__main__":
    unittest.main()
