import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from manual_feedback_live_isolation import install as install_live_isolation
from manual_feedback_unified import UnifiedManualFeedbackJournal
from storage import Store
from teaching import distance


class FakeCandidateManager:
    def __init__(self):
        self.calls = []

    def enqueue(self, agent_id, reason):
        result = {"agent_id": str(agent_id), "reason": str(reason),
                  "generation_id": "candidate:rebuild"}
        self.calls.append(result)
        return result


class UnifiedFeedbackJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "feedback.db"
        self.store = Store(self.path)
        self.now = 1000.0
        self.journal = UnifiedManualFeedbackJournal(self.store, clock=lambda: self.now)
        self.signature = {
            "binary_sensor.motion / state": 1.0,
            "home:p_arrival_30": 0.25,
            "home:p_arrival_120": 0.4,
            "home:occupancy_now": 0.0,
            "meta:home_known": 1.0,
            "meta:feature_schema_version": 12.0,
            "meta:policy_version": 11.0,
            "meta:signature_contract": 2.0,
        }

    def tearDown(self):
        self.temp.cleanup()

    def record(self, correct_action, **kwargs):
        return self.journal.record(
            agent_id="agent-1", selected_ts=kwargs.pop("selected_ts", self.now - 1),
            source=kwargs.pop("source", "correct"), rejected_action=0.0,
            correct_action=correct_action, error_kind=kwargs.pop("error_kind", "state"),
            scope=kwargs.pop("scope", "similar_context"), fingerprint="fp",
            context_signature=dict(kwargs.pop("signature", self.signature)), deadband=.1,
            **kwargs,
        )

    def test_single_feedback_and_negative_only_fact_are_explicit(self):
        positive = self.record(1.0)
        self.assertEqual(positive["application_status"], "recorded")
        self.assertEqual(positive["correct_action"], 1.0)

        negative = self.journal.record(
            agent_id="agent-2", selected_ts=self.now - 2, source="negative_rating",
            rejected_action=1.0, correct_action=None, error_kind="too_late",
            scope="episode", context_signature=dict(self.signature), fingerprint="fp2",
        )
        self.assertIsNone(negative["correct_action"])
        self.assertEqual(negative["error_kind"], "too_late")
        self.assertIn("brak założenia", self.journal.ui_summary(negative))

    def test_unresolved_conflict_stays_conflict_for_later_labels(self):
        first = self.record(0.0, selected_ts=self.now - 3)
        second = self.record(1.0, selected_ts=self.now - 2)
        first = self.journal.get(first["feedback_id"])
        self.assertEqual(first["application_status"], "conflict")
        self.assertEqual(second["application_status"], "conflict")

        # A third instruction matching only one side must not silently reactivate the
        # contradictory context. Missing information remains explicit until undo/context.
        third = self.record(0.0, selected_ts=self.now - 1)
        self.assertEqual(third["application_status"], "conflict")

    def test_one_time_exception_does_not_create_persistent_conflict(self):
        persistent = self.record(0.0, selected_ts=self.now - 3)
        exception = self.record(1.0, selected_ts=self.now - 2, scope="one_time")
        self.assertEqual(self.journal.get(persistent["feedback_id"])["application_status"], "recorded")
        self.assertEqual(exception["application_status"], "recorded")

    def test_delayed_feedback_links_nearby_decision_and_episode(self):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """CREATE TABLE provenance_decisions (
                     decision_id TEXT PRIMARY KEY, episode_id TEXT, generation_id TEXT,
                     agent_id TEXT NOT NULL, created_time REAL NOT NULL)"""
            )
            c.execute(
                "INSERT INTO provenance_decisions VALUES(?,?,?,?,?)",
                ("decision-1", "episode-1", "generation-1", "agent-1", self.now - 20),
            )
        row = self.record(1.0, selected_ts=self.now - 1)
        self.assertEqual(row["decision_id"], "decision-1")
        self.assertEqual(row["episode_id"], "episode-1")
        self.assertEqual(row["generation_id"], "generation-1")

    def test_same_local_observation_different_home_trajectory_does_not_match(self):
        arriving = dict(self.signature)
        leaving = dict(self.signature)
        arriving["home:p_arrival_30"] = 1.0
        leaving["home:p_arrival_30"] = -1.0
        self.assertIsNone(distance(arriving, leaving))

    def test_undo_after_restart_retires_label_context_and_rebuilds_once(self):
        row = self.record(1.0)
        with self.store.lock, self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL, sample_ts REAL NOT NULL, desired REAL NOT NULL,
                    previous_desired REAL, fingerprint TEXT NOT NULL, undone_ts REAL);
                CREATE TABLE manual_context_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL, desired_value REAL NOT NULL,
                    rejected_value REAL, source TEXT NOT NULL, user_id TEXT,
                    snapshot_json TEXT NOT NULL);
                """
            )
            label = c.execute(
                """INSERT INTO teaching_rl_labels
                   (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts)
                   VALUES(?,?,?,?,?,?,NULL)""",
                ("agent-1", self.now, self.now - 1, 1.0, 0.0, "rl-fp"),
            )
            label_id = int(label.lastrowid)
            context = c.execute(
                """INSERT INTO manual_context_feedback
                   (agent_id,created_ts,desired_value,rejected_value,source,user_id,snapshot_json)
                   VALUES(?,?,?,?,?,?,?)""",
                ("agent-1", self.now, 1.0, 0.0, "correct", "ui", "{}"),
            )
            context_id = int(context.lastrowid)
        self.journal.link(row["feedback_id"], "learning", "teach_rl_label", label_id)
        self.journal.link(row["feedback_id"], "context", "manual_context_feedback", context_id)
        self.journal.set_status(
            row["feedback_id"], "learning_queued",
            learning_effect={"candidate_queued": True,
                             "candidate_generation_id": "candidate:old",
                             "rebuild_required": True},
        )

        # Re-open the exact database to prove the retractable contract survives restart.
        fresh = UnifiedManualFeedbackJournal(Store(self.path), clock=lambda: self.now + 100)
        manager = FakeCandidateManager()
        undone = fresh.undo(row["feedback_id"], candidate_manager=manager)
        self.assertEqual(undone["application_status"], "undone")
        self.assertEqual(undone["undo_status"], "rebuild_queued")
        self.assertEqual(len(manager.calls), 1)
        with fresh.store.conn() as c:
            label_row = c.execute(
                "SELECT undone_ts FROM teaching_rl_labels WHERE id=?", (label_id,)
            ).fetchone()
            context_count = c.execute(
                "SELECT COUNT(*) FROM manual_context_feedback WHERE id=?", (context_id,)
            ).fetchone()[0]
        self.assertIsNotNone(label_row["undone_ts"])
        self.assertEqual(context_count, 0)

        # Undo is idempotent: a repeated request cannot queue a second inverse/rebuild.
        fresh.undo(row["feedback_id"], candidate_manager=manager)
        self.assertEqual(len(manager.calls), 1)


class FakePolicy:
    def __init__(self):
        self.updates = 0

    def update(self, *args, **kwargs):
        self.updates += 1

    def serialize(self):
        return {"updates": self.updates}


class FakeTeaching:
    def __init__(self):
        self.physical_calls = 0

    def physical_correction(self, engine, agent, state_map, current, timestamp):
        self.physical_calls += 1


class FakeStore:
    def __init__(self):
        self.feedback = []
        self.events = []

    def add_feedback(self, *args):
        self.feedback.append(args)

    def event(self, *args):
        self.events.append(args)


class PhysicalLiveIsolationTests(unittest.TestCase):
    def test_physical_user_change_keeps_legacy_update_out_of_live_policy(self):
        agent = {
            "id": "agent-1", "target_entity": "light.kitchen", "target_property": "power",
            "deadband": .5, "training_state": "qualified",
        }
        policy = FakePolicy()
        store = FakeStore()
        teaching = FakeTeaching()
        state_map = {
            "light.kitchen": {
                "entity_id": "light.kitchen", "state": "on", "attributes": {},
                "context": {"user_id": "human", "parent_id": None},
            }
        }
        engine = SimpleNamespace(
            runtime={"agent-1": {"previous_target": 0.0, "last_prediction": 0.0, "pending": None}},
            teaching=teaching, manual_feedback_journal=None,
            own_command_echo=lambda *args: False,
        )
        engine.policy = lambda subject: policy

        def legacy_process(subject, states, changed=None):
            # This reproduces the order in Engine.process_agent: physical_correction first,
            # then the old manual policy update and rl_feedback row.
            engine.teaching.physical_correction(engine, subject, states, 1.0, time.time())
            engine.policy(subject).update(1, 1, {0: 1.0}, 1.0)
            store.add_feedback(subject["id"], 1, 1.0, 1.0,
                               "manual demonstration", {0: 1.0}, "human")
            return "ok"

        engine.process_agent = legacy_process
        core = SimpleNamespace(ENGINE=engine, STORE=store)
        install_live_isolation(core)
        self.assertEqual(engine.process_agent(agent, state_map), "ok")
        self.assertEqual(teaching.physical_calls, 1)
        self.assertEqual(policy.updates, 0)
        self.assertEqual(store.feedback, [])


class CompositionContractTests(unittest.TestCase):
    def test_final_entrypoint_uses_unified_journal_and_live_isolation(self):
        source = (Path(__file__).resolve().parents[1] / "adaptive_ai/src/preference_queue_main.py").read_text(encoding="utf-8")
        self.assertIn("UnifiedManualFeedbackJournal", source)
        self.assertIn("install_manual_feedback_live_isolation(core)", source)
        self.assertIn("install_manual_feedback_workflow(manager)", source)


if __name__ == "__main__":
    unittest.main()
