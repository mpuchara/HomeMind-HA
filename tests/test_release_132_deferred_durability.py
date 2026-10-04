"""0.14.132 deferred persistence and shutdown durability regressions."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from provenance import ProvenanceJournal
from provenance_runtime import install as install_provenance
from storage import Store
from support import state
import test_executor as executor_fixture


ROOT = Path(__file__).resolve().parents[1]


class ProvenanceOverflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        self.journal = ProvenanceJournal(self.store, clock=lambda: 1000.0)

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_event_overflow_preserves_every_event_when_sync_flush_fails(self):
        self.journal._pending_event_limit = 2
        original = self.journal.flush_events_batch
        self.journal.flush_events_batch = Mock(side_effect=RuntimeError("sqlite busy"))
        try:
            for index in range(3):
                event_id, inserted = self.journal.record_event(
                    "light.kitchen",
                    state("light.kitchen", "on" if index % 2 else "off"),
                    event_time=100.0 + index,
                    received_time=100.0 + index,
                    source="ha_state_changed",
                    origin="own_command",
                    event_id=f"event-{index}",
                )
                self.assertTrue(inserted)
                self.assertEqual(event_id, f"event-{index}")
        finally:
            self.journal.flush_events_batch = original

        self.assertEqual(self.journal.pending_event_count(), 3)
        self.assertEqual(self.journal._dropped_pending_events, 0)
        self.assertEqual(self.journal._pending_event_overflow_sync, 1)
        self.assertEqual(self.journal._pending_event_overflow_errors, 1)
        self.assertGreaterEqual(self.journal._max_pending_events, 3)
        for index in range(3):
            self.assertEqual(
                self.journal.event(f"event-{index}")["origin"], "own_command"
            )

        self.assertEqual(self.journal.flush_events_batch(10), 3)
        self.assertEqual(self.journal.pending_event_count(), 0)
        fresh = ProvenanceJournal(self.store, clock=lambda: 2000.0)
        for index in range(3):
            self.assertEqual(fresh.event(f"event-{index}")["origin"], "own_command")


class DeferredDecisionRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = executor_fixture.ExecutorTests()
        self.fixture.setUp()
        self.core = SimpleNamespace(STORE=self.fixture.store, ENGINE=self.fixture.e)
        install_provenance(self.core)

    def tearDown(self):
        self.fixture.e.stop_event.set()
        event = getattr(self.fixture.e, "provenance_writer_event", None)
        if event is not None:
            event.set()
        thread = getattr(self.fixture.e, "provenance_writer_thread", None)
        if thread is not None:
            thread.join(timeout=1.0)
        self.fixture.tearDown()

    def test_failed_shadow_batch_flush_is_requeued_and_retryable(self):
        f = self.fixture
        f.store.update_agent(f.a["id"], {"mode": "shadow"})
        shadow = f.store.get_agent_config(f.a["id"])
        f.e.agent_configs = {f.a["id"]: dict(shadow)}

        result = f.e.executor.submit(f.intent(), {0: 1.0}, 1)
        self.assertEqual(result["status"], "SHADOW")
        before = f.e.provenance_deferred_snapshot()
        self.assertGreaterEqual(before["decisions"]["pending"], 1)

        original = f.e.provenance.record_decisions_batch
        f.e.provenance.record_decisions_batch = Mock(
            side_effect=RuntimeError("transient sqlite failure")
        )
        try:
            with self.assertRaises(RuntimeError):
                f.store._flush_provenance_decisions()
        finally:
            f.e.provenance.record_decisions_batch = original

        failed = f.e.provenance_deferred_snapshot()
        self.assertEqual(failed["decisions"]["pending"], before["decisions"]["pending"])
        self.assertGreaterEqual(failed["decisions"]["errors"], 1)

        flushed = f.store._flush_provenance_decisions()
        self.assertGreaterEqual(flushed, 1)
        after = f.e.provenance_deferred_snapshot()
        self.assertEqual(after["decisions"]["pending"], 0)


class ShutdownContractTests(unittest.TestCase):
    def test_deferred_writer_hooks_are_exposed_for_explicit_shutdown_drain(self):
        provenance = (ROOT / "adaptive_ai/src/provenance_runtime.py").read_text()
        observation = (ROOT / "adaptive_ai/src/observation_contract.py").read_text()
        self.assertIn("store._flush_all_provenance = flush_all_provenance", provenance)
        self.assertIn("engine.provenance_writer_thread = writer_thread", provenance)
        self.assertIn("engine.provenance_writer_event = deferred_event", provenance)
        self.assertIn("engine.flush_feature_journal = flush_feature_journal", observation)
        self.assertIn("engine.feature_journal_writer_thread = writer_thread", observation)
        self.assertIn("engine.feature_journal_writer_event = journal_event", observation)

    def test_shutdown_stops_producers_before_final_durability_barrier(self):
        source = (ROOT / "adaptive_ai/src/main.py").read_text()
        start = source.index("def shutdown_runtime():")
        end = source.index("\ndef run_initialize_runtime", start)
        shutdown = source[start:end]
        self.assertLess(
            shutdown.index("EVENT_STREAM.stop_event.set()"),
            shutdown.index("flush_all_provenance"),
        )
        self.assertLess(
            shutdown.index("ENGINE.stop_event.set()"),
            shutdown.index("flush_all_provenance"),
        )
        self.assertIn("EVENT_STREAM.join(timeout=6.0)", shutdown)
        self.assertIn("shutdown(wait=True, cancel_futures=True)", shutdown)
        self.assertIn("thread.join(timeout=2.5)", shutdown)
        self.assertIn('getattr(ENGINE, "flush_feature_journal", None)', shutdown)


if __name__ == "__main__":
    unittest.main()
