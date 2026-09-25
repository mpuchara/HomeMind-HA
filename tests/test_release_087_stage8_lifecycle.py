"""0.14.87 Stage-8 final lifecycle and stateful replay contracts."""
import json
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import history as history_module
import test_executor as executor_fixtures
from history import HistoryManager, stateful_continuation_seed_rows
from context import ExplicitFeatureSchema
from support import state
from learning_lifecycle import (
    correct_learning_path,
    training_request_decision,
)
from storage import Store
from training_queue import _queue_rebuild_reason


class FinalLearningLifecycleTests(unittest.TestCase):
    def test_compatible_trained_agent_stays_incremental_without_rebuild_reason(self):
        decision = training_request_decision(
            {
                "training_state": "qualified",
                "training_cursor_ts": 1234.0,
            },
            has_model=True,
        )
        self.assertFalse(decision["rebuild"])
        self.assertTrue(decision["resumed"])
        self.assertEqual(decision["learning_path"], "incremental_replay")
        self.assertIsNone(decision["rebuild_reason"])

    def test_schema_invalidated_agent_has_explicit_structural_rebuild_reason(self):
        decision = training_request_decision(
            {
                "training_state": "needs_retrain",
                "training_cursor_ts": 1234.0,
                "rebuild_reason": "feature_schema_change",
            },
            has_model=True,
        )
        self.assertTrue(decision["rebuild"])
        self.assertEqual(decision["learning_path"], "rebuild")
        self.assertEqual(decision["rebuild_reason"], "feature_schema_change")

    def test_manual_rebuild_is_explicit_and_initial_build_is_not_mislabelled(self):
        manual = training_request_decision(
            {"training_state": "qualified", "training_cursor_ts": 10.0},
            has_model=True,
            explicit_rebuild=True,
        )
        self.assertEqual(manual["rebuild_reason"], "explicit_manual_rebuild")

        initial = training_request_decision(
            {"training_state": "waiting", "training_cursor_ts": None},
            has_model=False,
        )
        self.assertEqual(initial["learning_path"], "initial_build")
        self.assertEqual(initial["rebuild_reason"], "initial_model_build")

    def test_config_invalidation_persists_structural_rebuild_reason(self):
        with tempfile.TemporaryDirectory(prefix="hm-stage8-lifecycle-") as root:
            store = Store(Path(root) / "lifecycle.db")
            agent = store.create_agent({
                "name": "Lifecycle",
                "target_entity": "switch.lifecycle",
                "target_property": "power",
                "min_value": 0, "max_value": 1,
                "deadband": .5, "exploration_step": 1,
                "confidence_threshold": .78, "action_interval": 30,
                "input_entities": ["binary_sensor.motion"],
            })
            store.save_model(agent["id"], {"version": 1, "value": "old"})
            store.update_agent(
                agent["id"],
                {"input_entities": ["binary_sensor.motion", "sensor.lux"]},
            )
            current = store.get_agent_config(agent["id"])
            self.assertEqual(
                current["benchmark_detail"]["rebuild_reason"],
                "feature_mask_change",
            )
            decision = training_request_decision(current, has_model=True)
            self.assertTrue(decision["rebuild"])
            self.assertEqual(
                decision["rebuild_reason"], "feature_mask_change"
            )

    def test_manual_correct_defaults_to_incremental_supervised_path(self):
        normal = correct_learning_path()
        self.assertFalse(normal["rebuild"])
        self.assertEqual(
            normal["learning_path"], "incremental_supervised_finetune"
        )
        self.assertIsNone(normal["rebuild_reason"])

        structural = correct_learning_path(
            structural_reason="feature_mask_change"
        )
        self.assertTrue(structural["rebuild"])
        self.assertEqual(
            structural["rebuild_reason"], "feature_mask_change"
        )

    def test_queue_preserves_explicit_reason_taxonomy(self):
        self.assertIsNone(
            _queue_rebuild_reason(False, "training", None)
        )
        self.assertEqual(
            _queue_rebuild_reason(True, "full_rebuild", None),
            "explicit_manual_rebuild",
        )
        self.assertEqual(
            _queue_rebuild_reason(True, "teach_rl", None),
            "feature_mask_change",
        )


class StatefulContinuationSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hm-stage8-seed-")
        self.store = Store(Path(self.temp.name) / "stage8.db")
        self.agent = {
            "id": "stage8-agent",
            "target_entity": "switch.stage8",
            "target_property": "power",
            "deadband": 0.5,
        }
        self.target_map = {"switch.stage8": [self.agent]}

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_matches_legacy_overlap_open_dwell_and_excludes_boundary(self):
        base = 1_700_000_000.0
        self.store.archive_batch([
            ("switch.stage8", base + 10, "off", {}, None, "test"),
            # Duplicate target state: legacy replay ignores it after last_value is set.
            ("switch.stage8", base + 20, "off", {}, None, "test"),
            ("switch.stage8", base + 30, "on", {}, None, "test"),
            ("switch.stage8", base + 40, "on", {}, None, "test"),
            # This is exactly at the continuation boundary and belongs to the new scan.
            ("switch.stage8", base + 50, "off", {}, None, "test"),
        ])

        seeds, scanned = stateful_continuation_seed_rows(
            self.store,
            [self.agent],
            self.target_map,
            base,
            base + 50,
        )
        row, value = seeds[self.agent["id"]]
        self.assertEqual(float(row["ts"]), base + 30)
        self.assertEqual(value, 1.0)
        self.assertEqual(scanned, 4)

        # Reference the old overlap algorithm: first row establishes last_value and each
        # effective target transition replaces the open pending dwell.
        legacy_value = None
        legacy_row = None
        for candidate in self.store.archive_iter(
            base, base + 50, ["switch.stage8"], chunk_size=256
        ):
            if float(candidate["ts"]) >= base + 50:
                continue
            value = 1.0 if candidate["state"] == "on" else 0.0
            if (
                legacy_value is not None
                and abs(value - legacy_value)
                    <= max(0.01, self.agent["deadband"] * 0.05)
            ):
                continue
            legacy_value = value
            legacy_row = candidate

        self.assertEqual(int(row["id"]), int(legacy_row["id"]))
        self.assertEqual(value, legacy_value)


class StatefulReplayEquivalenceTests(unittest.TestCase):
    @staticmethod
    def _run_path(stateful):
        fixture = executor_fixtures.ExecutorTests(methodName="test_control_dispatch")
        fixture.setUp()
        try:
            agent_id = fixture.a["id"]
            target = fixture.a["target_entity"]
            motion = "binary_sensor.motion"
            registry = {
                target: {"area_id": "stage8_room"},
                motion: {"area_id": "stage8_room"},
            }
            fixture.e.state_map[motion] = state(
                motion, "off", device_class="motion"
            )
            fixture.e.entity_registry = dict(registry)
            fixture.e.context.configure(
                fixture.e.state_map, entities=registry
            )
            fixture.model.schema = ExplicitFeatureSchema(
                fixture.model.dims, [motion]
            )
            fixture.store.save_model(agent_id, fixture.model.serialize())
            fixture.store.update_agent(agent_id, {"mode": "shadow"})

            base = 1_700_000_000.0
            target_edges = [
                (0.0, "off"),
                (1.0, "on"),
                (2.0, "off"),
                (4.0, "on"),
                (5.0, "off"),
                # Open dwell crossing the 6 h checkpoint:
                (5.5, "on"),
                (7.0, "off"),
                (8.5, "on"),
                (10.0, "off"),
                (11.5, "on"),
            ]
            rows = []
            for index, (hour, target_state) in enumerate(target_edges):
                ts = base + hour * 3600.0
                rows.append((
                    motion,
                    ts - 90.0,
                    "on" if index % 2 else "off",
                    {"device_class": "motion"},
                    None,
                    "stage8_equivalence",
                ))
                rows.append((
                    target, ts, target_state, {}, None,
                    "stage8_equivalence",
                ))
            fixture.store.archive_batch(rows)

            manager = HistoryManager(fixture.e, worker_mode=True)
            options = {
                **history_module.OPTIONS,
                "tiny_mlp_supervised_training_enabled": False,
                "training_cpu_duty_cycle": 1.0,
            }
            with patch.object(history_module, "OPTIONS", options), \
                 patch.object(
                     history_module.AUTOMATION_KNOWLEDGE,
                     "hints_for_target",
                     return_value=(set(), []),
                 ):
                first = manager.train_from_archive(
                    base, base + 6 * 3600.0,
                    qualify=False, agent_ids={agent_id},
                    benchmark=True, accumulate_benchmark=True,
                )
                second = manager.train_from_archive(
                    base + 3 * 3600.0, base + 12 * 3600.0,
                    qualify=False, agent_ids={agent_id},
                    benchmark=True, accumulate_benchmark=True,
                    continuation_from_ts=(
                        base + 6 * 3600.0 if stateful else None
                    ),
                )

            experiences = fixture.store.list_historical_experiences(
                agent_id, limit=10000
            )
            canonical_experiences = sorted(
                [
                    {
                        "target_history_id": int(row["target_history_id"]),
                        "action_index": int(row["action_index"]),
                        "action_value": float(row["action_value"]),
                        "reward": float(row["reward"]),
                        "dwell_seconds": float(row["dwell_seconds"]),
                        "features": {
                            str(k): float(v)
                            for k, v in sorted(row["features"].items())
                        },
                    }
                    for row in experiences
                ],
                key=lambda row: row["target_history_id"],
            )
            trained = fixture.store.get_agent_config(agent_id)
            model = fixture.store.get_model(agent_id) or {}
            counts = dict(model.get("_benchmark_counts") or {})
            policy = fixture.e.policy(trained)
            probe_features = [
                row["features"] for row in canonical_experiences[-4:]
            ]
            predictions = [
                float(policy.predict(features)[0]["value"])
                for features in probe_features
            ]
            return {
                "new_count": int(first) + int(second),
                "experiences": canonical_experiences,
                "benchmark_counts": json.loads(
                    json.dumps(counts, sort_keys=True)
                ),
                "benchmark_score": trained.get("benchmark_score"),
                "benchmark_samples": int(
                    trained.get("benchmark_samples") or 0
                ),
                "predictions": predictions,
            }
        finally:
            fixture.tearDown()

    def test_stateful_continuation_matches_legacy_overlap_training_semantics(self):
        legacy = self._run_path(False)
        stateful_result = self._run_path(True)

        self.assertEqual(
            stateful_result["new_count"], legacy["new_count"]
        )
        self.assertEqual(
            stateful_result["experiences"], legacy["experiences"]
        )
        self.assertEqual(
            stateful_result["benchmark_counts"],
            legacy["benchmark_counts"],
        )
        self.assertEqual(
            stateful_result["benchmark_samples"],
            legacy["benchmark_samples"],
        )
        self.assertEqual(
            stateful_result["benchmark_score"],
            legacy["benchmark_score"],
        )
        self.assertEqual(
            stateful_result["predictions"], legacy["predictions"]
        )


class StatefulChunkSchedulingTests(unittest.TestCase):
    def test_later_chunks_keep_logical_overlap_but_scan_from_saved_cursor(self):
        manager = HistoryManager.__new__(HistoryManager)
        manager.stop_event = threading.Event()
        manager.temporal_replay_stats = {}
        manager.training_stateful_replay_status = {}
        manager.engine = SimpleNamespace(wake_event=threading.Event())
        manager._training_bounds = lambda: (0.0, 18 * 3600.0)
        manager._refresh_agent_history = lambda *args, **kwargs: None

        calls = []

        def run_chunk(start_ts, end_ts, **kwargs):
            calls.append((float(start_ts), float(end_ts), dict(kwargs)))
            manager.temporal_replay_stats = {
                "continuation": {
                    "seed_target_rows_scanned": 2
                    if kwargs.get("continuation_from_ts") is not None else 0,
                    "seed_agents": 1
                    if kwargs.get("continuation_from_ts") is not None else 0,
                }
            }
            return 0

        manager._run_training_chunk = run_chunk

        agent = {
            "id": "stage8-agent",
            "name": "Stage 8",
            "training_window_start_ts": None,
            "training_cursor_ts": None,
            "training_window_end_ts": None,
            "training_state": "waiting",
            "benchmark_score": None,
            "benchmark_samples": 0,
            "benchmark_source": None,
            "benchmark_detail": {},
        }

        class FakeStore:
            def get_agent_config(self, agent_id):
                return dict(agent)

            def set_training_progress(self, *args, **kwargs):
                return None

            def event(self, *args, **kwargs):
                return None

            def set_training_state(self, *args, **kwargs):
                return None

        options = {
            **history_module.OPTIONS,
            "agent_training_chunk_hours": 6,
            "agent_training_overlap_hours": 6,
            "agent_training_stateful_continuation": True,
            "agent_training_pause_ms": 0,
        }
        with patch.object(history_module, "STORE", FakeStore()), \
             patch.object(history_module, "OPTIONS", options), \
             patch.object(history_module, "now_ts", return_value=18 * 3600.0):
            manager._run_agent_indexing(
                agent["id"],
                rebuild=True,
                rebuild_reason="explicit_manual_rebuild",
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual((calls[0][0], calls[0][1]), (0.0, 6 * 3600.0))
        self.assertIsNone(calls[0][2]["continuation_from_ts"])

        # Existing semantics retain a 3 h logical overlap (50% chunk cap), but physical
        # scanning begins at the already committed 6 h / 12 h cursor.
        self.assertEqual(
            (calls[1][0], calls[1][1], calls[1][2]["continuation_from_ts"]),
            (3 * 3600.0, 12 * 3600.0, 6 * 3600.0),
        )
        self.assertEqual(
            (calls[2][0], calls[2][1], calls[2][2]["continuation_from_ts"]),
            (9 * 3600.0, 18 * 3600.0, 12 * 3600.0),
        )
        summary = manager.training_stateful_replay_status
        self.assertEqual(summary["contract"], "stateful_chunk_continuation_v1")
        self.assertAlmostEqual(summary["logical_hours"], 24.0)
        self.assertAlmostEqual(summary["unique_hours_scanned"], 18.0)
        self.assertAlmostEqual(summary["overlap_hours_avoided"], 6.0)
        self.assertEqual(summary["continuation_seed_target_rows"], 4)
        self.assertEqual(summary["continuation_seed_agents"], 2)


class Stage8TransportSourceContracts(unittest.TestCase):
    def test_isolated_worker_receives_continuation_cursor(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "adaptive_ai" / "src" / "training_process.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"continuation_from_ts": kwargs.get("continuation_from_ts")',
            source,
        )

    def test_history_reports_explicit_rebuild_and_stateful_replay_diagnostics(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "adaptive_ai" / "src" / "history.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"training_rebuild_reason": self.training_rebuild_reason', source)
        self.assertIn('"stateful_replay": dict(self.training_stateful_replay_status', source)
        self.assertIn('"contract": "stateful_chunk_continuation_v1"', source)


if __name__ == "__main__":
    unittest.main()
