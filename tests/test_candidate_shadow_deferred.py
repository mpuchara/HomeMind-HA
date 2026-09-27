import threading
import unittest
from types import SimpleNamespace

from agent_candidates import AgentCandidateManager
from candidate_shadow_deferred import DeferredCandidateShadowQueue
from inference_hot_path_metrics import InferenceHotPathMetrics


class Wake:
    def __init__(self):
        self.calls = 0
    def set(self):
        self.calls += 1


class Home:
    def __init__(self):
        self.calls = 0
    def forecast(self, target, timestamp):
        self.calls += 1
        return {"known": True, "occupancy_now": .99}


class CandidateShadowDeferredTests(unittest.TestCase):
    def setUp(self):
        self.base_home = Home()
        self.temporal = SimpleNamespace(home_context=self.base_home)
        self.engine = SimpleNamespace(
            temporal_history=self.temporal,
            _inference_tls=threading.local(),
            inference_hot_path_metrics=InferenceHotPathMetrics(),
        )
        self.store = SimpleNamespace(
            _provenance_generation_revision=7,
            event=lambda *args, **kwargs: None,
        )
        self.wake = Wake()
        self.manager = SimpleNamespace(
            engine=self.engine,
            store=self.store,
            wake_event=self.wake,
        )
        self.executed = []
        self.queue = DeferredCandidateShadowQueue(self.manager, limit=4)
        self.manager.candidate_shadow_temporal = self.queue.current_temporal
        self.manager.candidate_shadow_home_provider = self.queue.current_home_provider
        self.manager.candidate_shadow_current_job = self.queue.current_job

        def execute(job):
            tls = self.engine._inference_tls
            temporal = self.queue.current_temporal()
            home = self.queue.current_home_provider()
            self.executed.append({
                "root": job["root_agent_id"],
                "state_revision": getattr(tls, "state_revision", None),
                "forecast": home.forecast("light.x", job["context_ts"]) if home else None,
                "job": self.queue.current_job(),
                "temporal_is_proxy": temporal is not self.temporal,
            })
            return {"ok": True}

        self.manager.execute_candidate_shadow_job = execute

    @staticmethod
    def job(root="a", revision=1, context_ts=10.0, generation_revision=7):
        return {
            "root_agent_id": root,
            "state_map": {"light.x": {"state": "off"}},
            "context_ts": float(context_ts),
            "inference_ts": float(context_ts),
            "state_revision": int(revision),
            "entity_revisions": {"light.x": int(revision)},
            "context_revision": int(revision),
            "generation_revision": int(generation_revision),
            "home_forecast_captured": True,
            "home_forecast": {"known": True, "occupancy_now": .25},
            "parent_observation": {
                "desired": 0.0,
                "confidence": .9,
                "model_revision": "parent",
                "schema_revision": "12",
            },
        }

    def test_enqueue_never_executes_candidate_until_worker_drain(self):
        receipt = self.queue.enqueue(self.job())
        self.assertTrue(receipt["deferred"])
        self.assertEqual(self.executed, [])
        self.assertEqual(self.queue.diagnostics()["queue_depth"], 1)

        self.assertEqual(self.queue.drain(max_roots=1), 1)
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.executed[0]["state_revision"], 1)
        self.assertEqual(self.executed[0]["forecast"]["occupancy_now"], .25)
        self.assertTrue(self.executed[0]["temporal_is_proxy"])
        self.assertEqual(self.base_home.calls, 0)

    def test_latest_job_per_root_coalesces_and_exact_duplicate_deduplicates(self):
        self.queue.enqueue(self.job(revision=1, context_ts=10.0))
        self.queue.enqueue(self.job(revision=2, context_ts=11.0))
        duplicate = self.queue.enqueue(self.job(revision=2, context_ts=11.0))
        diag = self.queue.diagnostics()
        self.assertEqual(diag["queue_depth"], 1)
        self.assertEqual(diag["coalesced"], 1)
        self.assertEqual(diag["deduplicated"], 1)
        self.assertTrue(duplicate["deduplicated"])

        self.queue.drain(max_roots=1)
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.executed[0]["state_revision"], 2)

    def test_queue_is_bounded_and_drops_oldest_root(self):
        for idx in range(5):
            self.queue.enqueue(self.job(root=f"root-{idx}", revision=idx + 1))
        diag = self.queue.diagnostics()
        self.assertEqual(diag["queue_depth"], 4)
        self.assertEqual(diag["dropped"], 1)
        self.assertNotIn("root-0", diag["pending_roots"])

    def test_lineage_revision_change_keeps_gap_instead_of_mismatched_inference(self):
        self.queue.enqueue(self.job(generation_revision=7))
        self.store._provenance_generation_revision = 8
        self.assertEqual(self.queue.drain(max_roots=1), 1)
        self.assertEqual(self.executed, [])
        diag = self.queue.diagnostics()
        self.assertEqual(diag["stale_generation_dropped"], 1)

    def test_process_wrapper_returns_without_running_candidate_backend(self):
        engine = SimpleNamespace(
            process_agent=lambda *args, **kwargs: {"live": True},
            inference_hot_path_metrics=InferenceHotPathMetrics(),
        )
        manager = SimpleNamespace(
            engine=engine,
            before_live_process=lambda *args, **kwargs: None,
            after_live_process=lambda *args, **kwargs: self.queue.enqueue(self.job()),
        )
        AgentCandidateManager._install_process_wrapper(manager)

        result = engine.process_agent({"id": "root"}, {}, {"sensor.x"})
        self.assertEqual(result, {"live": True})
        self.assertEqual(self.executed, [])
        self.assertEqual(self.queue.diagnostics()["queue_depth"], 1)


if __name__ == "__main__":
    unittest.main()
