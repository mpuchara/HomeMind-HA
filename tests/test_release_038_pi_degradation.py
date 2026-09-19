"""0.14.38 regressions from the long-running Raspberry Pi degradation."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from support import ROOT
from storage import Store
from episode_evaluator import EpisodeEvaluator
from cold_start_drift import AdaptationService


class _Engine:
    def __init__(self):
        self.state_map = {}
        self.models = {}
        self.runtime = {}
        self.lock = threading.RLock()


class _Manager:
    def __init__(self, store):
        self.store = store
        self.engine = _Engine()


class Release038PiDegradationTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / "adaptive_ai" / "src" / name).read_text(encoding="utf-8")

    def test_drift_observer_is_not_synchronous_inference_work(self):
        source = self.source("cold_start_drift.py")
        install = source.split("def install(manager):", 1)[1]
        after = install.split("def after_live_process", 1)[1].split("def promote", 1)[0]
        self.assertIn("service.schedule_observe(agent)", after)
        self.assertNotIn("service.observe_live(agent", after)
        self.assertIn('name="adaptive-ai-drift-observer"', source)
        self.assertIn("OBSERVE_THROTTLE_SECONDS", source)

    def test_drift_ingest_reads_only_missing_episode_suffix_in_one_batch(self):
        source = self.source("cold_start_drift.py")
        block = source.split("def ingest_episode_evaluator", 1)[1].split(
            "def record_environment", 1
        )[0]
        self.assertIn("LEFT JOIN adaptation_episode_observations", block)
        self.assertIn("a.episode_id IS NULL", block)
        self.assertIn("LIMIT ?", block)
        self.assertIn("c.executemany(", block)
        self.assertNotIn("self.record_episode(", block)

    def test_drift_detection_queries_are_bounded(self):
        source = self.source("cold_start_drift.py")
        self.assertIn(
            "limit=BASELINE_EPISODES + RECENT_EPISODES, quality_only=True",
            source,
        )
        self.assertIn(
            "rows = self._env_rows(agent_id, limit=ENV_STABLE_SNAPSHOTS * 2)",
            source,
        )
        self.assertIn("limit=POST_PROMOTION_EPISODES", source)

    def test_episode_ingestion_is_idempotent_and_incremental(self):
        with tempfile.TemporaryDirectory(prefix="release-038-") as tmp:
            store = Store(Path(tmp) / "test.db")
            EpisodeEvaluator(store)
            manager = _Manager(store)
            service = AdaptationService(manager)
            aid = "agent-a"
            now = 1000.0
            with store.lock, store.conn() as c:
                for idx in range(3):
                    eid = f"episode-{idx}"
                    c.execute(
                        """INSERT INTO episode_evaluator_episodes
                           (episode_id,contract_version,domain,agent_id,start_ts,end_ts,
                            context_json,labels_json,observability_json,automation_replay_json,
                            physical_outcome_json,end_reason,fingerprint,created_ts)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            eid, 1, "light_power", aid, now + idx, now + idx + 0.5,
                            "{}", "{}", "{}", None, None, "test", f"fp-{idx}", now,
                        ),
                    )
                    c.execute(
                        """INSERT INTO episode_evaluator_policy_results
                           (episode_id,policy_key,role,executed,counterfactual,metrics_json,evidence_json)
                           VALUES(?,?,'live',1,0,?,?)""",
                        (
                            eid, f"live-{idx}",
                            json.dumps({"meaningful": True, "manual_correction_count": 0}),
                            "{}",
                        ),
                    )
            self.assertEqual(service.ingest_episode_evaluator(aid), 3)
            self.assertEqual(service.ingest_episode_evaluator(aid), 0)
            with store.conn() as c:
                count = c.execute(
                    "SELECT COUNT(*) FROM adaptation_episode_observations WHERE agent_id=?",
                    (aid,),
                ).fetchone()[0]
            self.assertEqual(count, 3)

    def test_live_cards_use_ram_agent_config_cache(self):
        source = self.source("agent_live_card_refresh.py")
        body = source.split("def live_agent_payload", 1)[1].split("def install", 1)[0]
        self.assertIn("core.ENGINE._refresh_agent_index()", body)
        self.assertIn("core.ENGINE.all_agent_configs.values()", body)
        self.assertNotIn("STORE.list_agent_configs()", body)

    def test_healthy_websocket_uses_slow_full_state_resync(self):
        engine = self.source("engine.py")
        settings = self.source("settings.py")
        self.assertIn('ping_interval=20', engine)
        self.assertIn('ping_timeout=20', engine)
        self.assertIn('OPTIONS.get("realtime_resync_seconds", 300)', engine)
        self.assertIn('"realtime_resync_seconds": 300', settings)
        self.assertIn("self.engine.last_full_poll = 0.0", engine)

    def test_websocket_outage_fallback_is_fast_but_resync_processing_is_delta_only(self):
        engine = self.source("engine.py")
        settings = self.source("settings.py")
        self.assertIn('OPTIONS.get("realtime_fallback_poll_seconds", 10)', engine)
        self.assertIn('"realtime_fallback_poll_seconds": 10', settings)
        self.assertIn("changed_eids = {", engine)
        self.assertIn(
            "process_eids = set(state_map) if initial else changed_eids",
            engine,
        )
        self.assertIn(
            "topology_changed = initial or set(previous) != set(state_map)",
            engine,
        )
        self.assertNotIn("for st in state_map.values():\n            ts =", engine)

    def test_ha_status_uses_dedicated_state_sync_health(self):
        engine = self.source("engine.py")
        lifeline = self.source("release_017_ui_lifeline.py")
        self.assertIn("self.last_state_sync_ok", engine)
        self.assertIn("self.last_state_sync_error", engine)
        self.assertIn('"state_resync": {', engine)
        self.assertIn('last_state_sync_ok = getattr(core.ENGINE, "last_state_sync_ok", None)', lifeline)
        self.assertIn("last_state_sync_error is None", lifeline)
        self.assertNotIn('getattr(ha_client, "last_error", None)', lifeline)

    def test_drift_observer_does_not_resignal_same_throttled_agent(self):
        source = self.source("cold_start_drift.py")
        block = source.split("def schedule_observe", 1)[1].split(
            "def observer_snapshot", 1
        )[0]
        self.assertIn("if not existed:", block)
        self.assertIn("self._observer_event.set()", block)

    def test_frontend_has_one_owner_for_full_agent_and_candidate_reads(self):
        candidate = (ROOT / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        preference = (ROOT / "adaptive_ai/src/static/candidate_preference_ui.js").read_text(encoding="utf-8")
        confidence = (ROOT / "adaptive_ai/src/static/confidence_contract_ui.js").read_text(encoding="utf-8")
        manual = (ROOT / "adaptive_ai/src/static/manual_feedback.js").read_text(encoding="utf-8")
        app = (ROOT / "adaptive_ai/src/static/app.js").read_text(encoding="utf-8")
        self.assertIn("adaptive-ai:candidates", candidate)
        self.assertNotIn("setInterval(refresh,1500)", preference)
        self.assertNotIn("fetch('api/candidates'", preference)
        self.assertNotIn("fetch('api/agents'", confidence)
        self.assertNotIn("fetch('api/candidates'", confidence)
        self.assertNotIn("setInterval(refresh,2000)", confidence)
        self.assertIn("setTimeout(liveLoop,1000)", manual)
        self.assertIn("adaptiveAiTimeoutMs:3500", app)
        self.assertIn("const earlyAgents=wasReady?api('api/agents'):null", app)
        # A clean install must not render "0 active" before the Recorder classifier has
        # actually run, and duplicate Rescan clicks are locked/idempotent.
        self.assertIn("function discoveryPending(h)", app)
        self.assertIn("Activity classification has not run yet", app)
        self.assertIn("b.disabled=running", app)
        self.assertIn("manual_ready:'fast_targets'", app)
        main = self.source("main.py")
        history = self.source("history.py")
        self.assertIn('"already_running": True', main)
        self.assertIn('"discovery_classified": bool(self.discovery_classified)', history)

    def test_visible_diagnostics_expose_cpu_scheduler_and_drift_runtime(self):
        home = (ROOT / "adaptive_ai" / "src" / "static" / "home.js").read_text(encoding="utf-8")
        self.assertIn("cpu_percent_recent", home)
        self.assertIn("scheduler.event_passes", home)
        self.assertIn("drift.max_run_ms", home)
        self.assertIn("resync.last_changed_entities", home)
        self.assertIn("resync.max_duration_ms", home)


if __name__ == "__main__":
    unittest.main()
