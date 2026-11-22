"""Paired host measurements for Correct through the shipped HTTP route registry.

Run the same driver with --source pointing to each checkout. The fixture uses a
fixed observed history and a stubbed teaching context, so label timings measure
the journal/SQLite path and HTTP overhead. This is not a Pi acceptance test.
"""
import argparse
from contextlib import closing, redirect_stdout
import json
import math
from pathlib import Path
import sqlite3
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def quantiles(samples):
    ordered = sorted(samples)
    return {
        f"p{percentile}_ms": round(ordered[math.ceil(percentile * len(samples) / 100) - 1], 3)
        for percentile in (50, 95, 99)
    } | {"n": len(samples)}


def run(source, samples):
    sys.path[:0] = [str(source / "tests"), str(source / "adaptive_ai" / "src")]
    import trial_queue_main
    from agent_candidate_conservative_correct import _copy_parent_snapshot
    from agent_correct_generation_history import install as install_history
    from agent_workflow_actions import register_mutation_routes, register_read_routes
    from manual_feedback_contract import ManualFeedbackJournal
    from manual_feedback_rl import JournaledRLTeaching
    from manual_feedback_workflow import install as install_journal
    from runtime_http import ExplicitRouteRegistry, install_dispatch
    from test_agent_workflow_actions import AgentWorkflowActionTests
    from workflow_request_queue import WorkflowRequestQueue, register_routes

    fixture = AgentWorkflowActionTests(methodName="runTest")
    fixture.setUp()
    store, manager, engine = fixture.store, fixture.manager, fixture.engine
    aid, entity = fixture.root["id"], fixture.root["target_entity"]
    anchor = time.time() - 2200
    timestamps = [anchor + i for i in range(2000)]
    values = [(aid, ts, float(i % 2), 0.) for i, ts in enumerate(timestamps)]
    store.archive_batch([
        (entity, ts, "on" if current else "off", {}, None, "fixture")
        for _, ts, current, _ in values
    ])
    with store.conn() as db:
        db.executemany("INSERT INTO decision_history VALUES(?,?,?,?)", values)
        db.execute(
            """INSERT INTO historical_experiences
               (agent_id,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json)
               SELECT ?,id,'2026-11-22T00:00:00Z',0,0,1,30,'{"0":1}'
               FROM entity_history WHERE entity_id=?""", (aid, entity)
        )

    install_history(manager, legacy_get=False)
    engine.manual_feedback_journal = ManualFeedbackJournal(store)
    engine.rl_teaching = JournaledRLTeaching(store, engine)
    engine.teaching.point_context = Mock(return_value=(
        engine.state_map, {}, engine.policy(fixture.root)
    ))
    install_journal(manager)
    manager.workflow_requests = queue = WorkflowRequestQueue(manager, start_worker=False)

    core = trial_queue_main.core
    core.STORE, core.ENGINE, core.AUTOMATION_KNOWLEDGE = store, engine, object()
    core.set_startup("ready", 7, "fixture", ready=True)
    registry = ExplicitRouteRegistry()
    register_read_routes(registry, manager)
    register_mutation_routes(registry, manager)
    register_routes(registry, manager)
    core.HTTP_SERVER = server = ThreadingHTTPServer(("127.0.0.1", 0), core.Handler)
    install_dispatch(core, registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    results = {}
    path = f"http://127.0.0.1:{server.server_port}/api/agent-workflow/{aid}"

    def measure(name, operation):
        started = time.perf_counter()
        result = operation()
        results.setdefault(name, []).append((time.perf_counter() - started) * 1000)
        return result

    def http(endpoint, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(path + endpoint, data=payload, headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=35)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.load(response)

    def point(timestamp):
        return http(f"/correct-point?ts={timestamp}")

    def label(timestamp):
        return http("/correct-label", {"sample_ts": timestamp, "desired_value": 1.})

    try:
        # The release moved signature preparation to the workflow. Stub the same
        # deterministic output at both bindings for a paired SQLite comparison.
        old_signature = patch("manual_feedback_rl.teaching_signature", return_value={"fixture": 1.})
        new_signature = patch("manual_feedback_workflow.teaching_signature", return_value={"fixture": 1.}, create=True)
        with old_signature, new_signature:
            history_url = f"/correct-history?start={timestamps[0]}&end={timestamps[-1]}&compact=1"
            for _ in range(5):
                assert http(history_url)[0] == point(timestamps[-2])[0] == 200
            for index, timestamp in enumerate(timestamps[-samples - 10:-10]):
                code, history = measure("history_http_2000_rows", lambda: http(history_url))
                assert code == 200 and len(history["series"]["current"]["pairs"]) >= 1999, (code, len(history["series"]["current"]["pairs"]))
                code, observed = measure("correct_point_http", lambda: point(timestamp))
                assert code == 200 and observed["desired"] == 0.
                code, saved = measure("correct_label_http", lambda: label(timestamp))
                assert code == 200 and saved["label_id"] is not None, saved
                request_id = f"receipt-{index}"
                code, accepted = measure("durable_admission_http",
                                         lambda: http("/correct", {"request_id": request_id}))
                assert code == 202 and accepted["durable"]
                stage = "worker_create" if index == 0 else "worker_coalesce"
                assert measure(stage, queue.process_once)
                assert queue.status(request_id)["state"] == "done", queue.status(request_id)
            row = manager._candidate_row(aid)
            child = row["candidate_id"]
            for _ in range(samples):
                measure("snapshot_sqlite", lambda: _copy_parent_snapshot(manager, aid, child))
            # Hold a real, separate WAL writer for 350 ms. Record latency and
            # outcomes separately because the release intentionally returns 503.
            outcomes = []
            for i in range(min(8, samples)):
                ready = threading.Event()

                def hold_writer():
                    with closing(sqlite3.connect(store.path)) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        ready.set()
                        time.sleep(.35)

                holder = threading.Thread(target=hold_writer)
                holder.start()
                assert ready.wait(2.)
                code, result = measure("correct_label_with_350ms_writer", lambda: label(timestamps[-1-i]))
                holder.join()
                outcomes.append(code)
                assert code in (200, 503), result
        return {
            "host": sys.platform, "history_rows": 2000,
            "metrics": {name: quantiles(values) for name, values in results.items()},
            "contended_label_http_status": outcomes,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2.)
        fixture.tearDown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--samples", type=int, default=60, choices=range(1, 101), metavar="1-100")
    args = parser.parse_args()
    with redirect_stdout(sys.stderr):
        result = run(args.source.resolve(), args.samples)
    print(json.dumps(result, indent=2))
