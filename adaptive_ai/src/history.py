from urllib.error import HTTPError
from urllib.error import URLError
from collections import deque
import gc
import math
import threading
import time
import traceback
from settings import (OPTIONS, TRAINING_REVISION, clamp, iso_from_ts, now_ts, parse_ts)
from storage import STORE
from ha import HA, AUTOMATION_KNOWLEDGE
from context import (archived_state, balanced_presence_driver_score, controllable_context_exclusions, default_action_interval, electrical_context_exclusions, entity_capability_tags, historical_reward, is_context_candidate_entity, is_esphome_sensor_entity, is_fast_reactive_agent, numeric_activity_driver_score, occupancy_state_bool, target_options_for_state, target_value, transition_edges)
from telemetry import HEAVY_JOBS, rss_mb
from replay import SQLiteTemporalTracker, DeferredUpdates, BoundedUsage
from training_budget import TRAINING_BUDGET

class HistoryManager(threading.Thread):
    """Bootstraps HA Recorder history, keeps a longer local archive, discovers active targets,
    and turns logged trajectories into offline contextual-RL experiences."""
    daemon = True

    def __init__(self, engine):
        super().__init__(name="adaptive-ai-history")
        self.engine = engine
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.phase = "waiting"
        self.progress = 0.0
        self.message = "Waiting for Home Assistant"
        self.last_run = None
        self.discovered_controllable = 0
        self.discovered_active = 0
        self.discovery_eligible = 0
        self.discovery_filtered_config = 0
        self.discovery_inactive = 0
        self.auto_created = 0
        self.trained_new = 0
        self.error = None
        # Status must remain cheap while Recorder import is busy. In v0.3.3 the UI
        # could sit on “Connecting…” because /api/status waited for a COUNT() on the
        # archive while a large write transaction held the Store lock. Keep a cache
        # instead and refresh it only at safe checkpoints.
        try:
            self.archive_cache = STORE.archive_stats()
        except Exception:
            self.archive_cache = {"n": 0, "min_ts": None, "max_ts": None, "entities": 0, "days": 0.0, "by_source": {}}
        self.cycle_started_at = None
        self.phase_started_at = None
        self.eta_seconds = None
        self.stage_eta_seconds = None
        self.progress_rate_per_min = None
        self._progress_samples = []
        self.chunk_done = 0
        self.chunk_total = 0
        self.work_done = 0
        self.work_total = 0
        self.work_unit = None
        self.eta_source = None
        self.phase_detail = None
        self.context_candidate_count = 0
        self.esphome_candidate_count = 0
        self.esphome_sensor_sibling_overrides = 0
        self.agent_jobs = set()
        self.training_rows_per_second = 0.0
        self.history_rows_per_second = 0.0
        self.job_cancel_event = None
        self.agent_jobs_lock = threading.RLock()
        if bool(OPTIONS.get("manual_agent_training", True)):
            paused = STORE.pause_stale_training_agents()
            if paused:
                STORE.event(None, "info", "manual_training_migration", f"Paused {paused} unfinished automatic training job(s); resume manually when ready", None)

    def status(self):
        with self.lock:
            d = {
                "phase": self.phase, "progress": self.progress, "message": self.message,
                "last_run": self.last_run, "controllable": self.discovered_controllable,
                "active": self.discovered_active, "eligible": self.discovery_eligible,
                "filtered_config": self.discovery_filtered_config, "inactive": self.discovery_inactive,
                "auto_created": self.auto_created, "trained_new": self.trained_new, "error": self.error,
                "eta_seconds": self.eta_seconds, "stage_eta_seconds": self.stage_eta_seconds,
                "elapsed_seconds": (now_ts() - self.cycle_started_at) if self.cycle_started_at else 0,
                "progress_rate_per_min": self.progress_rate_per_min,
                "chunk_done": self.chunk_done, "chunk_total": self.chunk_total,
                "work_done": self.work_done, "work_total": self.work_total, "work_unit": self.work_unit,
                "eta_source": self.eta_source, "phase_detail": self.phase_detail,
                "context_candidates": self.context_candidate_count,
                "esphome_context_candidates": self.esphome_candidate_count,
                "esphome_sensor_sibling_overrides": self.esphome_sensor_sibling_overrides,
                "archive": dict(self.archive_cache),
                "training_rows_per_second": self.training_rows_per_second,
                "history_rows_per_second": self.history_rows_per_second,
            }
        return d

    def qualified_context_entities(self):
        """Context Recorder maintenance set after candidate qualification.

        Live state_changed events still archive the whole home, but expensive historical
        refreshes only need features consumed by qualified policies.
        """
        ids = set()
        for agent in STORE.qualified_agents():
            model = STORE.get_model(agent["id"]) or {}
            schema = model.get("schema") or {}
            ids.update(schema.get("entities") or [])
        return sorted(ids)

    def _training_bounds(self):
        stats = STORE.archive_stats()
        end_ts = float(stats.get("max_ts") or now_ts())
        start_ts = float(stats.get("min_ts") or (end_ts - float(OPTIONS["history_bootstrap_days"]) * 86400.0))
        return start_ts, end_ts

    def _start_agent_job(self, agent_id, rebuild=False):
        agent = STORE.get_agent(agent_id)
        if not agent:
            return False
        with self.agent_jobs_lock:
            if agent_id in self.agent_jobs:
                return False
            max_jobs = 1
            if len(self.agent_jobs) >= max_jobs:
                return False
            if not HEAVY_JOBS.acquire("agent:" + agent_id):
                return False
            self.agent_jobs.add(agent_id)

        try:
            with self.engine.executor.target_lock(agent["target_entity"]):
                if rebuild:
                    STORE.clear_learning(agent_id)
                    self.engine.models.pop(agent_id, None)
                    self.engine.runtime.pop(agent_id, None)
                else:
                    # Resume preserves policy, experiences, benchmark counts and cursor.
                    STORE.set_training_state(
                        agent_id, "training", score=agent.get("benchmark_score"),
                        samples=agent.get("benchmark_samples") or 0, source=agent.get("benchmark_source"),
                        detail=agent.get("benchmark_detail") or {},
                    )
        except Exception:
            with self.agent_jobs_lock:
                self.agent_jobs.discard(agent_id)
            HEAVY_JOBS.release('agent:' + agent_id)
            raise

        def worker():
            TRAINING_BUDGET.begin()
            try:
                self._run_agent_indexing(agent_id, rebuild=rebuild)
            except Exception as exc:
                STORE.event(agent_id, "error", "agent_index_failed", str(exc), {"trace": traceback.format_exc(limit=6)})
                STORE.set_training_state(agent_id, "paused", detail={"reason": str(exc)})
            finally:
                # Keep the heavy slot owned until final GC is complete. Otherwise the UI
                # lifeline can switch back to rich aggregate reads while this worker still
                # monopolizes the interpreter during collection.
                try:
                    TRAINING_BUDGET.checkpoint("pre_training_gc", force=True)
                    gc.collect()
                    TRAINING_BUDGET.checkpoint("post_training_gc", force=True)
                finally:
                    TRAINING_BUDGET.end()
                    with self.agent_jobs_lock:
                        self.agent_jobs.discard(agent_id)
                    HEAVY_JOBS.release("agent:" + agent_id)

        threading.Thread(target=worker, name=f"adaptive-ai-index-{agent_id}", daemon=True).start()
        return True

    def _eligible_rebuild_context(self):
        with self.engine.lock:
            current = dict(self.engine.state_map)
            registry = dict(self.engine.entity_registry)
        excluded_control, control_meta = controllable_context_exclusions(current, registry)
        excluded_electrical, _ = electrical_context_exclusions(current, registry)
        excluded = excluded_control | excluded_electrical
        out = []
        esphome = 0
        for eid, st in current.items():
            if not is_context_candidate_entity(eid, st, excluded):
                continue
            out.append(eid)
            if is_esphome_sensor_entity(eid, registry):
                esphome += 1
        out = sorted(set(out))
        with self.lock:
            self.context_candidate_count = len(out)
            self.esphome_candidate_count = esphome
            self.esphome_sensor_sibling_overrides = int(control_meta.get("esphome_sensor_sibling_overrides") or 0)
        return out

    def _eligible_presence_context(self):
        """Small high-resolution refresh set for fast behavioural driver discovery.

        Kept under the historical method name for compatibility. It includes classic
        occupancy plus non-diagnostic ESPHome radar/AI activity scores.
        """
        with self.engine.lock:
            current = dict(self.engine.state_map)
        eligible = set(self._eligible_rebuild_context())
        return sorted(
            eid for eid in eligible
            if entity_capability_tags(eid, current.get(eid) or {}) & {"occupancy", "activity"}
        )

    def _refresh_agent_history(self, agent, start_ts, end_ts, rebuild=False):
        """Explicit jobs backfill only what they need from Recorder.

        Resume is cheap: target + already selected feature entities from the saved cursor.
        Rebuild is intentionally broader so a newly added sensor can enter feature selection.
        """
        if end_ts <= start_ts:
            return
        target = agent["target_entity"]
        if rebuild:
            context_ids = [eid for eid in self._eligible_rebuild_context() if eid != target]
        else:
            model = STORE.get_model(agent["id"]) or {}
            context_ids = [eid for eid in ((model.get("schema") or {}).get("entities") or []) if eid != target]
        try:
            self._import_section(
                [target], start_ts, end_ts, batch_size=1, minimal=False, no_attributes=False,
                source="ha_history_full", progress_lo=self.progress, progress_hi=self.progress,
                label=f"Agent {agent['id']} · target history", max_hours=6, parallel_requests=1,
                inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
            )
            if context_ids:
                with self.engine.lock:
                    live_states = dict(self.engine.state_map)
                fast_ids = [eid for eid in context_ids if entity_capability_tags(eid, live_states.get(eid) or {}) & {"occupancy", "activity"}]
                regular_ids = [eid for eid in context_ids if eid not in set(fast_ids)]
                if fast_ids:
                    self._import_section(
                        fast_ids, start_ts, end_ts, batch_size=30, minimal=True, no_attributes=True,
                        source="ha_history_fast_context", progress_lo=self.progress, progress_hi=self.progress,
                        label=f"Agent {agent['id']} · high-resolution behavioural context", max_hours=6, parallel_requests=1,
                        inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
                    )
                if regular_ids:
                    self._import_section(
                        regular_ids, start_ts, end_ts, batch_size=50, minimal=True, no_attributes=True,
                        source="ha_history_minimal", progress_lo=self.progress, progress_hi=self.progress,
                        label=f"Agent {agent['id']} · context history", max_hours=12, parallel_requests=1,
                        inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
                    )
            self.refresh_archive_cache()
        except Exception as exc:
            # A Recorder backfill failure must not destroy resumability. Train from the
            # locally available archive and leave a diagnostic event.
            STORE.event(agent["id"], "warning", "agent_history_refresh_partial", str(exc), None)

    def _run_agent_indexing(self, agent_id, rebuild=False):
        agent = STORE.get_agent(agent_id)
        if not agent:
            return
        archive_start, archive_end = self._training_bounds()
        start_ts = archive_start if rebuild or agent.get("training_window_start_ts") is None else float(agent["training_window_start_ts"])
        cursor = start_ts if rebuild or agent.get("training_cursor_ts") is None else max(start_ts, float(agent["training_cursor_ts"]))
        # Explicit Resume/Rebuild reaches current Recorder time, not merely the previous
        # local archive maximum. Resume backfills only selected features; Rebuild scans
        # the broader eligible sensor pool so newly added sensors can be discovered.
        target_end = max(now_ts(), archive_end, float(agent.get("training_window_end_ts") or archive_end))
        refresh_start = start_ts if rebuild else max(start_ts, cursor - max(60.0, float(OPTIONS.get("agent_training_overlap_hours", 12)) * 3600.0))
        self._refresh_agent_history(agent, refresh_start, target_end, rebuild=rebuild)
        # Recorder refresh may extend the archive, but the requested pass still ends at
        # the captured current time for deterministic progress.
        STORE.set_training_progress(agent_id, start_ts, cursor, target_end)
        STORE.event(agent_id, "info", "agent_rebuild_started" if rebuild else "agent_resume_started",
                    "Full historical rebuild started from archive beginning" if rebuild else
                    "Training resumed from saved historical cursor",
                    {"start_ts": start_ts, "cursor_ts": cursor, "end_ts": target_end})

        if cursor >= target_end - 0.5:
            # Nothing new to index. A completed persisted model returns to Shadow even
            # when it is not Control-qualified; PAUSED training_state still blocks Control.
            score = float(agent.get("benchmark_score") or 0.0)
            threshold = float(OPTIONS.get("candidate_benchmark_threshold", 0.78))
            state = "qualified" if score > threshold else "paused"
            STORE.set_training_state(
                agent_id, state, score=agent.get("benchmark_score"),
                samples=agent.get("benchmark_samples") or 0, source=agent.get("benchmark_source"),
                detail=agent.get("benchmark_detail") or {}, shadow_after_completion=True,
            )
            STORE.set_training_progress(agent_id, start_ts, target_end, target_end)
            self.engine.wake_event.set()
            return

        chunk_s = max(6.0, float(OPTIONS.get("agent_training_chunk_hours", 48))) * 3600.0
        overlap_s = max(0.0, min(chunk_s * 0.5, float(OPTIONS.get("agent_training_overlap_hours", 12)) * 3600.0))
        while cursor < target_end - 0.5 and not self.stop_event.is_set():
            chunk_end = min(target_end, cursor + chunk_s)
            chunk_start = max(start_ts, cursor - overlap_s) if cursor > start_ts else start_ts
            final = chunk_end >= target_end - 0.5
            self.train_from_archive(
                chunk_start, chunk_end, qualify=final, agent_ids={agent_id}, include_candidates=True,
                benchmark=True, accumulate_benchmark=True,
                progress_lo=(cursor-start_ts)/max(1,target_end-start_ts),
                progress_hi=(chunk_end-start_ts)/max(1,target_end-start_ts),
                progress_label=f"Training {agent['name']}",
            )
            cursor = chunk_end
            STORE.set_training_progress(agent_id, start_ts, cursor, target_end)
            STORE.event(agent_id, "info", "agent_index_checkpoint",
                        f"Historical indexing checkpoint {((cursor-start_ts)/max(1.0,target_end-start_ts)):.0%}",
                        {"cursor_ts": cursor, "end_ts": target_end, "final": final})
            if not final:
                self.stop_event.wait(max(0.0, float(OPTIONS.get("history_background_pause_ms", 250))) / 1000.0)

    def request_agent_rebuild(self, agent_id):
        return self._start_agent_job(agent_id, rebuild=True)

    def request_agent_resume(self, agent_id):
        agent = STORE.get_agent(agent_id)
        if not agent or agent.get("training_state") not in ("paused", "training", "waiting"):
            return False
        return self._start_agent_job(agent_id, rebuild=False)

    def resume_incomplete_jobs(self):
        # Legacy entry point intentionally inert: restart never launches heavy jobs.
        return 0

    def start_cycle(self):
        with self.lock:
            self.cycle_started_at = now_ts()
            self.phase_started_at = self.cycle_started_at
            self.eta_seconds = None
            self.stage_eta_seconds = None
            self.progress_rate_per_min = None
            self._progress_samples = []
            self.chunk_done = 0
            self.chunk_total = 0

    def refresh_archive_cache(self):
        # Called by the history thread only, never synchronously from the UI.
        try:
            stats = STORE.archive_stats()
        except Exception:
            return
        with self.lock:
            self.archive_cache = stats

    def set_status(self, phase=None, progress=None, message=None, chunk_done=None, chunk_total=None,
                   stage_eta_seconds=None, work_done=None, work_total=None, work_unit=None,
                   eta_source=None, phase_detail=None):
        now = now_ts()
        with self.lock:
            if phase is not None and phase != self.phase:
                self.phase = phase
                self.phase_started_at = now
                self.stage_eta_seconds = None
                self.eta_seconds = None
                self.progress_rate_per_min = None
                self._progress_samples = []
                self.chunk_done = 0
                self.chunk_total = 0
                self.work_done = 0
                self.work_total = 0
                self.work_unit = None
                self.eta_source = None
                self.phase_detail = None
            if progress is not None:
                p = clamp(float(progress), 0, 1)
                self.progress = p
                if self.cycle_started_at is not None:
                    self._progress_samples.append((now, p))
                    cutoff = now - 600
                    self._progress_samples = [x for x in self._progress_samples if x[0] >= cutoff][-60:]
                    base = None
                    for sample in self._progress_samples:
                        if now - sample[0] >= 12 and p - sample[1] >= 0.005:
                            base = sample
                            break
                    if base is not None:
                        dt = max(1.0, now - base[0])
                        dp = max(1e-6, p - base[1])
                        rate = dp / dt
                        eta = (1.0 - p) / rate if p < 0.999 else 0.0
                        eta = clamp(eta, 0.0, 24 * 3600.0)
                        self.eta_seconds = eta if self.eta_seconds is None else (0.72 * self.eta_seconds + 0.28 * eta)
                        self.progress_rate_per_min = rate * 60.0
            if message is not None:
                self.message = message
            if chunk_done is not None:
                self.chunk_done = int(chunk_done)
            if chunk_total is not None:
                self.chunk_total = int(chunk_total)
            if stage_eta_seconds is not None:
                self.stage_eta_seconds = max(0.0, float(stage_eta_seconds))
            if work_done is not None:
                self.work_done = max(0, int(work_done))
            if work_total is not None:
                self.work_total = max(0, int(work_total))
            if work_unit is not None:
                self.work_unit = str(work_unit)
            if eta_source is not None:
                self.eta_source = str(eta_source)
            if phase_detail is not None:
                self.phase_detail = str(phase_detail)

    def run(self):
        while not self.stop_event.is_set():
            try:
                if not self.engine.state_map:
                    self.stop_event.wait(2)
                    continue
                self.bootstrap_and_train()
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.set_status("error", message=self.error)
                STORE.event(None, "error", "history_manager_error", self.error, {"trace": traceback.format_exc(limit=6)})
            mins = max(5, int(OPTIONS["history_maintenance_minutes"]))
            self.stop_event.wait(mins * 60)

    def _archive_history_payload(self, data, source):
        rows = []
        numeric_min_interval = max(5.0, float(OPTIONS.get("history_context_import_interval_seconds", 60)))
        for group in data or []:
            if not group:
                continue
            group_entity = group[0].get("entity_id")
            last_kept_ts = None
            last_item = None
            for idx, item in enumerate(group):
                eid = item.get("entity_id") or group_entity
                ts = parse_ts(item.get("last_changed") or item.get("last_updated"))
                if not eid or not ts:
                    continue
                keep = True
                # Whole-home context can contain very chatty numeric sensors. For the initial
                # historical archive we keep categorical transitions at full resolution, while
                # numeric context is sampled at a bounded interval. All live entities are still
                # available to the policy immediately, and future changes continue to be archived.
                if source == "ha_history_minimal":
                    raw_state = item.get("state")
                    try:
                        float(raw_state)
                        is_numeric = True
                    except (TypeError, ValueError):
                        is_numeric = False
                    if is_numeric and last_kept_ts is not None and ts - last_kept_ts < numeric_min_interval:
                        keep = False
                if keep:
                    rows.append((eid, ts, item.get("state"), item.get("attributes") or {},
                                 (item.get("context") or {}).get("user_id"), source))
                    last_kept_ts = ts
                last_item = (eid, ts, item)
            # Preserve the final numeric state of a group even when it fell inside the sampling window.
            if source == "ha_history_minimal" and last_item:
                eid, ts, item = last_item
                if last_kept_ts != ts:
                    rows.append((eid, ts, item.get("state"), item.get("attributes") or {},
                                 (item.get("context") or {}).get("user_id"), source))
        return STORE.archive_batch(rows)

    def _fetch_history_resilient(self, entity_ids, start_ts, end_ts, *, minimal, no_attributes, source, depth=0):
        """Fetch Recorder history without allowing one large query to kill bootstrap.

        A Raspberry Pi can legitimately need >45 s for a 10-day multi-entity Recorder query.
        We therefore request short windows and, on timeout/server errors, automatically split
        first by entity list and then by time. Successful pieces are committed immediately,
        so a restart resumes safely thanks to the archive UNIQUE(entity_id, ts) constraint.
        """
        if self.stop_event.is_set() or (self.job_cancel_event and self.job_cancel_event.is_set()):
            raise InterruptedError('History import cancelled')
        memory = rss_mb()
        if memory is not None and memory > 500:
            raise MemoryError('500 MB training memory limit reached')
        if not entity_ids or end_ts <= start_ts:
            return 0
        try:
            data = HA.history(
                entity_ids, iso_from_ts(start_ts), iso_from_ts(end_ts),
                minimal=minimal, no_attributes=no_attributes,
                # Controllable-device history needs attribute-only changes too (e.g.
                # brightness 30% → 50% while light.state stays "on"). Using
                # significant_changes_only here hid exactly the activity required for
                # auto-discovery in v0.3.3. Whole-home context remains significant-only.
                significant=(source != "ha_history_full"), timeout=20,
            ) or []
            return self._archive_history_payload(data, source)
        except HTTPError as exc:
            # Authentication / malformed-request errors will not improve by splitting.
            if getattr(exc, "code", 500) in (400, 401, 403, 404):
                raise
            err = exc
        except (URLError, TimeoutError, OSError) as exc:
            err = exc

        span = end_ts - start_ts
        # Split the entity set first. This is normally enough for busy HA databases.
        if len(entity_ids) > 1:
            mid = max(1, len(entity_ids) // 2)
            if depth <= 8:
                return (
                    self._fetch_history_resilient(entity_ids[:mid], start_ts, end_ts,
                                                   minimal=minimal, no_attributes=no_attributes,
                                                   source=source, depth=depth + 1)
                    + self._fetch_history_resilient(entity_ids[mid:], start_ts, end_ts,
                                                     minimal=minimal, no_attributes=no_attributes,
                                                     source=source, depth=depth + 1)
                )
        # A single very chatty entity can still be expensive; reduce its time window.
        if span > 1800 and depth <= 14:
            mid_ts = start_ts + span / 2.0
            return (
                self._fetch_history_resilient(entity_ids, start_ts, mid_ts,
                                               minimal=minimal, no_attributes=no_attributes,
                                               source=source, depth=depth + 1)
                + self._fetch_history_resilient(entity_ids, mid_ts, end_ts,
                                                 minimal=minimal, no_attributes=no_attributes,
                                                 source=source, depth=depth + 1)
            )

        # Do not abort the whole import for one pathological half-hour slice.
        label = ",".join(entity_ids[:2]) + ("…" if len(entity_ids) > 2 else "")
        msg = f"Skipped one Recorder slice after repeated timeouts: {label} ({type(err).__name__}: {err})"
        print(f"[history] {msg}", flush=True)
        STORE.event(None, "warning", "history_slice_skipped", msg, {
            "entities": entity_ids, "start": iso_from_ts(start_ts), "end": iso_from_ts(end_ts)
        })
        return 0

    @staticmethod
    def _time_windows(start_ts, end_ts, max_hours=24):
        step = max(1, int(max_hours)) * 3600.0
        cursor = float(end_ts)
        while cursor > start_ts:
            previous = max(float(start_ts), cursor-step)
            yield previous, cursor
            cursor = previous

    def _import_section(self, entity_ids, start_ts, end_ts, *, batch_size, minimal, no_attributes, source,
                        progress_lo, progress_hi, label, max_hours=24, parallel_requests=None, inter_chunk_pause_ms=0,
                        on_chunk=None):
        if not entity_ids or end_ts <= start_ts:
            return 0
        windows = self._time_windows(start_ts, end_ts, max_hours=max_hours)
        batches = [entity_ids[i:i + batch_size] for i in range(0, len(entity_ids), batch_size)]
        window_count = max(1, math.ceil((end_ts-start_ts)/(max(1,int(max_hours))*3600)))
        tasks = ((ws, we, batch, wi, bi)
                 for wi, (ws, we) in enumerate(windows, start=1)
                 for bi, batch in enumerate(batches, start=1))
        total = max(1, window_count * len(batches))
        inserted = 0
        done = 0
        workers = 1

        def fetch(task):
            ws, we, batch, wi, bi = task
            count = self._fetch_history_resilient(
                batch, ws, we, minimal=minimal, no_attributes=no_attributes, source=source
            )
            return count, wi, bi, batch

        iterator = map(fetch, tasks)
        pool = None

        stage_started = now_ts()
        try:
            for count, wi, bi, batch in iterator:
                if self.stop_event.is_set():
                    break
                inserted += count
                done += 1
                frac = done / total
                elapsed = max(0.001, now_ts() - stage_started)
                self.history_rows_per_second = inserted / elapsed
                stage_eta = (elapsed / done) * (total - done) if done else None
                self.set_status(
                    progress=progress_lo + (progress_hi - progress_lo) * frac,
                    message=(f"{label}: chunk {done}/{total} · "
                             f"window {wi}/{window_count} · batch {bi}/{len(batches)}"),
                    chunk_done=done, chunk_total=total, stage_eta_seconds=stage_eta,
                    work_done=done, work_total=total, work_unit="Recorder chunks",
                    eta_source="measured chunk throughput",
                    phase_detail=f"Recorder: {done}/{total} chunks complete",
                )
                if on_chunk is not None:
                    try:
                        on_chunk(batch)
                    except Exception as exc:
                        print(f"[history] on_chunk callback failed: {exc}", flush=True)
                if workers == 1 and inter_chunk_pause_ms and not self.stop_event.is_set():
                    # Yield CPU/IO to Home Assistant between background Recorder chunks.
                    # This deliberately trades background completion time for a responsive HA UI.
                    self.stop_event.wait(max(0.0, float(inter_chunk_pause_ms)) / 1000.0)
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        return inserted

    def _manual_lightweight_cycle(self, current, controllable, end_ts):
        start_ts = end_ts - float(OPTIONS["history_bootstrap_days"]) * 86400.0
        last = parse_ts(STORE.meta_get("manual_discovery_refresh"))
        if last:
            refresh_start = max(end_ts - 2 * 3600.0, float(last) - 300.0)
        else:
            refresh_start = max(start_ts, end_ts - max(6.0, float(OPTIONS.get("manual_discovery_hours", 24))) * 3600.0)
        self.set_status(
            "manual_ready", 0.10,
            "Low-memory discovery: refreshing controllable-device history only",
            phase_detail="Cold-start agents train automatically through the single-job FIFO; whole-home context stays idle",
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
        waiting = len([
            a for a in STORE.list_agents()
            if a.get("enabled") and a.get("training_state") in ("waiting", "paused", "needs_retrain")
        ])
        self.set_status(
            "ready", 1.0,
            f"Low-memory mode ready · {q} trained / {waiting} waiting or queued",
            stage_eta_seconds=0, work_done=0, work_total=0, work_unit="agents",
            eta_source="idle",
            phase_detail="Initial training is queued automatically; only one heavy training job runs at a time",
        )

    def bootstrap_and_train(self):
        with self.engine.lock:
            current = dict(self.engine.state_map)
        if not current:
            return

        # Device-level actuator exclusion needs Entity Registry device_id metadata. On a
        # healthy realtime connection this normally arrives immediately; give it a brief
        # head start before rebuilding the v0.7.4 policy schemas. Direct controllable
        # entities are filtered even if registry metadata is unavailable.
        if self.engine.ws_connected:
            deadline = time.monotonic() + 5.0
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                with self.engine.lock:
                    if self.engine.entity_registry:
                        break
                self.stop_event.wait(0.10)

        self.start_cycle()
        end_ts = now_ts()
        controllable = [eid for eid, st in current.items() if target_options_for_state(st)]
        self.discovered_controllable = len(controllable)

        training_rebuild = STORE.meta_get("training_revision", "") != TRAINING_REVISION

        # Read existing automations as a feature prior. This never creates rewards or
        # labels; it only identifies upstream trigger/condition entities likely to matter.
        if OPTIONS.get("automation_scan_enabled", True):
            last_scan = AUTOMATION_KNOWLEDGE.status().get("last_scan") or 0
            if training_rebuild or now_ts() - float(last_scan) > 900:
                self.set_status("automation_scan", 0.005, "Reading Home Assistant automations for predictive context hints")
                with self.engine.lock:
                    registry = dict(self.engine.entity_registry)
                AUTOMATION_KNOWLEDGE.scan(current, registry)

        if HEAVY_JOBS.acquire('discovery'):
            try:
                self._manual_lightweight_cycle(current, controllable, end_ts)
            finally:
                HEAVY_JOBS.release('discovery')

    def replay_live_feedback_all(self):
        # Deliberately disabled across feature-schema revisions. Raw entity_history is the
        # canonical source for deterministic policy rebuilds.
        return 0

    def usage_for(self, entity_id, prop, start_ts):
        rows = STORE.archive_iter(start_ts=start_ts, entity_ids=[entity_id])
        values = BoundedUsage()
        last = None
        for r in rows:
            v = target_value(archived_state(r), prop)
            if v is None:
                continue
            if last is None or abs(float(v) - float(last)) > 1e-6:
                values.append((r, float(v)))
                last = float(v)
        return values

    def auto_discover_agents(self, current, start_ts, threshold_override=None, update_active=True):
        threshold = int(threshold_override if threshold_override is not None else OPTIONS["auto_agent_min_changes"])
        recent_days = max(float(OPTIONS["auto_agent_recent_days"]), min(30.0, float(OPTIONS["history_bootstrap_days"])))
        recent_cutoff = now_ts() - recent_days * 86400
        active = 0
        eligible = 0
        filtered_config = 0
        inactive = 0
        created = 0
        max_agents = max(1, int(OPTIONS.get("max_auto_agents", 250)))
        # Remove only stale auto-created agents that the Entity Registry now identifies
        # as configuration/diagnostic/hidden/disabled. Manual agents are never touched.
        existing = STORE.list_agents()
        cleaned = 0
        for old_agent in list(existing):
            if not old_agent.get("auto_created"):
                continue
            reg = self.engine.registry_entry(old_agent["target_entity"]) or {}
            invalid = (
                reg.get("disabled_by") is not None
                or reg.get("hidden_by") is not None
                or reg.get("entity_category") in ("config", "diagnostic")
            )
            if invalid:
                STORE.set_training_state(old_agent['id'], 'paused', detail={'reason': 'Registry marks this target as config/diagnostic/hidden/disabled'})
                self.engine.models.pop(old_agent["id"], None)
                self.engine.runtime.pop(old_agent["id"], None)
                cleaned += 1
        if cleaned:
            STORE.event(None, "info", "auto_agent_cleanup",
                        f"Paused {cleaned} auto-agent(s) for config/diagnostic/hidden entities; data retained",
                        {"removed": cleaned})
        existing = STORE.list_agents()
        # One primary policy per physical/logical controllable entity. Manual agents also
        # suppress auto-creation for that entity so discovery cannot create duplicates.
        existing_entities = {a["target_entity"] for a in existing}
        for entity_id, state in current.items():
            opts = target_options_for_state(state)
            if not opts:
                continue
            reg = self.engine.registry_entry(entity_id) or {}
            if reg.get("disabled_by") is not None or reg.get("hidden_by") is not None or reg.get("entity_category") in ("config", "diagnostic"):
                filtered_config += 1
                continue
            eligible += 1
            candidates = []
            for opt in opts:
                changes = self.usage_for(entity_id, opt["property"], start_ts)
                last_ts = changes[-1][0]["ts"] if changes else 0
                score = max(0, len(changes) - 1)
                candidates.append((score, last_ts, opt, changes))
            # Existing automations that act on this target are evidence that a device is
            # intentionally controlled, so one observed historical transition is enough
            # to include it in Shadow. It is still trained only from rewards.
            _, automation_infos = AUTOMATION_KNOWLEDGE.hints_for_target(entity_id)
            effective_threshold = 1 if automation_infos else threshold
            candidates.sort(
                key=lambda x: (
                    x[0] >= effective_threshold and x[1] >= recent_cutoff,
                    x[2]["property"] not in ("power", "option_index"),
                    x[0],
                ),
                reverse=True,
            )
            score, last_ts, opt, _ = candidates[0]
            if score < effective_threshold or last_ts < recent_cutoff:
                inactive += 1
                continue
            active += 1
            if entity_id in existing_entities or len(existing_entities) >= max_agents:
                continue
            attrs = state.get("attributes") or {}
            name = attrs.get("friendly_name") or entity_id
            payload = {
                "name": name,
                "mode": "paused",
                "target_entity": entity_id,
                "target_property": opt["property"],
                "min_value": opt["min"], "max_value": opt["max"],
                "deadband": opt["deadband"], "exploration_step": opt["exploration_step"],
                "confidence_threshold": 0.78, "action_interval": default_action_interval(entity_id, opt["property"]),
                "auto_created": True, "micro_exploration": False, "exploration_interval": 21600,
            }
            STORE.create_agent(payload)
            existing_entities.add(entity_id)
            created += 1
        if update_active:
            self.discovered_active = active
            self.discovery_eligible = eligible
            self.discovery_filtered_config = filtered_config
            self.discovery_inactive = inactive
        return created

    def train_from_archive(self, start_ts, end_ts, **kwargs):
        ids = set(kwargs.get('agent_ids') or [a['id'] for a in STORE.list_agent_configs()])
        # A process crash may have committed audit rows before the model checkpoint.
        for aid in ids:
            STORE.discard_uncommitted_experiences(aid)
        try:
            return self._train_from_archive(start_ts, end_ts, **kwargs)
        except Exception:
            for aid in ids:
                STORE.discard_uncommitted_experiences(aid)
                self.engine.models.pop(aid, None)
            raise

    def _train_from_archive(self, start_ts, end_ts, *, qualify=False, agent_ids=None, include_candidates=False, benchmark=None, accumulate_benchmark=False, progress_lo=None, progress_hi=None, progress_label=None):
        benchmark = bool(qualify) if benchmark is None else bool(benchmark)
        agents = [a for a in STORE.list_agents() if a["enabled"]]
        if agent_ids is not None:
            wanted = set(agent_ids)
            agents = [a for a in agents if a["id"] in wanted]
        elif STORE.meta_get("candidate_qualification_complete", "0") == "1" and not qualify and not include_candidates:
            # Once the full-history benchmark has run, only proven agents consume CPU
            # in periodic offline training. PAUSED agents wait for explicit Resume.
            agents = [a for a in agents if a.get("training_state") == "qualified"]
        if not agents:
            return 0
        target_map = {}
        for a in agents:
            target_map.setdefault(a["target_entity"], []).append(a)

        # Feature screening is only needed while the policy schema is unresolved.
        # Once a model checkpoint exists, its explicit schema is authoritative for later
        # chunks/resume. Likewise, an explicitly selected input list (Teach/Correct or a
        # manual agent) does not need another whole-home precursor scan.
        saved_models = {a["id"]: STORE.get_model(a["id"]) for a in agents}
        screen_agents = [
            a for a in agents
            if saved_models.get(a["id"]) is None
            and ("*" in set(a.get("input_entities") or ["*"]))
        ]
        screen_target_map = {}
        for a in screen_agents:
            screen_target_map.setdefault(a["target_entity"], []).append(a)
        screening_required = bool(screen_agents)

        archive_row_count = STORE.archive_count(start_ts=start_ts, end_ts=end_ts)
        if archive_row_count <= 0:
            return 0
        progress_enabled = progress_lo is not None and progress_hi is not None and float(progress_hi) > float(progress_lo)
        progress_label = progress_label or "Historical policy rebuild"
        if progress_enabled:
            self.set_status(
                progress=float(progress_lo),
                message=(
                    f"{progress_label}: screening context candidates"
                    if screening_required else
                    f"{progress_label}: reusing persisted feature schema"
                ),
                work_done=0,
                work_total=archive_row_count if screening_required else 0,
                work_unit="history rows" if screening_required else "schema cache",
                eta_source="measured replay throughput" if screening_required else "persisted schema",
                phase_detail=(
                    "Finding causal precursors and behavioural drivers"
                    if screening_required else
                    "Historical feature screening skipped: persisted/explicit schema is already authoritative"
                ),
            )

        # Historical precursor relevance: every usable HA entity is considered. Entities
        # that repeatedly change shortly before a real target action receive a structural
        # boost in that agent's explicit context schema. This is feature selection only,
        # never a reward or supervised label.
        recent_change = {}
        activity_counts = {}
        relevance_raw = {a["id"]: {} for a in screen_agents}
        target_action_counts = {a["id"]: 0 for a in screen_agents}
        last_target_value = {}
        precursor_window = max(300.0, float(OPTIONS.get("temporal_long_seconds", 300)) * 2.0)
        archive_span = max(1.0, float(end_ts) - float(start_ts))
        selection_end = (float(end_ts) - max(1800.0, archive_span * float(OPTIONS.get("confidence_validation_fraction", .2)))) if screening_required else float(start_ts)

        # Build a small edge index for fast-light causal driver discovery.  This does not
        # depend on HA area metadata or direct automation target mapping: every eligible
        # occupancy/presence or radar/AI activity sensor is compared with the real ON/OFF transitions of each
        # fast target.  Only the pre-validation slice participates in feature selection.
        with self.engine.lock:
            discovery_states = dict(self.engine.state_map)
            discovery_registry = dict(self.engine.entity_registry)
        excluded_control, _ = controllable_context_exclusions(discovery_states, discovery_registry)
        excluded_electrical, _ = electrical_context_exclusions(discovery_states, discovery_registry)
        discovery_excluded = excluded_control | excluded_electrical
        fast_agents = [a for a in screen_agents if is_fast_reactive_agent(a)]
        behaviour_candidates = {
            eid for eid, st in discovery_states.items()
            if is_context_candidate_entity(eid, st, discovery_excluded)
            and (entity_capability_tags(eid, st) & {"occupancy", "activity"})
        }
        fast_targets = {a["target_entity"] for a in fast_agents}
        edge_limit = max(8, min(2048, 32768 // max(1, len(behaviour_candidates | fast_targets))))
        fast_edge_rows = {eid: deque(maxlen=edge_limit) for eid in (behaviour_candidates | fast_targets)}

        previous_context = {}
        for row in STORE.archive_iter(start_ts=start_ts, end_ts=end_ts, chunk_size=2000):
            ts = float(row["ts"]); eid = row["entity_id"]
            if ts >= selection_end:
                break
            if eid in fast_edge_rows:
                fast_edge_rows[eid].append(row)
            signature = (row.get("state"), row.get("attributes_json"))
            if previous_context.get(eid) == signature:
                continue
            previous_context[eid] = signature
            activity_counts[eid] = activity_counts.get(eid, 0) + 1
            for agent in screen_target_map.get(eid, []):
                st = archived_state(row)
                val = target_value(st, agent["target_property"])
                if val is None:
                    continue
                prev = last_target_value.get(agent["id"])
                changed = prev is None or abs(float(val) - float(prev)) > max(0.01, float(agent["deadband"]) * 0.05)
                if changed:
                    target_action_counts[agent["id"]] += 1
                    scores = relevance_raw[agent["id"]]
                    for ceid, cts in recent_change.items():
                        if ceid == eid:
                            continue
                        age = ts - cts
                        if is_fast_reactive_agent(agent):
                            agent_window = float(OPTIONS.get("fast_precursor_on_seconds", 8) if float(val) >= 0.5 else OPTIONS.get("fast_precursor_off_seconds", 120))
                        else:
                            agent_window = precursor_window
                        if 0.0 <= age <= agent_window:
                            # Fast lights use a much sharper precursor kernel: a kitchen
                            # sensor one minute old should not outrank the dedicated stair
                            # sensor that just changed. Slow plants keep the broad window.
                            tau = float(OPTIONS.get("fast_recent_change_seconds", 3)) if is_fast_reactive_agent(agent) else max(30.0, precursor_window / 2.0)
                            scores[ceid] = scores.get(ceid, 0.0) + math.exp(-age / max(0.5, tau))
                    # This fan-out can touch hundreds of context entities for one target
                    # edge. Yield before another target edge even if archive_iter has not
                    # yet reached its forced batch checkpoint.
                    TRAINING_BUDGET.checkpoint("context_screen_target_edge")
                    last_target_value[agent["id"]] = float(val)
            recent_change[eid] = ts

        occupancy_edge_index = {
            eid: transition_edges(fast_edge_rows.get(eid) or [], occupancy_state_bool)
            for eid in behaviour_candidates
            if "occupancy" in entity_capability_tags(eid, discovery_states.get(eid) or {})
        }
        activity_candidates = {
            eid for eid in behaviour_candidates
            if "activity" in entity_capability_tags(eid, discovery_states.get(eid) or {})
        }
        fast_driver_scores = {}
        for agent in fast_agents:
            midpoint = (float(agent["min_value"]) + float(agent["max_value"])) / 2.0
            target_edges = transition_edges(
                fast_edge_rows.get(agent["target_entity"]) or [],
                lambda st, a=agent, mid=midpoint: (None if target_value(st, a["target_property"]) is None else target_value(st, a["target_property"]) >= mid),
            )
            scores = {}
            for eid, sensor_edges in occupancy_edge_index.items():
                score = balanced_presence_driver_score(sensor_edges, target_edges)
                if score > 0:
                    scores[eid] = score
            for eid in activity_candidates:
                score = numeric_activity_driver_score(fast_edge_rows.get(eid) or [], target_edges)
                if score > 0:
                    scores[eid] = max(scores.get(eid, 0.0), score)
            fast_driver_scores[agent["id"]] = scores

        for agent in screen_agents:
            raw = relevance_raw.get(agent["id"], {})
            actions_n = max(1, int(target_action_counts.get(agent["id"], 0)))
            adjusted = {}
            for eid, hit_score in raw.items():
                # hit_strength is roughly the share of target actions preceded by this
                # entity, weighted towards close-in-time changes. expected_presence is the
                # chance of seeing at least one change in the precursor window solely from
                # its normal reporting rate (Poisson approximation). Their ratio is a
                # simple temporal lift estimate.
                hit_strength = clamp(float(hit_score) / actions_n, 0.0, 1.0)
                rate = float(activity_counts.get(eid, 0)) / archive_span
                # With exponentially distributed background updates and the same
                # exp(-age/tau) precursor kernel, E[kernel] = lambda/(lambda+1/tau).
                # This is much stricter than merely asking whether an update happened
                # somewhere inside the window and strongly suppresses high-rate sensors.
                tau = float(OPTIONS.get("fast_recent_change_seconds", 3)) if is_fast_reactive_agent(agent) else max(30.0, precursor_window / 2.0)
                expected_recency = clamp(rate / max(rate + 1.0 / max(0.5, tau), 1e-9), 0.0, 1.0)
                lift = hit_strength / max(0.03, expected_recency)
                specificity = clamp((lift - 1.0) / 2.0, 0.0, 1.0)
                adjusted[eid] = hit_strength * (0.10 + 0.90 * specificity)
            peak = max(adjusted.values()) if adjusted else 0.0
            normalized = ({k: v / peak for k, v in adjusted.items()} if peak > 0 else {})
            # Edge association is deliberately merged *after* generic normalization so a
            # true one-sensor behavioural rule can reach relevance ~=1 even when geography/name based
            # precursor heuristics missed it.  This fixes ESPHome LD24xx presence radars
            # used through group/device/script automations.
            for eid, driver_score in (fast_driver_scores.get(agent["id"]) or {}).items():
                normalized[eid] = max(float(normalized.get(eid, 0.0)), float(driver_score))
            self.engine.context_relevance[agent["id"]] = normalized
            # If this training revision is being rebuilt, force schema creation after the
            # precursor scores are known.
            if STORE.get_model(agent["id"]) is None:
                self.engine.models.pop(agent["id"], None)

        if progress_enabled:
            screening_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.20
            self.set_status(
                progress=screening_end,
                message=(
                    f"{progress_label}: context screening complete; replaying recorded behaviour"
                    if screening_required else
                    f"{progress_label}: persisted feature schema reused; replaying recorded behaviour"
                ),
                work_done=0,
                work_total=archive_row_count,
                work_unit="history rows",
                eta_source="measured replay throughput",
                phase_detail=(
                    f"Screened context for {len(screen_agents)} unresolved-schema agent(s); starting chronological replay"
                    if screening_required else
                    "No full context rescan required for this checkpoint; starting chronological replay"
                ),
            )
        policies = {a["id"]: self.engine.policy(a) for a in agents}
        automation_infos_by_agent = {
            a["id"]: list(AUTOMATION_KNOWLEDGE.hints_for_target(a["target_entity"])[1] or [])
            for a in agents
        }
        benchmark_stats = {}
        for a in agents:
            prior = ((STORE.get_model(a['id']) or {}).get('_benchmark_counts') or (a.get("benchmark_detail") or {}).get("counts") or {}) if accumulate_benchmark else {}
            benchmark_stats[a["id"]] = {
                "samples": int(prior.get("samples") or 0),
                "correct": int(prior.get("correct") or 0),
                "per_action": {str(k): {"samples": int(v.get("samples") or 0), "correct": int(v.get("correct") or 0)}
                               for k, v in (prior.get("per_action") or {}).items()},
                "automation_rules": max(int(prior.get("automation_rules") or 0), len(automation_infos_by_agent[a["id"]])),
                "origin_counts": {str(k): int(v or 0) for k, v in (prior.get("origin_counts") or {}).items()},
            }


        # One chronological replay cursor per prediction horizon. A 30 s head sees the
        # house exactly as it looked 30 s before the historical action; a 5 min HVAC head
        # sees the state 5 min earlier. All heads receive the same reward for the action.
        horizons = sorted({h for p in policies.values() for h in p.horizons})
        watched_entities = {eid for p in policies.values() for eid in p.schema.entities}
        replay_entities = set(watched_entities) | set(target_map.keys())
        rows = STORE.archive_iter(start_ts, end_ts, replay_entities, chunk_size=256)
        timeline = SQLiteTemporalTracker(STORE, watched_entities, self.engine.context, start_ts, end_ts)
        trackers = {h: timeline for h in horizons}
        pending = {}
        last_value = {}
        new_count = 0
        heldout_updates = DeferredUpdates(policies)
        validation_fraction = clamp(float(OPTIONS.get("confidence_validation_fraction", 0.20)), 0.05, 0.40)
        validation_span = max(1800.0, (float(end_ts) - float(start_ts)) * validation_fraction)
        validation_start = max(float(start_ts), float(end_ts) - validation_span)

        def _primary_occupancy_sensor(policy):
            meta = policy.selection_meta or {}
            return meta.get("primary_occupancy_sensor") or meta.get("primary_local_sensor") or next(iter(meta.get("primary_local_sensors") or []), None)

        def _fast_anchor(agent, policy, action_value, action_ts):
            if not is_fast_reactive_agent(agent):
                return float(action_ts), None
            positive = float(action_value) >= 0.5
            primary = _primary_occupancy_sensor(policy)
            local_ts = None
            if primary:
                window = float(OPTIONS.get("fast_precursor_on_seconds", 8) if positive else OPTIONS.get("fast_precursor_off_seconds", 120))
                local_ts = timeline.directional_transition_before(primary, action_ts, positive, window)
            upstream_ts = None
            if positive:
                for eid in (policy.selection_meta or {}).get("upstream_sensors") or []:
                    ts = timeline.directional_transition_before(eid, action_ts, True, float(OPTIONS.get("fast_precursor_on_seconds", 8)))
                    if ts is not None and (upstream_ts is None or ts > upstream_ts):
                        upstream_ts = ts
            return float(local_ts if local_ts is not None else action_ts), upstream_ts

        def _effective_fast_dwell_end(agent, policy, action_value, start_ts, end_ts):
            if not is_fast_reactive_agent(agent):
                return float(end_ts)
            primary = _primary_occupancy_sensor(policy)
            if not primary:
                return float(end_ts)
            # Stop reinforcing ON as soon as the dedicated local occupancy sensor becomes
            # vacant, and stop reinforcing OFF when it becomes occupied. This removes
            # inherited one-minute automation delays from desired-state learning.
            opposite_positive = float(action_value) < 0.5
            edge = timeline.first_directional_transition_after(primary, start_ts, end_ts, opposite_positive)
            return min(float(end_ts), float(edge)) if edge is not None else float(end_ts)

        def learn_or_validate(policy, horizon, action_idx, features, reward, sample_ts):
            head = policy.heads[int(horizon)]
            if float(sample_ts) >= validation_start:
                head.validate(action_idx, features, reward, sample_ts)
                heldout_updates.append((policy, int(horizon), int(action_idx), features, float(reward), float(sample_ts)))
            else:
                policy.update(horizon, action_idx, features, reward, sample_ts)

        def record_behavior_benchmark(agent, policy, old, reward):
            """Chronological held-out benchmark against the target's recorded behaviour.

            v0.7.7 required every scored transition to be attributable to a directly
            discovered Home Assistant automation. That can collapse a genuinely good
            light policy to 0% when the rule reaches the lamp through a group, device
            target, helper, script, or when Recorder no longer exposes enough provenance.

            The authoritative benchmark is therefore the *observed target behaviour*:
            what state the device actually entered under the historical context. Existing
            automations remain the strongest structural prior for feature selection and
            are counted for diagnostics, but they are no longer a gate for whether a
            held-out transition is scoreable. Explicit user changes are also legitimate
            preference evidence and are included rather than silently discarded.

            Binary targets use balanced per-action accuracy so an always-OFF policy still
            cannot qualify merely because OFF dominates wall-clock time.
            """
            if not benchmark or float(reward) <= 0.0:
                return
            if float(old.get("anchor_ts", old["ts"])) < validation_start:
                return
            h = min(old["features_by_horizon"])
            features = old["features_by_horizon"][h]
            head = policy.heads[int(h)]
            arms = head.evaluate(features)
            predicted = max(arms, key=lambda a: a["mean"])["index"]
            actual = int(old["action_idx"])
            predicted_value = float(policy.actions[predicted])
            actual_value = float(policy.actions[actual])
            if len(policy.actions) <= 2 or agent.get("target_property") == "power":
                correct = predicted == actual
            else:
                tolerance = max(float(agent.get("deadband") or 0.0), (float(agent["max_value"]) - float(agent["min_value"])) * 0.03)
                correct = abs(predicted_value - actual_value) <= tolerance
            stat = benchmark_stats[agent["id"]]
            stat["samples"] += 1
            stat["correct"] += 1 if correct else 0
            slot = stat["per_action"].setdefault(str(actual), {"samples": 0, "correct": 0})
            slot["samples"] += 1
            slot["correct"] += 1 if correct else 0
            infos = automation_infos_by_agent.get(agent["id"]) or []
            origin = "manual" if old.get("user_id") else ("automation_assisted" if infos else "anonymous_external")
            stat["origin_counts"][origin] = int(stat["origin_counts"].get(origin) or 0) + 1

        def replay_completed_dwell(agent, policy, old, end_time, next_user_id=None):
            """Train desired-state value, not merely the next transition.

            The transition onset remains useful for anticipation: context at t-H learns
            the state that becomes desirable at t. Stable dwells additionally contribute
            a few contexts from *inside* the dwell, so presence/lux/etc. while a light is
            ON reinforce ON rather than making ON look like a precursor to OFF.
            """
            nonlocal new_count
            effective_end = _effective_fast_dwell_end(agent, policy, old["action_value"], old["ts"], end_time)
            dwell = max(0.0, float(effective_end) - old["ts"])
            reward = historical_reward(agent, dwell, old.get("user_id"), next_user_id)
            primary_h = min(old["features_by_horizon"])
            inserted = STORE.add_historical_experience(
                agent["id"], old["history_id"], old["action_idx"], old["action_value"], reward,
                dwell, old["features_by_horizon"][primary_h], old.get("user_id")
            )
            if not inserted:
                return False

            # Score the held-out transition before it is folded into training. This is
            # the behavioural benchmark against the legacy HA automations.
            record_behavior_benchmark(agent, policy, old, reward)

            # 1) Onset samples. Context is taken at the action boundary. The runtime
            # is event-driven, so it can reach this same environmental context as soon
            # as a precursor state_changed event arrives, before a human acts.
            for h, features in old["features_by_horizon"].items():
                if old["ts"] < validation_start <= effective_end:
                    continue  # purge rewards whose outcomes cross the validation boundary
                learn_or_validate(policy, h, old["action_idx"], features, reward, old.get("anchor_ts", old["ts"]))

            # Optional upstream ON cue: neighbouring-room sensors may legitimately fire
            # a few seconds before the dedicated local sensor. Teach that cue weakly so
            # it can accelerate ON, but never let it define OFF/occupancy persistence.
            for h, features in (old.get("upstream_features_by_horizon") or {}).items():
                if old.get("upstream_anchor_ts", old["ts"]) >= validation_start:
                    heldout_updates.append((policy, int(h), old["action_idx"], features, float(reward) * 0.35, float(old["ts"])))
                else:
                    policy.update(h, old["action_idx"], features, float(reward) * 0.35, old["ts"])

            # 2) Persistence samples: learn what should remain true while the state is
            # accepted. Limit to three samples/head so a six-hour dwell cannot dominate
            # the policy merely because it lasted longer. Only sample contexts that are
            # safely inside the dwell for that prediction horizon.
            settle = min(20.0, max(3.0, float(OPTIONS.get("temporal_short_seconds", 60)) * 0.10))
            for h in policy.horizons:
                h = int(h)
                earliest_target = old["ts"] + h + settle
                latest_target = float(effective_end) - settle
                if latest_target <= earliest_target:
                    continue
                span = latest_target - earliest_target
                target_times = [earliest_target]
                if span >= max(20.0, h * 0.5):
                    target_times.append(earliest_target + span * 0.5)
                if span >= max(60.0, h):
                    target_times.append(latest_target)
                tracker = trackers[h]
                seen = set()
                for target_time in target_times:
                    context_ts = target_time
                    key = int(context_ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    tracker.advance(context_ts)
                    features, _, _ = policy.features(tracker.state_map, tracker.history, at_ts=context_ts)
                    if target_time < validation_start <= effective_end:
                        continue
                    if target_time >= validation_start:
                        # Correlated samples within a dwell are training evidence,
                        # not independent validation trials. Validate onset only.
                        heldout_updates.append((policy, h, old["action_idx"], features, float(reward), float(target_time)))
                    else:
                        policy.update(h, old["action_idx"], features, reward, target_time)

            new_count += 1
            return True

        replay_started = now_ts()
        replay_last_report = replay_started
        replay_total = max(1, STORE.archive_count(start_ts, end_ts, replay_entities))
        replay_done = 0
        if progress_enabled:
            screening_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.20
            replay_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.90
        for row in rows:
            if replay_done % 256 == 0:
                memory = rss_mb()
                if self.stop_event.is_set() or (memory is not None and memory > 500):
                    raise InterruptedError("Training interrupted or 500 MB memory limit reached")
                self.stop_event.wait(.001)
            replay_done += 1
            now_report = now_ts()
            if progress_enabled and (replay_done == replay_total or replay_done % 5000 == 0 or now_report - replay_last_report >= 2.0):
                elapsed_replay = max(0.01, now_report - replay_started)
                rate = replay_done / elapsed_replay
                self.training_rows_per_second = rate
                remaining = (replay_total - replay_done) / max(rate, 1e-9)
                frac = replay_done / replay_total
                p = screening_end + (replay_end - screening_end) * frac
                self.set_status(progress=p, message=f"{progress_label}: replay {replay_done:,}/{replay_total:,} archived state changes",
                                stage_eta_seconds=remaining, work_done=replay_done, work_total=replay_total,
                                work_unit="history rows", eta_source="measured replay throughput",
                                phase_detail=f"Chronological reward replay for {len(agents)} agent(s) · {rate:,.0f} rows/s")
                replay_last_report = now_report
            agents_for_target = target_map.get(row["entity_id"], [])
            if not agents_for_target:
                continue
            st = archived_state(row)
            for agent in agents_for_target:
                aid = agent["id"]
                policy = policies[aid]
                value = target_value(st, agent["target_property"])
                if value is None:
                    continue
                prev = last_value.get(aid)
                if prev is not None and abs(float(value) - float(prev)) <= max(0.01, float(agent["deadband"]) * 0.05):
                    continue

                if aid in pending:
                    old = pending[aid]
                    replay_completed_dwell(agent, policy, old, float(row["ts"]), row.get("context_user_id"))

                features_by_horizon = {}
                anchor_ts, upstream_anchor_ts = _fast_anchor(agent, policy, value, float(row["ts"]))
                for h in policy.horizons:
                    tracker = trackers[h]
                    tracker.advance(anchor_ts)
                    features, _, _ = policy.features(tracker.state_map, tracker.history, at_ts=anchor_ts)
                    features_by_horizon[h] = features
                upstream_features_by_horizon = {}
                if upstream_anchor_ts is not None and upstream_anchor_ts < anchor_ts and anchor_ts - upstream_anchor_ts <= float(OPTIONS.get("fast_upstream_lead_seconds", 4)):
                    for h in policy.horizons:
                        tracker = trackers[h]
                        tracker.advance(upstream_anchor_ts)
                        features, _, _ = policy.features(tracker.state_map, tracker.history, at_ts=upstream_anchor_ts)
                        upstream_features_by_horizon[h] = features

                if is_fast_reactive_agent(agent) and value >= .5:
                    for h in policy.horizons:
                        tracker = trackers[h]
                        tracker.advance(anchor_ts - max(1, h))
                        early, _, early_meta = policy.features(tracker.state_map, tracker.history, at_ts=anchor_ts-max(1,h))
                        forecast = early_meta.get('home_forecast', {})
                        if forecast.get('arrival_probability', 0) > .1 and forecast.get('occupancy_now', 0) < .5:
                            upstream_features_by_horizon[h] = early
                            upstream_anchor_ts = anchor_ts-max(1,h)
                actions = policy.actions
                action_idx = min(range(len(actions)), key=lambda i: abs(actions[i] - float(value)))
                pending[aid] = {
                    "history_id": row["id"], "ts": float(row["ts"]), "anchor_ts": anchor_ts,
                    "upstream_anchor_ts": upstream_anchor_ts, "features_by_horizon": features_by_horizon,
                    "upstream_features_by_horizon": upstream_features_by_horizon,
                    "action_idx": action_idx, "action_value": actions[action_idx], "user_id": row.get("context_user_id"),
                }
                last_value[aid] = float(value)

        for agent in agents:
            aid = agent["id"]
            policy = policies[aid]
            old = pending.get(aid)
            # The last open dwell is right-censored: wait for its actual end.
            # Otherwise its unique history id freezes a premature reward forever.
            if old:
                pass

        if progress_enabled:
            progress_span = float(progress_hi) - float(progress_lo)
            replay_end = float(progress_lo) + progress_span * 0.90
            validation_end = float(progress_lo) + progress_span * 0.92
            model_end = float(progress_lo) + progress_span * 0.96
            benchmark_end = float(progress_lo) + progress_span * 0.98
            self.set_status(progress=replay_end, message=f"{progress_label}: replay complete; yielding before finalization",
                            stage_eta_seconds=0, work_done=replay_total, work_total=replay_total,
                            work_unit="history rows", eta_source="bounded finalization",
                            phase_detail=f"Replay complete · {new_count:,} new rewarded experiences")
        else:
            validation_end = model_end = benchmark_end = None

        # 0.14.18: replay used to stop at exactly this point and the next expensive
        # finalization work ran outside the archive-iterator throttle. Force a yield
        # before any serialization/benchmark/qualification work begins.
        TRAINING_BUDGET.checkpoint("replay_complete", force=True)

        # The newest slice was held out while confidence was calibrated. Once its
        # out-of-sample score is recorded, fold it into the final policy so no history is
        # wasted. Calibration remains a genuine chronological backtest.
        for policy, horizon, action_idx, features, reward, sample_ts in heldout_updates:
            policy.update(horizon, action_idx, features, reward, sample_ts)
            TRAINING_BUDGET.checkpoint("heldout_update")

        if progress_enabled:
            self.set_status(progress=validation_end, message=f"{progress_label}: serializing policy model",
                            stage_eta_seconds=0, work_done=replay_total, work_total=replay_total,
                            work_unit="finalization", eta_source="bounded finalization",
                            phase_detail="Replay complete · applying bounded model checkpoint")

        for agent in agents:
            TRAINING_BUDGET.checkpoint("before_policy_serialize")
            policy = policies[agent["id"]]
            exported = policy.serialize()
            TRAINING_BUDGET.checkpoint("after_policy_serialize")
            exported['_benchmark_counts'] = benchmark_stats.get(agent['id'], {})
            STORE.save_model(agent["id"], exported)
            TRAINING_BUDGET.checkpoint("after_model_save")

        if progress_enabled:
            self.set_status(progress=model_end, message=f"{progress_label}: saving held-out benchmark",
                            stage_eta_seconds=0, work_done=replay_total, work_total=replay_total,
                            work_unit="finalization", eta_source="bounded finalization",
                            phase_detail="Policy model saved · finalizing benchmark")

        if benchmark:
            for agent in agents:
                TRAINING_BUDGET.checkpoint("before_partial_benchmark")
                STORE.set_partial_benchmark(agent["id"], benchmark_stats.get(agent["id"]) or {})
                TRAINING_BUDGET.checkpoint("after_partial_benchmark")

        if progress_enabled:
            self.set_status(progress=benchmark_end, message=f"{progress_label}: final qualification checks",
                            stage_eta_seconds=0, work_done=replay_total, work_total=replay_total,
                            work_unit="finalization", eta_source="bounded finalization",
                            phase_detail="Benchmark saved · checking qualification")

        qualification_summary = None
        if qualify:
            threshold = clamp(float(OPTIONS.get("candidate_benchmark_threshold", 0.78)), 0.0, 1.0)
            min_samples = max(1, int(OPTIONS.get("candidate_benchmark_min_samples", 12)))
            qualified_count = 0
            paused_count = 0
            for agent in agents:
                TRAINING_BUDGET.checkpoint("qualification_agent")
                stat = benchmark_stats.get(agent["id"]) or {}
                samples = int(stat.get("samples") or 0)
                per_action = stat.get("per_action") or {}
                per_action_accuracy = {
                    key: (float(v.get("correct") or 0) / max(1, int(v.get("samples") or 0)))
                    for key, v in per_action.items() if int(v.get("samples") or 0) > 0
                }
                binary = len(policies[agent["id"]].actions) <= 2 or agent.get("target_property") == "power"
                class_coverage = (len(per_action_accuracy) >= 2) if binary else bool(per_action_accuracy)
                if binary and per_action_accuracy:
                    # Balanced transition accuracy: ON and OFF count equally. An agent
                    # that simply predicts the dominant OFF state cannot pass 78%.
                    score = sum(per_action_accuracy.values()) / len(per_action_accuracy)
                else:
                    score = float(stat.get("correct") or 0) / samples if samples else 0.0
                enough = samples >= min_samples and class_coverage
                passed = bool(enough and score > threshold)
                state = "qualified" if passed else "paused"
                origin_counts = {str(k): int(v or 0) for k, v in (stat.get("origin_counts") or {}).items()}
                automation_rules = int(stat.get("automation_rules") or 0)
                reason = (
                    "recorded-behaviour benchmark passed" if passed else
                    f"insufficient held-out behaviour samples ({samples}/{min_samples})" if not enough else
                    f"recorded-behaviour benchmark {score:.1%} below {threshold:.0%}"
                )
                detail = {
                    "threshold": threshold, "minimum_samples": min_samples,
                    "balanced": bool(binary), "class_coverage": bool(class_coverage),
                    "per_action_accuracy": per_action_accuracy,
                    "automation_rules": automation_rules,
                    "origin_counts": origin_counts,
                    "counts": {"samples": samples, "correct": int(stat.get("correct") or 0),
                               "per_action": stat.get("per_action") or {},
                               "automation_rules": automation_rules,
                               "origin_counts": origin_counts},
                    "reason": reason,
                }
                detail["mode_after_training"] = "shadow"
                detail["control_qualified"] = bool(passed)
                STORE.set_training_state(
                    agent["id"], state, score=score, samples=samples,
                    source="recorded-behaviour", detail=detail, demote_control=True,
                    shadow_after_completion=True,
                )
                STORE.set_training_progress(agent["id"], float(start_ts), float(end_ts), float(end_ts))
                if passed:
                    qualified_count += 1
                else:
                    paused_count += 1
                STORE.event(
                    agent["id"], "info" if passed else "warning",
                    "candidate_qualified" if passed else "candidate_shadow_observing",
                    (
                        f"Behaviour benchmark {score:.1%} over {samples} held-out transition(s): {reason}. "
                        + ("Control qualification passed; Shadow is active."
                           if passed else "Control remains blocked; Shadow stays active to collect future evidence.")
                    ),
                    detail,
                )
            qualification_summary = {
                "qualified": qualified_count, "paused": paused_count,
                "shadow_active": qualified_count + paused_count,
                "threshold": threshold, "minimum_samples": min_samples,
            }
            if qualified_count or paused_count:
                self.engine.wake_event.set()
            if agent_ids is None:
                STORE.meta_set("candidate_qualification_complete", "1")

        TRAINING_BUDGET.checkpoint("before_training_event")
        if new_count or qualification_summary:
            STORE.event(
                None, "info", "offline_rl_training",
                f"Added {new_count} selective temporal RL experiences across {len(horizons)} prediction horizons",
                {"experiences": new_count, "prediction_horizons_seconds": horizons,
                 "feature_dimensions": int(OPTIONS.get("feature_dimensions", 128)),
                 "validation_fraction": validation_fraction, "heldout_updates": len(heldout_updates),
                 "qualification": qualification_summary},
            )
        heldout_updates.close()
        timeline.close()
        TRAINING_BUDGET.checkpoint("finalization_complete", force=True)
        if progress_enabled:
            self.set_status(progress=float(progress_hi), message=f"{progress_label}: training checkpoint complete",
                            stage_eta_seconds=0, work_done=replay_total, work_total=replay_total,
                            work_unit="finalization", eta_source="complete",
                            phase_detail="Replay, model save, benchmark and qualification complete")
        return new_count
