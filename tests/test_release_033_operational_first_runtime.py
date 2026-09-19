import inspect
import threading
import time
import unittest

from support import *
from history import HistoryManager
from support import ROOT
import release_016_guard
import release_017_ui_lifeline


class OperationalFirstRuntimeTests(unittest.TestCase):
    def test_manual_discovery_request_runs_outside_request_thread_and_is_single_flight(self):
        manager = object.__new__(HistoryManager)
        manager.discovery_job_lock = threading.RLock()
        manager.discovery_job_active = False
        manager.discovery_job_started_at = None
        manager.error = None
        started = threading.Event()
        release = threading.Event()

        def fake_bootstrap():
            started.set()
            release.wait(2.0)

        manager.bootstrap_and_train = fake_bootstrap
        manager.set_status = lambda *a, **kw: None

        self.assertTrue(manager.request_discovery_rescan())
        self.assertTrue(started.wait(1.0))
        self.assertTrue(manager.discovery_job_active)
        self.assertFalse(manager.request_discovery_rescan())

        release.set()
        deadline = time.time() + 2.0
        while manager.discovery_job_active and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(manager.discovery_job_active)

    def test_quiet_start_never_schedules_periodic_recorder_bootstrap(self):
        source = inspect.getsource(release_016_guard.install)
        self.assertNotIn("original_bootstrap(history_self)", source)
        self.assertIn("request_discovery_rescan", source)
        self.assertIn("Operational-first runtime", source)

    def test_rescan_route_is_async_and_does_not_run_discovery_in_http_handler(self):
        source = (ROOT / "adaptive_ai/src/main.py").read_text(encoding="utf-8")
        marker = 'if path == "/api/discovery/rescan":'
        self.assertIn(marker, source)
        block = source[source.index(marker):]
        block = block[:block.index('if path.startswith("/api/agents/") and path.endswith("/train")')]
        self.assertIn("request_discovery_rescan", block)
        self.assertNotIn("auto_discover_agents", block)
        self.assertNotIn("AUTOMATION_KNOWLEDGE.scan", block)
        self.assertIn("202", block)

    def test_periodic_agent_ui_path_does_not_call_rich_agent_aggregates(self):
        source = inspect.getsource(release_017_ui_lifeline.install)
        hot_start = source.index("def hot_agent_payloads")
        hot_end = source.index("queue_runtime._agent_payloads = hot_agent_payloads")
        hot = source[hot_start:hot_end]
        self.assertIn("configs = hot_configs()", hot)
        self.assertNotIn("list_agent_configs", hot)
        self.assertNotIn("original_agent_payloads(handler_self)", hot)

    def test_periodic_status_path_does_not_call_engine_status(self):
        source = inspect.getsource(release_017_ui_lifeline.install)
        status_start = source.index("def status_payload")
        status_end = source.index("core.Handler.status_payload = status_payload")
        status = source[status_start:status_end]
        self.assertIn("configs = hot_configs()", status)
        self.assertNotIn("list_agent_configs", status)
        self.assertNotIn("ENGINE.status(", status)
        self.assertIn('"status_read_mode": "operational_hot"', status)


if __name__ == "__main__":
    unittest.main()
