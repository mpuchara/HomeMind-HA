"""0.14.130 fast full-training regressions."""
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import history as history_module


class PersistentNeuralTrainingBufferTests(unittest.TestCase):
    def manager(self):
        manager = history_module.HistoryManager.__new__(
            history_module.HistoryManager
        )
        manager._persistent_neural_sample_state = {}
        return manager

    def test_same_mask_reuses_one_bounded_training_buffer(self):
        manager = self.manager()
        identity = ("schema-a", "mask-a", ("f1", "f2"), (0.0, 1.0), (1,), 3)

        first = manager._persistent_neural_train_buffer(
            "agent-a", identity, 3
        )
        first.extend([1, 2, 3])
        second = manager._persistent_neural_train_buffer(
            "agent-a", identity, 3
        )
        second.append(4)

        self.assertIs(first, second)
        self.assertEqual(list(second), [2, 3, 4])
        self.assertEqual(second.maxlen, 3)

    def test_mask_change_resets_training_buffer(self):
        manager = self.manager()
        first = manager._persistent_neural_train_buffer(
            "agent-a", ("schema-a", "mask-a"), 4
        )
        first.extend([1, 2])

        second = manager._persistent_neural_train_buffer(
            "agent-a", ("schema-a", "mask-b"), 4
        )

        self.assertIsNot(first, second)
        self.assertEqual(list(second), [])
        self.assertEqual(second.maxlen, 4)

    def test_close_releases_neural_training_rows(self):
        manager = self.manager()
        manager._persistent_neural_train_buffer(
            "agent-a", ("schema-a", "mask-a"), 4
        ).append({"sample": 1})
        manager._persistent_replay_sqlite_connection = None

        manager.close_persistent_training_resources()

        self.assertEqual(manager._persistent_neural_sample_state, {})


class DurableRecorderCoverageTests(unittest.TestCase):
    def manager(self):
        manager = history_module.HistoryManager.__new__(
            history_module.HistoryManager
        )
        manager.training_recorder_coverage = {}
        manager.training_recorder_coverage_hits = 0
        manager.training_recorder_coverage_misses = 0
        manager.recorder_skipped_slices = 0
        manager.progress = 0.0
        return manager

    def test_coverage_round_trip_is_versioned_and_filters_invalid_rows(self):
        payload = json.dumps({
            "contract": history_module.HistoryManager.RECORDER_COVERAGE_META_KEY,
            "rows": [
                {
                    "source": "ha_history_full",
                    "entity_id": "light.test",
                    "start_ts": 100.0,
                    "end_ts": 200.0,
                },
                {
                    "source": "ha_history_full",
                    "entity_id": "light.invalid",
                    "start_ts": 300.0,
                    "end_ts": 200.0,
                },
            ],
        })

        decoded = history_module.HistoryManager._decode_training_recorder_coverage(
            payload
        )

        self.assertEqual(
            decoded,
            {("ha_history_full", "light.test"): (100.0, 200.0)},
        )
        self.assertEqual(
            history_module.HistoryManager._decode_training_recorder_coverage(
                '{"contract":"wrong","rows":[]}'
            ),
            {},
        )

    def test_successful_coverage_is_persisted_but_meta_failure_is_nonfatal(self):
        manager = self.manager()
        manager.training_recorder_coverage = {
            ("ha_history_full", "light.test"): (100.0, 200.0)
        }

        with patch.object(history_module.STORE, "meta_set") as meta_set:
            self.assertTrue(manager._persist_training_recorder_coverage())
        raw = meta_set.call_args.args[1]
        self.assertEqual(
            manager._decode_training_recorder_coverage(raw),
            manager.training_recorder_coverage,
        )

        with patch.object(
            history_module.STORE, "meta_set", side_effect=RuntimeError("disk")
        ):
            self.assertFalse(manager._persist_training_recorder_coverage())

    def test_restored_coverage_fetches_only_overlap_and_new_tail(self):
        manager = self.manager()
        manager.training_recorder_coverage = {
            ("ha_history_full", "light.test"): (0.0, 7200.0)
        }
        manager._import_section = MagicMock(return_value=7)
        manager._persist_training_recorder_coverage = MagicMock(return_value=True)

        with patch.dict(
            history_module.OPTIONS,
            {
                "training_recorder_refresh_overlap_minutes": 30,
                "agent_training_pause_ms": 0,
            },
            clear=False,
        ):
            inserted = manager._training_recorder_import(
                ["light.test"],
                0.0,
                10800.0,
                batch_size=1,
                minimal=False,
                no_attributes=False,
                source="ha_history_full",
                label="target",
                max_hours=6,
            )

        self.assertEqual(inserted, 7)
        self.assertEqual(manager.training_recorder_coverage_hits, 1)
        self.assertEqual(manager.training_recorder_coverage_misses, 0)
        args = manager._import_section.call_args.args
        self.assertEqual(args[0], ["light.test"])
        self.assertEqual(args[1], 5400.0)
        self.assertEqual(args[2], 10800.0)
        manager._persist_training_recorder_coverage.assert_called_once()
        self.assertEqual(
            manager.training_recorder_coverage[
                ("ha_history_full", "light.test")
            ],
            (0.0, 10800.0),
        )


class DeferredNeuralFinalizationSourceContractTests(unittest.TestCase):
    def test_persistent_sequence_finalizes_neural_only_on_last_chunk(self):
        root = Path(__file__).resolve().parents[1]
        history = (root / "adaptive_ai" / "src" / "history.py").read_text(
            encoding="utf-8"
        )
        process = (
            root / "adaptive_ai" / "src" / "training_process.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'chunk["train_kwargs"]["finalize_neural"] = (',
            history,
        )
        self.assertIn("if neural_enabled and finalize_neural:", history)
        self.assertIn("_persistent_neural_train_buffer(", history)
        self.assertIn(
            '"finalize_neural": bool(kwargs.get("finalize_neural", True))',
            process,
        )

    def test_ridge_checkpoint_and_benchmark_persistence_remain_per_chunk(self):
        root = Path(__file__).resolve().parents[1]
        history = (root / "adaptive_ai" / "src" / "history.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('STORE.save_model(agent["id"], exported)', history)
        self.assertIn("STORE.set_partial_benchmark(", history)
        self.assertIn('TRAINING_BUDGET.checkpoint("after_model_save")', history)


if __name__ == "__main__":
    unittest.main()
