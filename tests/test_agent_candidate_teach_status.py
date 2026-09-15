import unittest
from types import SimpleNamespace

import support
from agent_candidate_teach_status import install


class FakeHandler:
    def do_GET(self):
        self.fell_through = True

    def require_trusted_client(self):
        return True

    def require_runtime(self):
        return True

    def send_json(self, code, body):
        self.sent = (code, body)
        return self.sent


class CandidateTeachStatusTests(unittest.TestCase):
    def test_candidate_build_is_not_reported_as_live_training_queue(self):
        live_queue = SimpleNamespace(status_for=lambda agent_id: None)
        teaching = SimpleNamespace(status=lambda agent_id: {"state": "idle", "labels": 3, "report": {}})
        core = SimpleNamespace(
            Handler=FakeHandler,
            STORE=SimpleNamespace(get_agent_config=lambda agent_id: {"id": agent_id}),
            ENGINE=SimpleNamespace(rl_teaching=teaching),
            HISTORY=SimpleNamespace(status=lambda: {"phase": "idle"}),
            TRAINING_QUEUE=live_queue,
        )
        manager = SimpleNamespace(status=lambda agent_id: {"parent_agent_id": agent_id, "state": "building"})
        install(core, manager)
        handler = FakeHandler()
        handler.path = "/api/agents/live-1/teach-rl-status"
        handler.do_GET()
        code, body = handler.sent
        self.assertEqual(code, 200)
        self.assertIsNone(body["training_queue"])
        self.assertEqual(body["candidate"]["state"], "building")
        self.assertEqual(body["state"], "idle")


if __name__ == "__main__":
    unittest.main()
