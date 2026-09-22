from pathlib import Path
import unittest

import support  # noqa: F401
from telemetry import HeavyJobGate, RUNTIME_DEBUG, RuntimeDebugTrace, Telemetry

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"


class RuntimeDebugLogTests(unittest.TestCase):
    def tearDown(self):
        RUNTIME_DEBUG.set_enabled(False, clear=True)

    def test_trace_is_noop_while_disabled(self):
        trace = RuntimeDebugTrace()
        self.assertIsNone(trace.begin("inference_agent", agent_id="a"))
        self.assertIsNone(trace.instant("event_to_intent", sample_ms=12.0))
        exported = trace.export()
        self.assertEqual(exported["entries"], [])
        self.assertEqual(exported["active"], [])

    def test_trace_exposes_current_work_and_completed_duration(self):
        trace = RuntimeDebugTrace()
        trace.set_enabled(True, clear=True)
        token = trace.begin("candidate_lifecycle", candidate_id="c1", state="queued")
        summary = trace.summary()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["active_count"], 1)
        self.assertEqual(summary["active"][0]["operation"], "candidate_lifecycle")
        trace.end(token, status="ok")
        exported = trace.export()
        self.assertEqual(exported["active"], [])
        self.assertTrue(any(row["kind"] == "end" for row in exported["entries"]))

    def test_event_to_intent_samples_are_logged_only_when_enabled(self):
        telemetry = Telemetry()
        RUNTIME_DEBUG.set_enabled(False, clear=True)
        telemetry.observe("event_to_intent", 30.0)
        self.assertFalse(any(
            row.get("operation") == "event_to_intent"
            for row in RUNTIME_DEBUG.export()["entries"]
        ))

        RUNTIME_DEBUG.set_enabled(True, clear=True)
        telemetry.observe("event_to_intent", 10.0)
        telemetry.observe("event_to_intent", 20.0)
        rows = [
            row for row in RUNTIME_DEBUG.export()["entries"]
            if row.get("operation") == "event_to_intent"
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["fields"]["recent_count"], 3)
        self.assertIsNotNone(rows[-1]["fields"]["recent_p95_ms"])

    def test_heavy_job_gate_is_visible_as_active_work(self):
        RUNTIME_DEBUG.set_enabled(True, clear=True)
        gate = HeavyJobGate()
        self.assertTrue(gate.acquire("candidate_correct"))
        active = RUNTIME_DEBUG.summary()["active"]
        self.assertTrue(any(
            row["operation"] == "heavy_job"
            and row["fields"].get("owner") == "candidate_correct"
            for row in active
        ))
        gate.release("candidate_correct")
        self.assertFalse(any(
            row["operation"] == "heavy_job"
            for row in RUNTIME_DEBUG.summary()["active"]
        ))

    def test_runtime_debug_routes_have_toggle_status_and_download(self):
        source = (SRC / "runtime_debug_log.py").read_text(encoding="utf-8")
        self.assertIn('r"^/api/debug/runtime-log$"', source)
        self.assertIn('r"^/api/debug/runtime-log/download$"', source)
        self.assertIn('"Content-Disposition"', source)
        self.assertIn("RUNTIME_DEBUG.set_enabled", source)
        self.assertIn('"threads": _thread_snapshot()', source)

    def test_diagnostics_ui_surfaces_p95_current_work_and_controls(self):
        source = (SRC / "static" / "runtime_debug_ui.js").read_text(encoding="utf-8")
        self.assertIn("event → intent p95 · last 60 s", source)
        self.assertIn("Currently executing", source)
        self.assertIn("Start debug log", source)
        self.assertIn("Stop debug log", source)
        self.assertIn("Download log", source)
        self.assertIn("api/debug/runtime-log/download", source)

    def test_runtime_instrumentation_covers_inference_candidate_and_heavy_work(self):
        engine = (SRC / "engine.py").read_text(encoding="utf-8")
        candidates = (SRC / "agent_candidates.py").read_text(encoding="utf-8")
        telemetry = (SRC / "telemetry.py").read_text(encoding="utf-8")
        self.assertIn('"inference_target"', engine)
        self.assertIn('"inference_agent"', engine)
        self.assertIn('"event_pass"', engine)
        self.assertIn('"candidate_lifecycle"', candidates)
        self.assertIn('debug.begin("heavy_job"', telemetry)
        self.assertNotIn("open(", telemetry)
        self.assertNotIn("write_text(", telemetry)


if __name__ == "__main__":
    unittest.main()
