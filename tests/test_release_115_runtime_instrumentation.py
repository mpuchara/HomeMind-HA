from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"


class RuntimeInstrumentation115Tests(unittest.TestCase):
    def test_engine_carries_per_event_receive_timestamps(self):
        source = (SRC / "engine.py").read_text(encoding="utf-8")
        self.assertIn("self.entity_event_received_perf = {}", source)
        self.assertIn("self.entity_event_received_perf[entity_id] = received_perf", source)
        self.assertIn("event_received_perf = {", source)
        self.assertIn("self.process_target, target_agents, changed, snapshot, event_received_perf", source)
        self.assertIn("self._inference_tls.event_received_perf", source)
        self.assertIn('"event_to_intent_pass"', source)

    def test_full_agent_path_has_stage_markers(self):
        source = (SRC / "engine.py").read_text(encoding="utf-8")
        for stage in (
            '"pre_inference"',
            '"policy_context"',
            '"feature_construction"',
            '"policy_predict"',
            '"post_predict"',
            '"executor_submit"',
        ):
            self.assertIn(stage, source)
        self.assertIn('"inference_stage"', source)

    def test_correct_history_is_stage_traced(self):
        source = (SRC / "agent_correct_generation_history.py").read_text(encoding="utf-8")
        self.assertIn('RUNTIME_DEBUG.begin(', source)
        self.assertIn('"correct_history_stage"', source)
        for stage in (
            '"resolve_generation"',
            '"labels"',
            '"live_observed_history"',
            '"child_generation_history"',
            '"parent_generation_history"',
            '"current_history"',
        ):
            self.assertIn(stage, source)
        self.assertIn('TELEMETRY.observe("correct_history"', source)

    def test_live_endpoint_reports_snapshot_timing_without_changing_values(self):
        source = (SRC / "main.py").read_text(encoding="utf-8")
        self.assertIn('"snapshot_revision": snapshot_revision', source)
        self.assertIn('"snapshot_age_ms": snapshot_age_ms', source)
        self.assertIn('"config_lookup_ms": config_lookup_ms', source)
        self.assertIn('"build_ms": max(0.0, completed - live_started)', source)
        self.assertIn('"current_value": target_value(ENGINE.state_map.get', source)
        self.assertIn('"api_live"', source)

    def test_training_queue_keeps_bounded_lifecycle_trace(self):
        source = (SRC / "training_queue.py").read_text(encoding="utf-8")
        self.assertIn("self.recent_transitions = deque(maxlen=50)", source)
        self.assertIn('self._record_transition_locked("queued", job)', source)
        self.assertIn('"started"', source)
        self.assertIn('"worker_released"', source)
        self.assertIn('"recent_transitions": list(self.recent_transitions)', source)

    def test_release_version_remains_consistent_after_014115(self):
        import json
        version = json.loads(
            (ROOT / "adaptive_ai" / "BUILD_INFO.json").read_text(encoding="utf-8")
        )["version"]
        settings = (SRC / "settings.py").read_text(encoding="utf-8")
        config = (ROOT / "adaptive_ai" / "config.yaml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "adaptive_ai" / "Dockerfile").read_text(encoding="utf-8")
        index = (SRC / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'APP_VERSION = "{version}"', settings)
        self.assertIn(f'version: "{version}"', config)
        self.assertIn(f"ARG BUILD_VERSION={version}", dockerfile)
        self.assertIn(f"?v={version}", index)


if __name__ == "__main__":
    unittest.main()
