"""0.14.44 regressions for whole-training progress and post-training lifecycle UX."""
import inspect
import unittest

from support import ROOT
from history import HistoryManager
from training_queue import TrainingQueue


class Release044TrainingProgressLifecycleTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / "adaptive_ai" / "src" / "static" / name).read_text(encoding="utf-8")

    def test_history_exposes_whole_training_progress_and_eta(self):
        init = inspect.getsource(HistoryManager.__init__)
        status = inspect.getsource(HistoryManager.status)
        set_status = inspect.getsource(HistoryManager.set_status)
        start = inspect.getsource(HistoryManager._start_agent_job)
        self.assertIn("training_job_started_at", init)
        self.assertIn('"training_overall_progress"', status)
        self.assertIn('"training_overall_eta_seconds"', status)
        self.assertIn('"training_stage_progress"', status)
        self.assertIn("elapsed * max(0.0, 1.0 - effective)", set_status)
        self.assertIn('self.phase = "training"', start)
        self.assertIn('self.phase = "ready"', start)

    def test_task_panel_uses_global_progress_not_persisted_chunk_checkpoint(self):
        source = self.source("p0.js")
        block = source.split("const taskFor = (h,status) => {", 1)[1].split(
            "const b=status.home_bootstrap||{}", 1
        )[0]
        self.assertIn("h.training_overall_progress", block)
        self.assertIn("h.training_overall_eta_seconds", block)
        self.assertIn("Current stage:", block)
        self.assertNotIn("p:Number(training.training_progress||0)", block)
        self.assertIn("stage counters may restart between chunks", source)

    def test_hot_agent_card_receives_continuous_training_progress(self):
        source = (ROOT / "adaptive_ai/src/release_017_ui_lifeline.py").read_text(encoding="utf-8")
        self.assertIn('active_training_progress = history_hot.get("training_overall_progress")', source)
        self.assertIn('agent["training_progress"] = max(0.0, min(1.0, float(active_training_progress)))', source)
        self.assertIn('runtime_payload["training_overall_eta_seconds"]', source)

    def test_queue_revision_forces_fresh_agent_read_after_job_transition(self):
        queue_source = inspect.getsource(TrainingQueue)
        app = self.source("app.js")
        self.assertIn("self.revision = 0", queue_source)
        self.assertIn('"revision": int(self.revision)', queue_source)
        self.assertIn("lastTrainingQueueAgentRefresh", app)
        self.assertIn("training_revision", app)
        self.assertIn("refreshAfterTrainingQueue", app)

    def test_generation_workflow_restores_shadow_and_resume_for_paused_model(self):
        workflow = self.source("agent_workflow_ui.js")
        runtime = self.source("runtime_activity_ui.js")
        self.assertIn('data-wf="shadow">Start Shadow</button>', workflow)
        self.assertIn("window.setMode?.(a.id,a.mode==='shadow'?'paused':'shadow')", workflow)
        self.assertIn(">Resume training</button>", workflow)
        # Last-loaded UI layer is a defensive lifecycle guard.
        self.assertIn("shadow.textContent='Start Shadow'", runtime)
        self.assertIn("resume.textContent='Resume training'", runtime)
        self.assertIn("agent.training_state||agent.runtime?.training_state", runtime)

    def test_runtime_activity_does_not_replace_global_task_panel(self):
        source = self.source("runtime_activity_ui.js")
        active = source.split("if(active){", 1)[1].split("return result;", 1)[0]
        self.assertIn("panel.querySelector('.history-timing span')", active)
        self.assertNotIn("panel.innerHTML=", active)
        self.assertIn("wall duty target", active)


if __name__ == "__main__":
    unittest.main()
