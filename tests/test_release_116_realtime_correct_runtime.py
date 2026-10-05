import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401
import engine as engine_module
from engine import HAEventStream

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"


class FakeRegistryWorker:
    def submit(self, callback, payload):
        callback(payload)


class FakeEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.ws_connected = False
        self.ws_subscription_confirmed = False
        self.ws_messages_total = 0
        self.ws_state_events_total = 0
        self.ws_last_message = None
        self.ws_error = None
        self.registry_refresh_stats = {"coalesced": 0, "requests": 0}
        self.registry_worker = FakeRegistryWorker()
        self.state_resync_stats = {"urgent_requested": 0}
        self.state_resync_urgent = False
        self.state_resync_due_since_monotonic = 0.0
        self.next_resync_retry_monotonic = 0.0
        self.last_full_poll = 0.0
        self.wake_event = threading.Event()
        self.events = []
        self.connected_when_event = None
        self.confirmed_when_event = None
        self.stream = None

    def update_entity_registry(self, _payload):
        return None

    def update_device_registry(self, _payload):
        return None

    def update_area_registry(self, _payload):
        return None

    def on_state_changed(self, data):
        self.events.append(dict(data))
        self.connected_when_event = self.ws_connected
        self.confirmed_when_event = self.ws_subscription_confirmed
        if self.stream is not None:
            self.stream.stop_event.set()


class FakeWebSocket:
    def __init__(self, loop_messages, *, stop_stream_on_first_loop=False):
        self.loop_messages = list(loop_messages)
        self.handshake = [
            {"type": "auth_required"},
            {"type": "auth_ok"},
        ]
        self.sent = []
        self.stop_stream_on_first_loop = bool(stop_stream_on_first_loop)
        self.stream = None
        self.loop_reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self, timeout=None):
        if timeout is None and self.handshake:
            return json.dumps(self.handshake.pop(0))
        self.loop_reads += 1
        if not self.loop_messages:
            raise TimeoutError()
        if self.stop_stream_on_first_loop and self.loop_reads == 1 and self.stream is not None:
            self.stream.stop_event.set()
        return json.dumps(self.loop_messages.pop(0))


class RealtimeSubscription116Tests(unittest.TestCase):
    def run_stream(self, ws):
        fake = FakeEngine()
        stream = HAEventStream(fake)
        fake.stream = stream
        ws.stream = stream
        with patch.object(engine_module, "ws_connect", return_value=ws):
            stream.run()
        return fake, ws

    def test_state_subscription_must_be_confirmed_before_event_is_healthy(self):
        ws = FakeWebSocket([
            {"id": 2, "type": "result", "success": True, "result": None},
            {
                "id": 2,
                "type": "event",
                "event": {"data": {"entity_id": "light.test", "new_state": {"state": "on"}}},
            },
        ])
        fake, ws = self.run_stream(ws)
        self.assertEqual(len(fake.events), 1)
        self.assertTrue(fake.connected_when_event)
        self.assertTrue(fake.confirmed_when_event)
        self.assertEqual(fake.ws_state_events_total, 1)
        self.assertGreaterEqual(fake.ws_messages_total, 2)
        self.assertTrue(any(
            row.get("id") == 2
            and row.get("type") == "subscribe_events"
            and row.get("event_type") == "state_changed"
            for row in ws.sent
        ))

    def test_failed_state_subscription_is_not_reported_connected_and_requests_resync(self):
        ws = FakeWebSocket(
            [{"id": 2, "type": "result", "success": False, "error": {"message": "denied"}}],
            stop_stream_on_first_loop=True,
        )
        fake, _ = self.run_stream(ws)
        self.assertFalse(fake.ws_connected)
        self.assertFalse(fake.ws_subscription_confirmed)
        self.assertIn("state_changed subscription failed", fake.ws_error)
        self.assertTrue(fake.state_resync_urgent)
        self.assertEqual(fake.state_resync_stats["urgent_requested"], 1)


class CorrectRuntimeComposition116Tests(unittest.TestCase):
    def test_final_runtime_installs_generation_correct_after_workflow_routes(self):
        source = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertIn(
            "from agent_correct_generation_history import install as install_correct_generation_history",
            source,
        )
        workflow = source.index("manager = install_agent_workflow_actions(manager, legacy_get=False)")
        request_queue = source.index("manager = install_workflow_request_queue(manager)")
        generation = source.index("manager = install_correct_generation_history(manager, legacy_get=False)")
        explore = source.index("manager = install_agent_explore(manager, legacy_get=False)")
        self.assertLess(workflow, request_queue)
        self.assertLess(request_queue, generation)
        self.assertLess(generation, explore)

    def test_generation_correct_installer_exposes_runtime_binding_flag(self):
        source = (SRC / "agent_correct_generation_history.py").read_text(encoding="utf-8")
        self.assertIn('manager._correct_generation_history_installed = True', source)
        self.assertIn('tokens[3] in ("correct-history", "correct-point")', source)

    def test_debug_export_exposes_subscription_and_correct_binding(self):
        source = (SRC / "runtime_debug_log.py").read_text(encoding="utf-8")
        self.assertIn('"subscription_confirmed"', source)
        self.assertIn('"messages_total"', source)
        self.assertIn('"state_events_total"', source)
        self.assertIn('"generation_correct_history_installed"', source)
        self.assertRegex(source, r"CONTRACT_VERSION = [3-9][0-9]*")

    def test_realtime_status_exposes_confirmed_subscription_counters(self):
        source = (SRC / "engine.py").read_text(encoding="utf-8")
        self.assertIn('"subscription_confirmed": ws_subscription_confirmed', source)
        self.assertIn('"messages_total": ws_messages_total', source)
        self.assertIn('"state_events_total": ws_state_events_total', source)
        self.assertIn("state_changed subscription confirmation timeout", source)


if __name__ == "__main__":
    unittest.main()
