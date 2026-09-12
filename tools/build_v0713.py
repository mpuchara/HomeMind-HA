#!/usr/bin/env python3
from pathlib import Path
import base64
import json
import re
import shutil
import sys
import textwrap
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "_materialized_v0712"
OUT_ROOT = ROOT / "_release_v0713"
OUT = OUT_ROOT / "adaptive_ai"


def must_replace(text, old, new, name):
    if old not in text:
        raise RuntimeError(f"anchor not found: {name}")
    return text.replace(old, new, 1)


def patch_main(path):
    s = path.read_text(encoding="utf-8")
    s = must_replace(s, 'APP_VERSION = "0.7.12"', 'APP_VERSION = "0.7.13"', 'version')
    s = must_replace(s, 'TRAINING_REVISION = "unit-only-all-context-v16"', 'TRAINING_REVISION = "esphome-sensor-context-v17"', 'training revision')

    start = s.index('def controllable_context_exclusions(state_map, registry):')
    end = s.index('\ndef select_context_entities(', start)
    replacement = r'''def is_esphome_sensor_entity(entity_id, registry):
    """True for ESPHome sensor/binary_sensor Entity Registry entries.

    ESPHome devices frequently expose configuration number/select/switch entities next
    to the actual LD24xx presence/radar sensors. Those configuration controls must never
    make the physical sensor channels disappear from the learning universe.
    """
    reg = (registry or {}).get(entity_id) or {}
    platform = str(reg.get("platform") or reg.get("integration") or "").strip().lower()
    domain = str(entity_id or "").split(".", 1)[0]
    return platform == "esphome" and domain in ("sensor", "binary_sensor")


def controllable_context_exclusions(state_map, registry):
    """Exclude actuators without blacklisting useful ESPHome sensing siblings.

    Every directly controllable entity is excluded from every agent input. Device-wide
    sibling exclusion is reserved for *real actuator* domains (light/switch/climate/etc.).
    Configuration-like number/select entities are deliberately NOT allowed to blacklist
    their whole physical ESPHome device. Even on a real ESPHome actuator device,
    sensor/binary_sensor siblings remain eligible; explicit electrical-unit filtering is
    applied separately afterwards.

    This fixes LD2411/LD24xx layouts such as ``binary_sensor.kitchen_presence_presence``
    sharing one ESPHome device with threshold numbers, engineering-mode selects/switches
    and radar diagnostics.
    """
    registry = registry or {}
    controllable_entities = set()
    actuator_devices = set()
    strong_actuator_domains = {
        "light", "switch", "climate", "cover", "fan", "media_player",
        "humidifier", "water_heater",
    }
    for eid, st in (state_map or {}).items():
        try:
            is_controllable = bool(target_options_for_state(st))
        except Exception:
            is_controllable = False
        if not is_controllable:
            continue
        controllable_entities.add(eid)
        domain = str(eid).split(".", 1)[0]
        device_id = (registry.get(eid) or {}).get("device_id")
        if device_id and domain in strong_actuator_domains:
            actuator_devices.add(device_id)

    excluded = set(controllable_entities)
    rescued_esphome_sensors = 0
    if actuator_devices:
        for eid, reg in registry.items():
            if (reg or {}).get("device_id") not in actuator_devices:
                continue
            if eid in controllable_entities:
                continue
            if is_esphome_sensor_entity(eid, registry):
                rescued_esphome_sensors += 1
                continue
            excluded.add(eid)
    return excluded, {
        "detected_controllable_entities": len(controllable_entities),
        "detected_controllable_devices": len(actuator_devices),
        "excluded_controllable_context_entities": len(excluded),
        "esphome_sensor_sibling_overrides": rescued_esphome_sensors,
    }
'''
    s = s[:start] + replacement + s[end:]

    s = must_replace(
        s,
        '    ranked = []\n    considered = 0\n    for eid, st in state_map.items():',
        '    ranked = []\n    considered = 0\n    esphome_candidates = 0\n    for eid, st in state_map.items():',
        'esphome candidate init',
    )
    s = must_replace(
        s,
        '        reg = registry.get(eid) or {}\n        considered += 1\n        if eid == target:',
        '        reg = registry.get(eid) or {}\n        considered += 1\n        if is_esphome_sensor_entity(eid, registry):\n            esphome_candidates += 1\n        if eid == target:',
        'esphome candidate count',
    )
    s = must_replace(
        s,
        '    upstream = [eid for _, eid, reasons, loc in ranked if eid in selected_set and not loc["local"] and ("automation" in reasons or "historical-precursor" in reasons or "causal-behaviour" in reasons)]\n    return selected, {',
        '    upstream = [eid for _, eid, reasons, loc in ranked if eid in selected_set and not loc["local"] and ("automation" in reasons or "historical-precursor" in reasons or "causal-behaviour" in reasons)]\n    esphome_selected = sum(1 for eid in selected if is_esphome_sensor_entity(eid, registry))\n    return selected, {',
        'esphome selected count',
    )
    s = must_replace(
        s,
        '        "fast_local_profile": bool(fast),\n        **exclusion_meta, **electrical_meta,',
        '        "fast_local_profile": bool(fast),\n        "esphome_context_candidates": esphome_candidates,\n        "esphome_selected_context": esphome_selected,\n        **exclusion_meta, **electrical_meta,',
        'esphome selection meta',
    )

    # Richer preparation status fields.
    s = must_replace(
        s,
        '        self.chunk_done = 0\n        self.chunk_total = 0\n        self.agent_jobs = set()',
        '        self.chunk_done = 0\n        self.chunk_total = 0\n        self.work_done = 0\n        self.work_total = 0\n        self.work_unit = None\n        self.eta_source = None\n        self.phase_detail = None\n        self.context_candidate_count = 0\n        self.esphome_candidate_count = 0\n        self.esphome_sensor_sibling_overrides = 0\n        self.agent_jobs = set()',
        'history init status fields',
    )
    s = must_replace(
        s,
        '                "chunk_done": self.chunk_done, "chunk_total": self.chunk_total,\n                "archive": dict(self.archive_cache),',
        '                "chunk_done": self.chunk_done, "chunk_total": self.chunk_total,\n                "work_done": self.work_done, "work_total": self.work_total, "work_unit": self.work_unit,\n                "eta_source": self.eta_source, "phase_detail": self.phase_detail,\n                "context_candidates": self.context_candidate_count,\n                "esphome_context_candidates": self.esphome_candidate_count,\n                "esphome_sensor_sibling_overrides": self.esphome_sensor_sibling_overrides,\n                "archive": dict(self.archive_cache),',
        'history status output',
    )

    old_eligible = '''        excluded_control, _ = controllable_context_exclusions(current, registry)\n        excluded_electrical, _ = electrical_context_exclusions(current, registry)\n        excluded = excluded_control | excluded_electrical\n        out = []\n        for eid, st in current.items():\n            if not is_context_candidate_entity(eid, st, excluded):\n                continue\n            out.append(eid)\n        return sorted(set(out))'''
    new_eligible = '''        excluded_control, control_meta = controllable_context_exclusions(current, registry)\n        excluded_electrical, _ = electrical_context_exclusions(current, registry)\n        excluded = excluded_control | excluded_electrical\n        out = []\n        esphome = 0\n        for eid, st in current.items():\n            if not is_context_candidate_entity(eid, st, excluded):\n                continue\n            out.append(eid)\n            if is_esphome_sensor_entity(eid, registry):\n                esphome += 1\n        out = sorted(set(out))\n        with self.lock:\n            self.context_candidate_count = len(out)\n            self.esphome_candidate_count = esphome\n            self.esphome_sensor_sibling_overrides = int(control_meta.get("esphome_sensor_sibling_overrides") or 0)\n        return out'''
    s = must_replace(s, old_eligible, new_eligible, 'eligible rebuild context')

    # Replace set_status with work-aware version. Keep adaptive ETA, but reset stale ETA on phase changes.
    start = s.index('    def set_status(self, phase=None, progress=None, message=None, chunk_done=None, chunk_total=None, stage_eta_seconds=None):')
    end = s.index('\n    def run(self):', start)
    new_set_status = r'''    def set_status(self, phase=None, progress=None, message=None, chunk_done=None, chunk_total=None,
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
'''
    s = s[:start] + new_set_status + s[end:]

    # Give each Recorder import explicit work counters / source.
    s = must_replace(
        s,
        '                    chunk_done=done, chunk_total=total, stage_eta_seconds=stage_eta,\n                )',
        '                    chunk_done=done, chunk_total=total, stage_eta_seconds=stage_eta,\n                    work_done=done, work_total=total, work_unit="Recorder chunks",\n                    eta_source="measured chunk throughput",\n                    phase_detail=f"Recorder: {done}/{total} chunks complete",\n                )',
        'import section progress metadata',
    )

    # Extend train function with progress range.
    s = must_replace(
        s,
        '    def train_from_archive(self, start_ts, end_ts, *, qualify=False, agent_ids=None, include_candidates=False, benchmark=None, accumulate_benchmark=False):',
        '    def train_from_archive(self, start_ts, end_ts, *, qualify=False, agent_ids=None, include_candidates=False, benchmark=None, accumulate_benchmark=False, progress_lo=None, progress_hi=None, progress_label=None):',
        'train signature',
    )
    s = must_replace(
        s,
        '        rows = STORE.archive_rows(start_ts=start_ts, end_ts=end_ts)\n        if not rows:\n            return 0\n\n        # Historical precursor relevance:',
        '        rows = STORE.archive_rows(start_ts=start_ts, end_ts=end_ts)\n        if not rows:\n            return 0\n        progress_enabled = progress_lo is not None and progress_hi is not None and float(progress_hi) > float(progress_lo)\n        progress_label = progress_label or "Historical policy rebuild"\n        if progress_enabled:\n            span = float(progress_hi) - float(progress_lo)\n            self.set_status(progress=float(progress_lo), message=f"{progress_label}: screening context candidates",\n                            work_done=0, work_total=len(rows), work_unit="history rows",\n                            eta_source="measured replay throughput",\n                            phase_detail="Finding causal precursors and behavioural drivers")\n\n        # Historical precursor relevance:',
        'train progress init',
    )

    # Just before policies: feature screening done.
    s = must_replace(
        s,
        '        policies = {a["id"]: self.engine.policy(a) for a in agents}',
        '        if progress_enabled:\n            screening_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.20\n            self.set_status(progress=screening_end, message=f"{progress_label}: context screening complete; replaying recorded behaviour",\n                            work_done=0, work_total=len(rows), work_unit="history rows",\n                            eta_source="measured replay throughput",\n                            phase_detail=f"Screened context for {len(agents)} agent(s); starting chronological replay")\n        policies = {a["id"]: self.engine.policy(a) for a in agents}',
        'screening checkpoint',
    )

    # Add measured progress in chronological replay.
    s = must_replace(
        s,
        '        for row in rows:\n            agents_for_target = target_map.get(row["entity_id"], [])',
        '        replay_started = now_ts()\n        replay_last_report = replay_started\n        replay_total = max(1, len(rows))\n        replay_done = 0\n        if progress_enabled:\n            screening_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.20\n            replay_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.90\n        for row in rows:\n            replay_done += 1\n            now_report = now_ts()\n            if progress_enabled and (replay_done == replay_total or replay_done % 5000 == 0 or now_report - replay_last_report >= 2.0):\n                elapsed_replay = max(0.01, now_report - replay_started)\n                rate = replay_done / elapsed_replay\n                remaining = (replay_total - replay_done) / max(rate, 1e-9)\n                frac = replay_done / replay_total\n                p = screening_end + (replay_end - screening_end) * frac\n                self.set_status(progress=p, message=f"{progress_label}: replay {replay_done:,}/{replay_total:,} archived state changes",\n                                stage_eta_seconds=remaining, work_done=replay_done, work_total=replay_total,\n                                work_unit="history rows", eta_source="measured replay throughput",\n                                phase_detail=f"Chronological reward replay for {len(agents)} agent(s) · {rate:,.0f} rows/s")\n                replay_last_report = now_report\n            agents_for_target = target_map.get(row["entity_id"], [])',
        'row replay progress',
    )

    # Before final heldout folding/saving, show finalization.
    s = must_replace(
        s,
        '        # The newest slice was held out while confidence was calibrated. Once its',
        '        if progress_enabled:\n            replay_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.90\n            self.set_status(progress=replay_end, message=f"{progress_label}: finalizing held-out benchmark and policy models",\n                            stage_eta_seconds=0, work_done=replay_total, work_total=replay_total,\n                            work_unit="history rows", eta_source="benchmark finalization",\n                            phase_detail=f"Replay complete · {new_count:,} new rewarded experiences")\n\n        # The newest slice was held out while confidence was calibrated. Once its',
        'finalization checkpoint',
    )

    # Make the problematic global rebuild progress measurable instead of freezing at 13%.
    s = must_replace(
        s,
        '                self.trained_new = self.train_from_archive(discovery_start, end_ts, qualify=True, include_candidates=True)',
        '                self.trained_new = self.train_from_archive(discovery_start, end_ts, qualify=True, include_candidates=True,\n                                                                  progress_lo=0.13, progress_hi=0.55,\n                                                                  progress_label="Rebuilding predictive policies")',
        'global rebuild progress call',
    )
    s = must_replace(
        s,
        '            self.trained_new += self.train_from_archive(start_ts, end_ts, qualify=True, include_candidates=True)',
        '            self.trained_new += self.train_from_archive(start_ts, end_ts, qualify=True, include_candidates=True,\n                                                               progress_lo=0.92, progress_hi=0.995,\n                                                               progress_label="Final behaviour benchmark")',
        'bootstrap benchmark progress call',
    )

    path.write_text(s, encoding="utf-8")


def patch_app(path):
    s = path.read_text(encoding="utf-8")
    start = s.index('function renderHistory(h,status={}){')
    end = s.index('\nfunction sensorRecommendations', start)
    new_fn = r'''function renderHistory(h,status={}){
  const ar=h.archive||{}, progress=Math.round((Number(h.progress)||0)*100);
  const eta=duration(h.eta_seconds),stageEta=duration(h.stage_eta_seconds),elapsed=duration(h.elapsed_seconds);
  const workDone=Number(h.work_done||0),workTotal=Number(h.work_total||0),workPct=workTotal?Math.min(100,Math.round(workDone*100/workTotal)):null;
  const phase=String(h.phase||'starting');
  const phases=[
    ['fast_targets','Targets','Find controllable devices'],
    ['fast_context','Context','Import recent house context'],
    ['context_refresh','Sensors','Refresh all eligible sensors'],
    ['rebuilding','Learn','Screen + replay historical behaviour'],
    ['benchmarking','Validate','Held-out behaviour benchmark'],
    ['ready','Ready','Qualified agents enter Shadow'],
  ];
  const phaseAliases={automation_scan:'fast_targets',discovering:'fast_targets',enriching_targets:'fast_targets',ready_enriching:'fast_context',enriching_context:'fast_context',importing:'fast_context',fast_training:'rebuilding',training:'rebuilding'};
  const canonical=phaseAliases[phase]||phase;
  let activeIdx=phases.findIndex(x=>x[0]===canonical);if(activeIdx<0)activeIdx=0;
  const pipeline=phases.map((x,i)=>`<div class="prep-step ${i<activeIdx?'done':i===activeIdx?'active':''}"><i>${i<activeIdx?'✓':i+1}</i><div><b>${esc(x[1])}</b><span>${esc(x[2])}</span></div></div>`).join('');
  const etaPrimary=stageEta?`Current phase ${stageEta}`:(eta?`Adaptive overall estimate ${eta}`:'Calibrating ETA…');
  const workLine=workTotal?`${num(workDone)} / ${num(workTotal)} ${esc(h.work_unit||'items')} · ${workPct}%`:null;
  const ak=status.automation_knowledge||{};
  $('#historyPanel').innerHTML=`
    <div class="history-head"><div><b>${esc(h.message||'Starting history engine…')}</b><span>${esc(phase.toUpperCase())}${h.phase_detail?` · ${esc(h.phase_detail)}`:''}</span></div><div class="history-percent"><strong>${progress}%</strong><small>${esc(etaPrimary)}</small></div></div>
    <div class="bar history-bar"><i style="width:${progress}%"></i></div>
    <div class="prep-pipeline">${pipeline}</div>
    <div class="history-timing"><b>${workLine?esc(workLine):(progress>=100?'Index ready':'Preparing measurable work…')}</b><span>${elapsed?`elapsed ${esc(elapsed)}`:''}${h.eta_source?` · ETA: ${esc(h.eta_source)}`:''}</span></div>
    ${workPct!=null?`<div class="bar work-bar"><i style="width:${workPct}%"></i></div>`:''}
    <div class="history-grid">
      <div><b>${num(ar.n||0)}</b><span>archived state changes</span></div>
      <div><b>${Number(ar.days||0).toFixed(1)} d</b><span>local history coverage</span></div>
      <div><b>${num(h.context_candidates||ar.entities||0)}</b><span>eligible context candidates</span></div>
      <div><b>${num(h.esphome_context_candidates||0)}</b><span>ESPHome sensors eligible</span></div>
      <div><b>${h.active||0}/${h.eligible||h.controllable||0}</b><span>active / eligible targets</span></div>
    </div>
    <div class="history-meta"><span>${h.filtered_config||0} config/diagnostic targets filtered</span><span>${h.inactive||0} insufficient target activity</span><span>${ak.automation_count||0} automations scanned</span><span>${status.realtime?.connected?'Realtime event stream active':'REST fallback active'}</span><span>${h.esphome_sensor_sibling_overrides||0} ESPHome sensor siblings preserved</span>${ak.error?`<span title="${esc(ak.error)}">Automation scan partial</span>`:''}</div>`;
}'''
    s = s[:start] + new_fn + s[end:]

    # Add ESPHome counters to per-agent learning diagnostics.
    anchor = '<b>Context candidates screened:</b> ${num(ctx.considered_entities||0)} · selected ${num(ctx.selected_entities||0)}<br>'
    if anchor in s:
        s = s.replace(anchor, anchor + '<b>ESPHome context:</b> ${num(ctx.esphome_context_candidates||0)} eligible · ${num(ctx.esphome_selected_context||0)} selected · ${num(ctx.esphome_sensor_sibling_overrides||0)} sensor siblings preserved<br>', 1)
    path.write_text(s, encoding="utf-8")


def patch_css(path):
    s = path.read_text(encoding="utf-8")
    s += r'''

/* 0.7.13 preparation telemetry */
.prep-pipeline{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:14px 0 12px}
.prep-step{display:flex;gap:8px;align-items:flex-start;padding:9px 10px;border:1px solid var(--border,#2b3440);border-radius:10px;opacity:.58;background:rgba(255,255,255,.015)}
.prep-step i{font-style:normal;display:grid;place-items:center;min-width:22px;height:22px;border-radius:50%;border:1px solid currentColor;font-size:11px}
.prep-step b,.prep-step span{display:block}.prep-step b{font-size:12px}.prep-step span{font-size:10px;line-height:1.25;margin-top:2px;color:var(--muted,#9aa7b6)}
.prep-step.done{opacity:.82}.prep-step.done i{color:#62e69a}.prep-step.active{opacity:1;border-color:#58c7ff;box-shadow:0 0 0 1px rgba(88,199,255,.13) inset}.prep-step.active i{color:#58c7ff}
.work-bar{height:5px;margin-top:8px;opacity:.9}
.history-grid{grid-template-columns:repeat(5,minmax(0,1fr))!important}
@media(max-width:900px){.prep-pipeline{grid-template-columns:repeat(2,minmax(0,1fr))}.history-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}}
'''
    path.write_text(s, encoding="utf-8")


def write_addon_files():
    # Runtime source is now normal readable source in the install package.
    docker = '''ARG BUILD_VERSION=0.7.13\nARG BUILD_ARCH=amd64\nFROM python:3.13-alpine\nARG BUILD_VERSION\nARG BUILD_ARCH\nLABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}"\nWORKDIR /app\nCOPY src/ /app/\nRUN python -m py_compile /app/main.py /app/control.py \\\n    && pip install --no-cache-dir websockets==15.0.1 \\\n    && chmod +x /app/run.sh\nEXPOSE 8099\nCMD ["/app/run.sh"]\n'''
    (OUT / "Dockerfile").write_text(docker, encoding="utf-8")

    cfg = (ROOT / "adaptive_ai" / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace('version: "0.7.12"', 'version: "0.7.13"', 1)
    (OUT / "config.yaml").write_text(cfg, encoding="utf-8")
    for name in ("README.md", "DOCS.md", "ARCHITECTURE.md", "CHANGELOG.md"):
        src = ROOT / "adaptive_ai" / name
        if src.exists():
            shutil.copy2(src, OUT / name)
    changelog = OUT / "CHANGELOG.md"
    if changelog.exists():
        c = changelog.read_text(encoding="utf-8")
        c = c.replace('# Changelog\n', '''# Changelog\n\n## 0.7.13\n- Fix ESPHome/LD2411 context exclusion: configuration `number`/`select`/`switch` siblings no longer remove `sensor`/`binary_sensor` channels such as `kitchen_presence_presence` from training.\n- Keep direct actuators and explicit electrical-unit telemetry excluded while preserving ESPHome sensory siblings.\n- Force a new broad historical context rebuild so newly admitted ESPHome sensors are imported from Recorder and can compete during feature screening.\n- Add measured replay progress, work counters and phase-specific ETA during long policy rebuilds instead of freezing the UI at 13%.\n- Add a preparation pipeline explaining target discovery, context import, sensor screening, historical replay, benchmark and readiness.\n- Surface eligible/selected ESPHome context counts and preserved sibling diagnostics.\n''', 1)
        changelog.write_text(c, encoding="utf-8")
    tr = ROOT / "adaptive_ai" / "translations"
    if tr.exists():
        shutil.copytree(tr, OUT / "translations", dirs_exist_ok=True)


def sanity_tests(main_text, app_text):
    required = [
        'APP_VERSION = "0.7.13"',
        'TRAINING_REVISION = "esphome-sensor-context-v17"',
        'def is_esphome_sensor_entity',
        'esphome_sensor_sibling_overrides',
        'progress_label="Rebuilding predictive policies"',
        'work_done=replay_done',
    ]
    for token in required:
        if token not in main_text:
            raise RuntimeError(f"sanity token missing from main.py: {token}")
    for token in ('ESPHome sensors eligible', 'prep-pipeline', 'Current phase'):
        if token not in app_text:
            raise RuntimeError(f"sanity token missing from app.js: {token}")
    # Contract-level source assertions for the regression that motivated this release.
    if 'domain in strong_actuator_domains' not in main_text:
        raise RuntimeError('device-wide exclusion is not restricted to strong actuators')
    if 'if is_esphome_sensor_entity(eid, registry):\n                rescued_esphome_sensors += 1\n                continue' not in main_text:
        raise RuntimeError('ESPHome sensor sibling rescue missing')


def main():
    if not SRC.exists():
        raise SystemExit(f"missing {SRC}")
    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    (OUT / "src" / "static").mkdir(parents=True)
    shutil.copy2(SRC / "main.py", OUT / "src" / "main.py")
    shutil.copy2(SRC / "control.py", OUT / "src" / "control.py")
    shutil.copy2(SRC / "run.sh", OUT / "src" / "run.sh")
    for name in ("app.js", "index.html", "settings.js", "style.css"):
        shutil.copy2(SRC / "static" / name, OUT / "src" / "static" / name)

    patch_main(OUT / "src" / "main.py")
    patch_app(OUT / "src" / "static" / "app.js")
    patch_css(OUT / "src" / "static" / "style.css")
    write_addon_files()

    main_text = (OUT / "src" / "main.py").read_text(encoding="utf-8")
    app_text = (OUT / "src" / "static" / "app.js").read_text(encoding="utf-8")
    sanity_tests(main_text, app_text)

    # Syntax check without importing HA/websocket runtime.
    compile(main_text, str(OUT / "src" / "main.py"), 'exec')
    compile((OUT / "src" / "control.py").read_text(encoding="utf-8"), str(OUT / "src" / "control.py"), 'exec')

    manifest = {
        "version": "0.7.13",
        "base": "0.7.12 materialized runtime",
        "regression": "ESPHome LD2411 sensor siblings must remain training context",
        "install": "replace the existing adaptive_ai directory with this adaptive_ai directory, then rebuild/reinstall the Home Assistant app",
    }
    (OUT / "BUILD_INFO.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    zip_path = OUT_ROOT / "HomeMind-Adaptive-AI-0.7.13.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(OUT.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(OUT_ROOT))

    encoded = base64.b64encode(zip_path.read_bytes()).decode("ascii")
    for i in range(0, len(encoded), 8000):
        (OUT_ROOT / f"zip_b64.part{i//8000:02d}").write_text(encoded[i:i+8000], encoding="ascii")
    (OUT_ROOT / "BUILD_OK.txt").write_text(
        f"Adaptive AI 0.7.13 built successfully. ZIP bytes={zip_path.stat().st_size}; b64 parts={(len(encoded)+7999)//8000}\n",
        encoding="utf-8",
    )
    print((OUT_ROOT / "BUILD_OK.txt").read_text(), end="")


if __name__ == '__main__':
    main()
