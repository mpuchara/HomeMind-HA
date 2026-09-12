#!/usr/bin/env python3
from pathlib import Path
import json
import re

ROOT = Path("_release_v0714/adaptive_ai")
MAIN = ROOT / "src/main.py"
APP = ROOT / "src/static/app.js"
CFG = ROOT / "config.yaml"
CHANGELOG = ROOT / "CHANGELOG.md"
BUILD = ROOT / "BUILD_INFO.json"


def replace_once(text, old, new, label):
    if old not in text:
        raise SystemExit(f"missing patch anchor: {label}")
    return text.replace(old, new, 1)


main = MAIN.read_text()
main = replace_once(main, 'from contextlib import contextmanager\n', 'from contextlib import contextmanager\nimport gc\n', 'import gc')
main = replace_once(main, 'APP_VERSION = "0.7.13"', 'APP_VERSION = "0.7.14"', 'version')
main = replace_once(main, '    "agent_training_chunk_hours": 48,\n    "agent_training_overlap_hours": 12,', '    "agent_training_chunk_hours": 24,\n    "agent_training_overlap_hours": 6,', 'smaller training chunks')
main = replace_once(main, '    "history_parallel_requests": 2,', '    "history_parallel_requests": 1,', 'single recorder worker')
main = replace_once(main, '    "history_background_pause_ms": 250,', '    "history_background_pause_ms": 500,', 'training throttle')
main = replace_once(main, '    "process_nice": 10,\n}', '    "process_nice": 10,\n    "manual_agent_training": True,\n    "max_concurrent_training_jobs": 1,\n    "manual_discovery_hours": 24,\n}', 'manual training options')

# New agents are discovered but stay idle/paused until the user explicitly starts training.
main = replace_once(
    main,
    '            c.execute("UPDATE agents SET training_state=\'training\', mode=\'paused\', training_progress=0, training_updated_at=? WHERE id=?", (iso_now(), agent_id))',
    '            c.execute("UPDATE agents SET training_state=\'paused\', mode=\'paused\', training_progress=0, training_updated_at=? WHERE id=?", (iso_now(), agent_id))',
    'new agent paused state',
)

# Add bounded/streaming archive APIs. train_from_archive previously materialized millions of
# dicts in RAM (one dict per archived HA state row).
old_archive = '''    def archive_rows(self, start_ts=None, end_ts=None, entity_id=None):
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        if entity_id:
            where.append("entity_id=?"); vals.append(entity_id)
        sql = "SELECT * FROM entity_history" + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY ts,id"
        with self.conn() as c:
            rows = c.execute(sql, vals).fetchall()
        return [dict(r) for r in rows]
'''
new_archive = '''    def archive_rows(self, start_ts=None, end_ts=None, entity_id=None):
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        if entity_id:
            where.append("entity_id=?"); vals.append(entity_id)
        sql = "SELECT * FROM entity_history" + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY ts,id"
        with self.conn() as c:
            rows = c.execute(sql, vals).fetchall()
        return [dict(r) for r in rows]

    def archive_count(self, start_ts=None, end_ts=None, entity_ids=None):
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        ids = sorted(set(entity_ids or []))
        if ids:
            where.append("entity_id IN (%s)" % ",".join("?" for _ in ids)); vals.extend(ids)
        sql = "SELECT COUNT(*) FROM entity_history" + ((" WHERE " + " AND ".join(where)) if where else "")
        with self.conn() as c:
            return int(c.execute(sql, vals).fetchone()[0] or 0)

    def archive_iter(self, start_ts=None, end_ts=None, entity_ids=None, chunk_size=2000):
        """Stream history rows in bounded batches instead of materializing the archive."""
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        ids = sorted(set(entity_ids or []))
        if ids:
            where.append("entity_id IN (%s)" % ",".join("?" for _ in ids)); vals.extend(ids)
        sql = "SELECT * FROM entity_history" + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY ts,id"
        with self.conn() as c:
            cursor = c.execute(sql, vals)
            while True:
                batch = cursor.fetchmany(max(100, int(chunk_size)))
                if not batch:
                    break
                for row in batch:
                    yield dict(row)

    def archive_rows_for_entities(self, start_ts, end_ts, entity_ids):
        return list(self.archive_iter(start_ts=start_ts, end_ts=end_ts, entity_ids=entity_ids, chunk_size=2000))
'''
main = replace_once(main, old_archive, new_archive, 'streaming archive api')

# Pause stale jobs after a restart in manual mode, but preserve cursor/model so the user can Resume.
anchor = '''    def candidate_agent_ids(self):
        # Compatibility name used by discovery: only brand-new, not resumable jobs.
        return self.training_agent_ids(unstarted_only=True)

    def qualified_agents(self):
'''
replacement = '''    def candidate_agent_ids(self):
        # Compatibility name used by discovery: only brand-new, not resumable jobs.
        return self.training_agent_ids(unstarted_only=True)

    def pause_stale_training_agents(self):
        with self.lock, self.conn() as c:
            cur = c.execute("UPDATE agents SET training_state='paused', mode='paused', training_updated_at=? WHERE training_state='training'", (iso_now(),))
            return int(cur.rowcount or 0)

    def qualified_agents(self):
'''
main = replace_once(main, anchor, replacement, 'pause stale jobs')

# Only one heavy training job at a time on low-memory Home Assistant hosts.
old_job = '''        with self.agent_jobs_lock:
            if agent_id in self.agent_jobs:
                return False
            self.agent_jobs.add(agent_id)
'''
new_job = '''        with self.agent_jobs_lock:
            if agent_id in self.agent_jobs:
                return False
            max_jobs = max(1, int(OPTIONS.get("max_concurrent_training_jobs", 1)))
            if len(self.agent_jobs) >= max_jobs:
                return False
            self.agent_jobs.add(agent_id)
'''
main = replace_once(main, old_job, new_job, 'training concurrency')
main = replace_once(
    main,
    '''            finally:
                with self.agent_jobs_lock:
                    self.agent_jobs.discard(agent_id)

        threading.Thread(target=worker, name=f"adaptive-ai-index-{agent_id}", daemon=True).start()
''',
    '''            finally:
                with self.agent_jobs_lock:
                    self.agent_jobs.discard(agent_id)
                # Explicitly collect the large temporary replay structures after each job.
                gc.collect()

        threading.Thread(target=worker, name=f"adaptive-ai-index-{agent_id}", daemon=True).start()
''',
    'post-training gc',
)

# Do not auto-resume a heavy training job after reboot when manual training is enabled.
main = replace_once(
    main,
    '''                self.bootstrap_and_train()
                self.resume_incomplete_jobs()
                self.error = None
''',
    '''                self.bootstrap_and_train()
                if not bool(OPTIONS.get("manual_agent_training", True)):
                    self.resume_incomplete_jobs()
                self.error = None
''',
    'no automatic resume',
)

# Compact temporal cache and shorten per-entity deque. The full live HA state remains in state_map;
# temporal lags only need state plus a small attribute subset.
main = replace_once(main, '        self.temporal_history = TemporalHistory(maxlen=64)', '        self.temporal_history = TemporalHistory(maxlen=24)', 'temporal cache bound')
main = replace_once(
    main,
    '''    def _compact_attrs(self, st):
        attrs = st.get("attributes") or {}
        compact = {}
        for k, v in attrs.items():
            if k in NUMERIC_ATTRS or k in (
                "device_class", "unit_of_measurement", "friendly_name", "supported_color_modes",
                "min", "max", "step", "options", "supported_features",
            ):
                compact[k] = v
        return compact
''',
    '''    def _compact_attrs(self, st):
        attrs = st.get("attributes") or {}
        compact = {}
        for k, v in attrs.items():
            if k in NUMERIC_ATTRS or k in (
                "device_class", "unit_of_measurement", "friendly_name", "supported_color_modes",
                "min", "max", "step", "options", "supported_features",
            ):
                compact[k] = v
        return compact

    def _temporal_state(self, st):
        if st is None:
            return None
        return {
            "entity_id": st.get("entity_id"), "state": st.get("state"),
            "attributes": self._compact_attrs(st),
            "last_changed": st.get("last_changed"), "last_updated": st.get("last_updated"),
        }
''',
    'compact temporal state helper',
)
main = main.replace('self.temporal_history.add(entity_id, ts, new_state)', 'self.temporal_history.add(entity_id, ts, self._temporal_state(new_state))')
main = main.replace('self.temporal_history.add(st.get("entity_id"), ts, st)', 'self.temporal_history.add(st.get("entity_id"), ts, self._temporal_state(st))')

# Manual lightweight bootstrap: target discovery/history only. No broad context import, no
# all-agent replay, no automatic benchmark. All eligible sensors are screened later when the
# user starts a specific agent.
bootstrap_anchor = '    def bootstrap_and_train(self):\n'
manual_method = '''    def _manual_lightweight_cycle(self, current, controllable, end_ts):
        start_ts = end_ts - float(OPTIONS["history_bootstrap_days"]) * 86400.0
        last = parse_ts(STORE.meta_get("manual_discovery_refresh"))
        if last:
            refresh_start = max(end_ts - 2 * 3600.0, float(last) - 300.0)
        else:
            refresh_start = max(start_ts, end_ts - max(6.0, float(OPTIONS.get("manual_discovery_hours", 24))) * 3600.0)
        self.set_status(
            "manual_ready", 0.10,
            "Low-memory discovery: refreshing controllable-device history only",
            phase_detail="Agent training is manual; whole-home context stays idle",
        )
        if controllable and end_ts > refresh_start:
            self._import_section(
                controllable, refresh_start, end_ts, batch_size=8, minimal=False, no_attributes=False,
                source="ha_history_full", progress_lo=0.10, progress_hi=0.65,
                label="Lightweight target discovery", max_hours=3, parallel_requests=1,
                inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 500)),
            )
        self.refresh_archive_cache()
        created = self.auto_discover_agents(current, start_ts)
        self.auto_created += created
        # Populate diagnostics from current state only; this does not import context history.
        self._eligible_rebuild_context()
        STORE.meta_set("manual_discovery_refresh", iso_from_ts(end_ts))
        STORE.meta_set("training_revision", TRAINING_REVISION)
        self.last_run = now_ts()
        q = len(STORE.qualified_agents())
        waiting = len([a for a in STORE.list_agents() if a.get("enabled") and a.get("training_state") == "paused"])
        self.set_status(
            "ready", 1.0,
            f"Low-memory mode ready · {q} trained / {waiting} waiting for manual training",
            stage_eta_seconds=0, work_done=0, work_total=0, work_unit="agents",
            eta_source="idle", phase_detail="Press Train on one agent; only one training job can run at a time",
        )

'''
main = replace_once(main, bootstrap_anchor, manual_method + bootstrap_anchor, 'manual lightweight method')

# Preserve existing models on upgrade and exit into the lightweight path before the old automatic
# context refresh/training stages.
main = replace_once(
    main,
    '''        training_rebuild = STORE.meta_get("training_revision", "") != TRAINING_REVISION
        if training_rebuild:
            STORE.clear_historical_models()
            self.engine.models.clear()
''',
    '''        manual_training = bool(OPTIONS.get("manual_agent_training", True))
        training_rebuild = STORE.meta_get("training_revision", "") != TRAINING_REVISION
        if training_rebuild and not manual_training:
            STORE.clear_historical_models()
            self.engine.models.clear()
''',
    'manual preserve models',
)
main = replace_once(
    main,
    '''                AUTOMATION_KNOWLEDGE.scan(current, registry)

        if bootstrap_done:
''',
    '''                AUTOMATION_KNOWLEDGE.scan(current, registry)

        if manual_training:
            self._manual_lightweight_cycle(current, controllable, end_ts)
            return

        if bootstrap_done:
''',
    'manual early return',
)

# Stream the broad screening pass and materialize only the one agent's selected context for replay.
main = replace_once(
    main,
    '''        rows = STORE.archive_rows(start_ts=start_ts, end_ts=end_ts)
        if not rows:
            return 0
        progress_enabled = progress_lo is not None and progress_hi is not None and float(progress_hi) > float(progress_lo)
''',
    '''        archive_row_count = STORE.archive_count(start_ts=start_ts, end_ts=end_ts)
        if archive_row_count <= 0:
            return 0
        progress_enabled = progress_lo is not None and progress_hi is not None and float(progress_hi) > float(progress_lo)
''',
    'archive count instead of full materialization',
)
main = main.replace('work_total=len(rows), work_unit="history rows"', 'work_total=archive_row_count, work_unit="history rows"')
# First screening loop only.
train_pos = main.index('    def train_from_archive(')
loop_old = '        for row in rows:\n            ts = float(row["ts"]); eid = row["entity_id"]\n'
loop_at = main.find(loop_old, train_pos)
if loop_at < 0:
    raise SystemExit('missing patch anchor: screening loop')
loop_new = '        for row in STORE.archive_iter(start_ts=start_ts, end_ts=end_ts, chunk_size=2000):\n            ts = float(row["ts"]); eid = row["entity_id"]\n'
main = main[:loop_at] + loop_new + main[loop_at + len(loop_old):]

main = replace_once(
    main,
    '''        horizons = sorted({h for p in policies.values() for h in p.horizons})
        watched_entities = {eid for p in policies.values() for eid in p.schema.entities}
        timeline = HistoricalTemporalTracker(rows, watched_entities)
''',
    '''        horizons = sorted({h for p in policies.values() for h in p.horizons})
        watched_entities = {eid for p in policies.values() for eid in p.schema.entities}
        replay_entities = set(watched_entities) | set(target_map.keys())
        rows = STORE.archive_rows_for_entities(start_ts, end_ts, replay_entities)
        if not rows:
            return 0
        timeline = HistoricalTemporalTracker(rows, watched_entities)
''',
    'selected-context replay materialization',
)

# Startup cleanup of stale v0.7.13 jobs happens once, before the user can start a new one.
main = replace_once(
    main,
    '''        self.agent_jobs = set()
        self.agent_jobs_lock = threading.RLock()
''',
    '''        self.agent_jobs = set()
        self.agent_jobs_lock = threading.RLock()
        if bool(OPTIONS.get("manual_agent_training", True)):
            paused = STORE.pause_stale_training_agents()
            if paused:
                STORE.event(None, "info", "manual_training_migration", f"Paused {paused} unfinished automatic training job(s); resume manually when ready", None)
''',
    'startup stale job pause',
)

# Rescan discovers agents only; it must never launch training.
main = replace_once(
    main,
    '''                created = HISTORY.auto_discover_agents(current, start_ts, threshold_override=1)
                candidate_ids = STORE.candidate_agent_ids() if created else []
                for aid in candidate_ids:
                    HISTORY._start_agent_job(aid, rebuild=True)
                return self.send_json(200, {"ok": True, "created": created, "training_started": len(candidate_ids), "history": HISTORY.status(), "automation_knowledge": AUTOMATION_KNOWLEDGE.status()})
''',
    '''                created = HISTORY.auto_discover_agents(current, start_ts, threshold_override=1)
                return self.send_json(200, {"ok": True, "created": created, "training_started": 0, "manual_training": True, "history": HISTORY.status(), "automation_knowledge": AUTOMATION_KNOWLEDGE.status()})
''',
    'rescan no autotraining',
)

# Dedicated per-agent Train endpoint. New/never-trained agents rebuild from raw history; a
# partially completed paused job resumes from its cursor.
resume_anchor = '''            if path.startswith("/api/agents/") and path.endswith("/resume"):
'''
train_endpoint = '''            if path.startswith("/api/agents/") and path.endswith("/train"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if not agent or HISTORY is None:
                    return self.send_json(404, {"error": "agent/history engine not found"})
                if agent.get("training_state") == "training":
                    return self.send_json(409, {"error": "this agent is already training"})
                partial = agent.get("training_cursor_ts") is not None and float(agent.get("training_progress") or 0.0) < 0.999
                started = HISTORY.request_agent_resume(agent_id) if partial else HISTORY.request_agent_rebuild(agent_id)
                if not started:
                    return self.send_json(409, {"error": "another training job is already active; low-memory mode allows one at a time"})
                return self.send_json(202, {"ok": True, "state": "training", "resumed": bool(partial), "message": "Per-agent training started in low-memory mode"})
'''
main = replace_once(main, resume_anchor, train_endpoint + resume_anchor, 'train endpoint')

MAIN.write_text(main)

# UI: accurately describe idle agents and expose Train instead of silently starting work.
app = APP.read_text()
app = replace_once(
    app,
    "    const training=rt.training_state||a.training_state||'training', paused=training==='paused', indexing=training==='training', qualified=training==='qualified';",
    "    const training=rt.training_state||a.training_state||'paused', paused=training==='paused', indexing=training==='training', qualified=training==='qualified';",
    'ui default paused',
)
app = replace_once(
    app,
    "    const trainProgress=Math.round(Number(a.training_progress||0)*100);\n    const decisionReason=paused?`Historical pass complete. Behaviour confidence ${benchmark==null?'—':pct(benchmark)} (${benchmarkSamples} samples) — paused to save CPU. Resume continues from the saved cursor.`:indexing?`Indexing historical data ${trainProgress}% — live inference is paused until the full pass reaches current data.`:(rt.decision_reason||'Waiting for inference');",
    "    const trainProgress=Math.round(Number(a.training_progress||0)*100);\n    const neverTrained=paused&&benchmark==null&&!a.training_cursor_ts;\n    const decisionReason=neverTrained?'Training has not started. Low-memory mode keeps this agent idle until you press Train.':paused?`Training paused. Behaviour confidence ${benchmark==null?'—':pct(benchmark)} (${benchmarkSamples} samples). Resume continues from the saved cursor.`:indexing?`Training this agent ${trainProgress}% — other agents remain idle to protect Home Assistant resources.`:(rt.decision_reason||'Waiting for inference');",
    'ui idle reason',
)
old_actions = "      <div class=\"actions\">${['shadow','control','paused'].map(m=>`<button class=\"ghost ${a.mode===m?'active':''}\" ${(m==='control'&&!qualified)||indexing?'disabled title=\"Requires completed benchmark >78%\"':''} onclick=\"setMode('${a.id}','${m}')\">${m}</button>`).join('')}${paused?`<button class=\"ghost resume\" onclick=\"resumeLearning('${a.id}')\">Resume</button>`:''}<button class=\"ghost\" onclick=\"editAgent('${a.id}')\">Ustawienia</button><button class=\"ghost\" onclick=\"verifyControl('${a.id}')\">Verify control</button><button class=\"ghost ${a.micro_exploration?'active warn':''}\" ${!qualified?'disabled':''} onclick=\"toggleExplore('${a.id}',${a.micro_exploration?'false':'true'})\">Explore ${a.micro_exploration?'ON':'OFF'}</button><button class=\"ghost rebuild\" ${indexing?'disabled':''} onclick=\"resetLearning('${a.id}')\">Rebuild</button><button class=\"ghost danger\" onclick=\"removeAgent('${a.id}')\">Delete</button></div>"
new_actions = "      <div class=\"actions\">${['shadow','control','paused'].map(m=>`<button class=\"ghost ${a.mode===m?'active':''}\" ${(m==='control'&&!qualified)||indexing?'disabled title=\"Requires completed benchmark >78%\"':''} onclick=\"setMode('${a.id}','${m}')\">${m}</button>`).join('')}${neverTrained?`<button class=\"ghost resume\" onclick=\"trainAgent('${a.id}')\">Train</button>`:(paused?`<button class=\"ghost resume\" onclick=\"resumeLearning('${a.id}')\">Resume</button>`:'')}<button class=\"ghost\" onclick=\"editAgent('${a.id}')\">Ustawienia</button><button class=\"ghost\" onclick=\"verifyControl('${a.id}')\">Verify control</button><button class=\"ghost ${a.micro_exploration?'active warn':''}\" ${!qualified?'disabled':''} onclick=\"toggleExplore('${a.id}',${a.micro_exploration?'false':'true'})\">Explore ${a.micro_exploration?'ON':'OFF'}</button><button class=\"ghost rebuild\" ${indexing||neverTrained?'disabled':''} onclick=\"resetLearning('${a.id}')\">Rebuild</button><button class=\"ghost danger\" onclick=\"removeAgent('${a.id}')\">Delete</button></div>"
app = replace_once(app, old_actions, new_actions, 'ui train button')
app = replace_once(
    app,
    "async function resumeLearning(id){if(!confirm('Resume learning from the saved historical cursor? Existing model and benchmark are preserved.'))return;try{await api(`api/agents/${id}/resume`,{method:'POST',body:'{}'});await load();}catch(e){alert('Resume failed: '+e.message);await load();}}",
    "async function trainAgent(id){if(!confirm('Start training this agent now? Low-memory mode trains only one agent at a time; all other agents stay idle.'))return;try{await api(`api/agents/${id}/train`,{method:'POST',body:'{}'});await load();}catch(e){alert('Train failed: '+e.message);await load();}}\nasync function resumeLearning(id){if(!confirm('Resume learning from the saved historical cursor? Existing model and benchmark are preserved.'))return;try{await api(`api/agents/${id}/resume`,{method:'POST',body:'{}'});await load();}catch(e){alert('Resume failed: '+e.message);await load();}}",
    'train js function',
)
app = replace_once(
    app,
    'window.setMode=setMode;window.verifyControl=verifyControl;window.toggleExplore=toggleExplore;window.resumeLearning=resumeLearning;window.resetLearning=resetLearning;window.removeAgent=removeAgent;',
    'window.setMode=setMode;window.verifyControl=verifyControl;window.toggleExplore=toggleExplore;window.trainAgent=trainAgent;window.resumeLearning=resumeLearning;window.resetLearning=resetLearning;window.removeAgent=removeAgent;',
    'export train function',
)
# Manual-ready phase belongs at the beginning of the preparation pipeline.
app = app.replace("const phaseAliases={automation_scan:'fast_targets'", "const phaseAliases={manual_ready:'ready',automation_scan:'fast_targets'", 1)
APP.write_text(app)

cfg = CFG.read_text()
cfg = cfg.replace('version: "0.7.13"', 'version: "0.7.14"', 1)
cfg = cfg.replace('  agent_training_chunk_hours: 48\n  agent_training_overlap_hours: 12', '  agent_training_chunk_hours: 24\n  agent_training_overlap_hours: 6', 1)
cfg = cfg.replace('  history_parallel_requests: 2', '  history_parallel_requests: 1', 1)
cfg = cfg.replace('  history_background_pause_ms: 250', '  history_background_pause_ms: 500', 1)
cfg = cfg.replace('  process_nice: 10\nschema:', '  process_nice: 10\n  manual_agent_training: true\n  max_concurrent_training_jobs: 1\n  manual_discovery_hours: 24\nschema:', 1)
cfg = cfg.replace('  process_nice: "int(0,19)"', '  process_nice: "int(0,19)"\n  manual_agent_training: "bool"\n  max_concurrent_training_jobs: "int(1,2)"\n  manual_discovery_hours: "int(6,72)"', 1)
CFG.write_text(cfg)

entry = '''\n## 0.7.14 — low-memory manual training\n\n- Agent discovery no longer starts offline RL training automatically. Each agent waits for an explicit **Train** action.\n- Only one training job can run at a time by default.\n- Broad historical context screening is streamed from SQLite in bounded batches instead of materializing millions of rows in Python RAM.\n- The replay phase materializes only the target plus the context entities selected for the active agent.\n- Home Assistant Recorder reads use one worker by default and a longer background yield.\n- Temporal state history stores compact state objects and a shorter per-entity deque.\n- Rescan discovers targets only; it never starts training.\n- Interrupted automatic jobs from 0.7.13 are paused on startup and can be resumed manually.\n\n'''
CHANGELOG.write_text(entry + CHANGELOG.read_text())
BUILD.write_text(json.dumps({
    "version": "0.7.14",
    "profile": "rpi4-low-memory-manual-training",
    "manual_agent_training": True,
    "max_concurrent_training_jobs": 1,
    "archive_screening": "sqlite-streaming",
    "replay_scope": "active-agent-selected-context",
}, indent=2) + "\n")
print("Patched Adaptive AI 0.7.14")
