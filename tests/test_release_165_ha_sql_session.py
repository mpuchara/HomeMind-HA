"""Actual HA socket session lifetime, independent commits and fresh reads."""
from contextlib import closing
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support import agent
import engine as engine_module
from engine import HAEventStream
from storage import Store
from test_release_117_ha_websocket_id_order import FakeEngine, FakeWebSocket
from agent_candidates import install_store_overlay, refresh_candidate_ids_cache


def event(target="light.bathroom"):
    return {"id": 2, "type": "event", "event": {"data": {
        "entity_id": target, "new_state": {"state": "on", "context": {"user_id": "operator"}}}}}


class HASession165Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ha-session-165-")
        self.store = Store(Path(self.directory.name) / "test.db")
        install_store_overlay(self.store)
        self.config = self.store.create_agent(agent(target_entity="light.bathroom"))
        self.fake = FakeEngine()
        self.stream = HAEventStream(self.fake)
        self.fake.stream = self.stream
        self.connections = []
        self.original_connect = sqlite3.connect

    def tearDown(self):
        self.directory.cleanup()

    def connect(self, *args, **kwargs):
        connection = self.original_connect(*args, **kwargs)
        if threading.current_thread() is self.stream:
            self.connections.append(connection)
        return connection

    def run_stream(self, sockets, callback, *, allow_error=False):
        self.fake.on_state_changed = callback
        with patch.object(engine_module, "STORE", self.store), \
             patch.object(engine_module, "ws_connect", side_effect=sockets), \
             patch.object(engine_module.time, "sleep", return_value=None), \
             patch("storage.sqlite3.connect", side_effect=self.connect):
            self.stream.start()
            self.stream.join(timeout=8)
            if self.stream.is_alive():
                self.stream.stop_event.set()
                self.stream.join(timeout=8)
            self.assertFalse(self.stream.is_alive(), "HA fixture never stopped")
        if not allow_error:
            self.assertIsNone(self.fake.ws_error)

    def socket(self, messages):
        ws = FakeWebSocket()
        ws.loop_messages = [{"id": 2, "type": "result", "success": True}] + messages
        return ws

    def assert_closed(self, connection):
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_many_real_socket_events_use_one_connection_and_close_on_stop(self):
        rows = []
        def handle(data):
            rows.append(self.store.list_agent_configs_for_target(data["entity_id"]))
            self.assertFalse(self.connections[-1].in_transaction)
            if len(rows) == 25:
                self.stream.stop_event.set()
        self.run_stream([self.socket([event()] * 25)], handle)
        self.assertEqual(len(rows), 25)
        self.assertEqual(len(self.connections), 1)
        self.assertTrue(all(r[0]["id"] == self.config["id"] for r in rows))
        self.assert_closed(self.connections[0])
        self.assertIsNone(self.fake.ws_error)
        self.assertFalse(self.fake.ws_connected)

    def test_same_connection_sees_external_updates_without_process_revision(self):
        observed = []
        revision = self.store._agent_index_revision
        def handle(data):
            observed.append(self.store.list_agent_configs_for_target(data["entity_id"])[0])
            if len(observed) == 1:
                with closing(self.original_connect(self.store.path)) as external, external:
                    external.execute("UPDATE agents SET enabled=0,training_state='needs_retrain',benchmark_detail_json=? WHERE id=?",
                                     ('{"new": [1, 2]}', self.config["id"]))
            else:
                self.stream.stop_event.set()
        self.run_stream([self.socket([event(), event()])], handle)
        self.assertTrue(observed[0]["enabled"])
        self.assertFalse(observed[1]["enabled"])
        self.assertEqual(observed[1]["benchmark_detail"], {"new": [1, 2]})
        self.assertEqual(self.store._agent_index_revision, revision)
        self.assertEqual(len(self.connections), 1)

    def test_idle_socket_does_not_pin_wal_reader_or_prevent_external_commit(self):
        checkpoints = []
        def handle(data):
            self.store.list_agent_configs_for_target(data["entity_id"])
            with closing(self.original_connect(self.store.path)) as external, external:
                external.execute("INSERT INTO app_meta(key,value) VALUES('external','committed')")
            with closing(self.original_connect(self.store.path, isolation_level=None)) as checkpoint:
                checkpoints.append(checkpoint.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
            self.assertFalse(self.connections[-1].in_transaction)
            acquired = []
            def check_lock():
                free = self.store.lock.acquire(blocking=False)
                acquired.append(free)
                if free:
                    self.store.lock.release()
            witness = threading.Thread(target=check_lock)
            witness.start()
            witness.join(timeout=3)
            self.assertEqual(acquired, [True])
            self.stream.stop_event.set()
        self.run_stream([self.socket([event()])], handle)
        busy, log, copied = checkpoints[0]
        self.assertEqual(busy, 0)
        self.assertEqual(log, copied)
        self.assertGreater(copied, 0)

    def test_earlier_callback_commit_survives_later_callback_rollback(self):
        count = 0
        def handle(_data):
            nonlocal count
            count += 1
            with self.store.conn() as c:
                c.execute("INSERT INTO app_meta(key,value) VALUES(?,?)",
                          ("committed" if count == 1 else "rollback", "yes"))
                if count == 2:
                    self.stream.stop_event.set()
                    raise ValueError("callback failed")
            with closing(self.original_connect(self.store.path)) as external:
                self.assertEqual(external.execute("SELECT value FROM app_meta WHERE key='committed'").fetchone()[0], "yes")
        self.run_stream([self.socket([event(), event()])], handle, allow_error=True)
        with self.store.conn() as c:
            self.assertEqual([tuple(r) for r in c.execute("SELECT key,value FROM app_meta WHERE key IN ('committed','rollback')")],
                             [("committed", "yes")])
        self.assertTrue(self.fake.state_resync_urgent)
        self.assertIn("callback failed", self.fake.ws_error)
        self.assert_closed(self.connections[0])

    def test_reconnect_replaces_and_closes_socket_owned_connection(self):
        seen = []
        class BrokenSocket(FakeWebSocket):
            def recv(ws, timeout=None):
                if timeout is not None and not ws.loop_messages:
                    raise ConnectionError("socket lost")
                return super().recv(timeout=timeout)
        first = BrokenSocket()
        first.loop_messages = [{"id": 2, "type": "result", "success": True}, event()]
        def handle(data):
            seen.append(self.store.list_agent_configs_for_target(data["entity_id"]))
            if len(seen) == 2:
                self.stream.stop_event.set()
        self.run_stream([first, self.socket([event()])], handle)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(self.connections), 2)
        self.assertIsNot(self.connections[0], self.connections[1])
        for c in self.connections:
            self.assert_closed(c)
        self.assertEqual(self.fake.state_resync_stats["urgent_requested"], 1)
        self.assertIsNone(self.fake.ws_error)

    def test_auth_failure_closes_session_before_resync(self):
        ws = self.socket([])
        ws.handshake = [{"type": "auth_required"}, {"type": "auth_invalid", "message": "denied"}]
        old_recv = ws.recv
        def recv(timeout=None):
            result = old_recv(timeout=timeout)
            if "auth_invalid" in result:
                self.stream.stop_event.set()
            return result
        ws.recv = recv
        self.run_stream([ws], lambda _data: self.fail("unauthenticated callback"), allow_error=True)
        self.assertEqual(len(self.connections), 1)
        self.assert_closed(self.connections[0])
        self.assertIn("denied", self.fake.ws_error)
        self.assertTrue(self.fake.state_resync_urgent)

    def test_failed_socket_open_never_allocates_sql_connection(self):
        def connect_ws(*_args, **_kwargs):
            self.stream.stop_event.set()
            raise ConnectionError("failed before socket entered")
        with patch.object(engine_module, "STORE", self.store), \
             patch.object(engine_module, "ws_connect", side_effect=connect_ws), \
             patch("storage.sqlite3.connect", side_effect=self.connect):
            self.stream.start()
            self.stream.join(timeout=3)
        self.assertEqual(self.connections, [])
        self.assertIn("failed before socket", self.fake.ws_error)

    def test_unmatched_sensor_still_executes_fresh_select_without_json_decode(self):
        seen = []
        with patch.object(self.store, "_agent_dict", side_effect=AssertionError("unrelated JSON")):
            def handle(data):
                seen.append(self.store.list_agent_configs_for_target(data["entity_id"]))
                if len(seen) == 10:
                    self.stream.stop_event.set()
            self.run_stream([self.socket([event("sensor.radar")] * 10)], handle)
        self.assertEqual(seen, [[]] * 10)
        self.assertEqual(len(self.connections), 1)

    def test_session_keeps_candidate_surrogates_hidden(self):
        child = self.store.create_agent(agent(target_entity="light.bathroom"))
        with self.store.conn() as c:
            c.execute("INSERT INTO agent_candidates(parent_agent_id,candidate_id,updated_ts) VALUES(?,?,1)",
                      (self.config["id"], child["id"]))
        refresh_candidate_ids_cache(self.store)
        rows = []
        def handle(data):
            rows.extend(self.store.list_agent_configs_for_target(data["entity_id"]))
            self.stream.stop_event.set()
        self.run_stream([self.socket([event()])], handle)
        self.assertEqual([r["id"] for r in rows], [self.config["id"]])

    def test_concurrent_foreground_query_owns_separate_thread_connection(self):
        owner = []
        def handle(_data):
            self.store.list_agent_configs_for_target("light.bathroom")
            self.assertTrue(hasattr(self.store._connection_session, "connection"))
            def witness():
                with self.store.conn() as c:
                    owner.append(c)
            thread = threading.Thread(target=witness)
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertIsNot(owner[0], self.connections[-1])
            self.stream.stop_event.set()
        self.run_stream([self.socket([event()])], handle)
        self.assertEqual(len(owner), 1)
        self.assertFalse(hasattr(self.store._connection_session, "connection"))

    def test_shared_connection_timeout_restores_after_background_callback(self):
        timeouts = []
        def handle(_data):
            with self.store.background_sqlite(), self.store.conn() as c:
                timeouts.append(c.execute("PRAGMA busy_timeout").fetchone()[0])
            with self.store.conn() as c:
                timeouts.append(c.execute("PRAGMA busy_timeout").fetchone()[0])
            self.stream.stop_event.set()
        self.run_stream([self.socket([event()])], handle)
        self.assertEqual(timeouts, [250, 30000])


if __name__ == "__main__":
    unittest.main()
