import json
import threading
import unittest
from unittest.mock import patch

import support  # noqa: F401
import engine as engine_module
from engine import HAEventStream


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
        self.stream = None
        self.connected_when_event = None
        self.confirmed_when_event = None

    def update_entity_registry(self, _payload):
        return None

    def update_device_registry(self, _payload):
        return None

    def update_area_registry(self, _payload):
        return None

    def on_state_changed(self, _data):
        self.connected_when_event = self.ws_connected
        self.confirmed_when_event = self.ws_subscription_confirmed
        if self.stream is not None:
            self.stream.stop_event.set()


class FakeWebSocket:
    def __init__(self):
        self.handshake = [{"type": "auth_required"}, {"type": "auth_ok"}]
        self.sent = []
        self.loop_messages = [
            {"id": 2, "type": "result", "success": True, "result": None},
            {"id": 5, "type": "result", "success": True, "result": None},
            {"id": 6, "type": "result", "success": True, "result": None},
            {"id": 7, "type": "result", "success": True, "result": None},
            {"id": 10, "type": "result", "success": True, "result": []},
            {"id": 11, "type": "result", "success": True, "result": []},
            {"id": 12, "type": "result", "success": True, "result": []},
            {
                "id": 2,
                "type": "event",
                "event": {"data": {"entity_id": "light.test", "new_state": {"state": "on"}}},
            },
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self, timeout=None):
        if timeout is None and self.handshake:
            return json.dumps(self.handshake.pop(0))
        if not self.loop_messages:
            raise TimeoutError()
        return json.dumps(self.loop_messages.pop(0))


class HaWebsocketIdOrder117Tests(unittest.TestCase):
    def test_commands_are_sent_in_strictly_increasing_id_order(self):
        fake = FakeEngine()
        ws = FakeWebSocket()
        stream = HAEventStream(fake)
        fake.stream = stream
        with patch.object(engine_module, "ws_connect", return_value=ws):
            stream.run()

        command_ids = [row["id"] for row in ws.sent if "id" in row]
        self.assertEqual(command_ids, [2, 5, 6, 7, 10, 11, 12])
        self.assertEqual(command_ids, sorted(command_ids))
        self.assertEqual(len(command_ids), len(set(command_ids)))
        self.assertTrue(fake.confirmed_when_event)
        self.assertTrue(fake.connected_when_event)
        self.assertIsNone(fake.ws_error)

    def test_registry_reads_start_only_after_event_subscriptions(self):
        source = open(engine_module.__file__, encoding="utf-8").read()
        state_sub = source.index(
            'ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))'
        )
        registry_event = source.index('(5, "entity_registry_updated")')
        registry_reads = source.index('for name in ("entity", "device", "area"):\n                        send_registry(name, force=True)')
        self.assertLess(state_sub, registry_event)
        self.assertLess(registry_event, registry_reads)


if __name__ == "__main__":
    unittest.main()
