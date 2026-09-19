"""0.14.37 regressions from first 0.14.36 Raspberry Pi run."""
import unittest
from pathlib import Path

from support import ROOT


class Release037PiRuntimeRegressionTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / "adaptive_ai" / "src" / name).read_text(encoding="utf-8")

    def test_candidate_poll_loop_is_executable_not_hidden_in_line_comment(self):
        source = (ROOT / "adaptive_ai" / "src" / "static" / "candidate_ui.js").read_text(encoding="utf-8")
        self.assertNotIn(r"cadence\n  //", source)
        self.assertRegex(source, r"(?m)^\s*async function loop\(\)\{await refresh\(\);setTimeout\(loop,4000\);\}\s*$")
        self.assertRegex(source, r"(?m)^\s*loop\(\);\s*$")

    def test_agent_and_event_ui_reads_fail_independently(self):
        source = (ROOT / "adaptive_ai" / "src" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Promise.allSettled", source)
        self.assertIn("if(agentsResult.status==='fulfilled')", source)
        self.assertIn("if(eventsResult.status==='fulfilled')", source)
        self.assertNotIn("const [agents,events]=await Promise.all(", source)

    def test_event_feed_has_dedicated_ram_lock(self):
        source = self.source("storage.py")
        self.assertIn("self._event_lock = threading.RLock()", source)
        event = source.split("def event(self,", 1)[1].split("def flush_events", 1)[0]
        self.assertIn("with self._event_lock:", event)
        self.assertIn("self.flush_events(nonblocking=True)", event)
        listing = source.split("def list_events(self,", 1)[1].split("def find_agent_by_target", 1)[0]
        self.assertIn("with self._event_lock:", listing)
        self.assertNotIn("with self.lock:", listing)

    def test_hot_agent_cards_report_actual_websocket_state(self):
        source = self.source("release_017_ui_lifeline.py")
        hot = source.split("def hot_agent_payloads", 1)[1].split("queue_runtime._agent_payloads", 1)[0]
        self.assertIn("realtime_connected = bool(core.ENGINE.ws_connected)", hot)
        self.assertIn('runtime_payload["realtime_connected"] = realtime_connected', hot)

    def test_hot_status_separates_ha_rest_reachability_from_websocket(self):
        source = self.source("release_017_ui_lifeline.py")
        status = source.split("def status_payload", 1)[1]
        self.assertIn('payload["ha_connected"] = bool(ws_connected or ha_rest_connected)', status)
        self.assertIn('"connected": bool(ws_connected)', status)
        self.assertIn('"inference_scheduler": inference_scheduler', status)


if __name__ == "__main__":
    unittest.main()
