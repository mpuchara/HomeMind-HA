"""Opt-in, bounded contextual-bandit trials alongside the qualified policy.

The historical policy/schema is never rewritten here. A trial changes one action
near a learned boundary, not a HA sensor. Only Executor may dispatch it. Sparse
named features keep other-device discoveries out of historical feature slots.
"""
import copy
from datetime import datetime
import json
import math
import random
import threading
from uuid import uuid4

from context import state_scalar, target_value
from control import legal_value, same_value, timing_for
from home_sources import source_kind
from settings import now_ts, OPTIONS

FOCUSES = {'presence', 'environment', 'devices'}
DEVICE_DOMAINS = {'light', 'switch', 'fan', 'climate', 'media_player', 'cover',
                  'humidifier', 'water_heater', 'vacuum'}
ENV_CLASSES = {'illuminance', 'temperature', 'humidity', 'pressure', 'atmospheric_pressure'}
DEFAULTS = dict(enabled=False, focus='presence', intensity=.2, interval=900,
                daily_budget=6, observation_seconds=30, max_step=1.0)


def activity_scalar(state):
    if not state or state.get('state') in ('unavailable', 'unknown'):
        return None
    activity = str((state.get('attributes') or {}).get('hvac_action') or state['state'])
    return -1. if activity in ('off', 'idle', 'standby', 'docked', 'paused') else 1.


def state_change_ts(state):
    """Return a HA transition timestamp when one is available."""
    if not state:
        return None
    raw = state.get('last_changed') or state.get('last_updated')
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        return value if math.isfinite(value) else None
    text = str(raw).strip()
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


class Experiments:
    def __init__(self, store, clock=now_ts, rng=None):
        self.store, self.clock = store, clock
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        self.cache, self.prepared, self.messages = {}, {}, {}
        self.screen_after = {}

    def _get(self, aid):
        if aid not in self.cache:
            raw = self.store.meta_get('experiment:' + aid)
            data = json.loads(raw) if raw else dict(config=dict(DEFAULTS), revision=0,
                learners={}, trials=[], last_started=0, cooldown_until=0, active=None, last_outcome=None)
            self.cache[aid] = data
            if data.get('active'):
                # A restart loses continuous observation; never invent acceptance.
                data['last_outcome'] = dict(reason='restart: observation interrupted', reward=None)
                data['active'] = None
                self._save(aid)
        return self.cache[aid]

    def _save(self, aid):
        self.store.meta_set('experiment:' + aid, json.dumps(self.cache[aid], allow_nan=False))

    def configure(self, agent, payload):
        if not isinstance(payload, dict) or set(payload) - set(DEFAULTS):
            raise ValueError('Unknown experiment settings')
        with self.lock:
            data = self._get(agent['id'])
            cfg = {**data['config'], **payload}
            if type(cfg['enabled']) is not bool or not isinstance(cfg['focus'], str) or cfg['focus'] not in FOCUSES:
                raise ValueError('Select presence, environment or devices and a boolean enabled value')
            bounds = dict(intensity=(.05, .35), interval=(300, 86400), daily_budget=(1, 24),
                          observation_seconds=(10, 3600), max_step=(.01, 100))
            for key, (lo, hi) in bounds.items():
                value = cfg[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
                    raise ValueError(f'{key} must be between {lo} and {hi}')
            if int(cfg['daily_budget']) != cfg['daily_budget']:
                raise ValueError('daily_budget must be an integer')
            if cfg['enabled'] and agent.get('training_state') != 'qualified':
                raise ValueError('Complete training before enabling experiments')
            if data.get('active'):
                self._finish(agent['id'], None, 'settings changed; observation interrupted')
            data['config'], data['revision'] = cfg, data['revision'] + 1
            self.prepared.pop(agent['id'], None)
            self.screen_after.pop(agent['id'], None)
            self.messages[agent['id']] = 'Waiting for an eligible context' if cfg['enabled'] else 'Experiments disabled'
            self._save(agent['id'])
            self.store.event(agent['id'], 'info', 'experiment_config', 'Context experiments configured', cfg)
            return self.status(agent['id'])

    def _learner(self, data):
        return data['learners'].setdefault(data['config']['focus'],
            dict(weights=[{}, {}, {}], counts=[0, 0, 0], rewards=[0., 0., 0.]))

    def status(self, aid):
        with self.lock:
            data = self._get(aid)
            learner = self._learner(data)
            active = data.get('active')
            influences = []
            for arm in (1, 2):
                for name, (a, b) in learner['weights'][arm].items():
                    if name.startswith('device:'):
                        influences.append(dict(entity_id=name[7:], contribution=round(b/a, 4), samples=learner['counts'][arm]))
            influences.sort(key=lambda x: abs(x['contribution']), reverse=True)
            active_keys = ('trial_id', 'kind', 'value', 'baseline', 'started', 'deadline',
                           'action_at', 'observation_start', 'observation_end')
            return copy.deepcopy(dict(config=data['config'], revision=data['revision'],
                active={k: active.get(k) for k in active_keys} if active else None,
                counts=learner['counts'], rewards=learner['rewards'],
                trials_today=sum(t > self.clock()-86400 for t in data['trials']),
                next_trial_after=max(data['last_started']+data['config']['interval'], data['cooldown_until']),
                last_outcome=data.get('last_outcome'), reason=self.messages.get(aid, 'Waiting for an eligible context'),
                device_influences=influences[:12]))

    @staticmethod
    def _watch_entities(trial):
        if not trial:
            return set()
        return (set(trial.get('prediction_inputs') or {})
                | set(trial.get('background_dependencies') or {})
                | set(trial.get('outcome_sources') or {})
                | set(trial.get('snapshot') or {}))

    def watches(self, aid):
        with self.lock:
            data = self._get(aid)
            return self._watch_entities(data.get('active')) | self._watch_entities(self.prepared.get(aid))

    def _inputs(self, agent, policy, states, registry, features, labels, focus, data):
        """Build prediction inputs only; outcome evidence and background watches stay separate."""
        selected, x, prediction_inputs = {}, {'bias': 1.0}, {}
        for index, names in labels.items():
            name = names[0]
            eid, _, suffix = name.partition(':')
            state = states.get(eid)
            if not state or suffix != 'value' or state.get('state') in ('unavailable', 'unknown'):
                continue
            attrs = state.get('attributes') or {}
            kind, _ = source_kind(eid, state, registry.get(eid, {}))
            environment = (eid.startswith(('weather.', 'sun.')) or attrs.get('device_class') in ENV_CLASSES
                or attrs.get('unit_of_measurement') in ('lx', '°C', '°F', 'hPa'))
            if (focus == 'presence' and kind) or (focus == 'environment' and environment):
                selected[index] = name
                x[name] = float(features.get(index, 0))
                prediction_inputs[eid] = state_scalar(state)
        if focus == 'presence':
            for index, names in labels.items():
                if names[0].startswith('home:occupancy'):
                    selected[index] = names[0]
                    x[names[0]] = float(features.get(index, 0))
        if focus == 'devices':
            own = registry.get(agent['target_entity'], {})
            candidates = []
            for eid, state in states.items():
                reg = registry.get(eid, {})
                if (eid == agent['target_entity'] or eid.split('.')[0] not in DEVICE_DOMAINS
                    or (own.get('device_id') and reg.get('device_id') == own['device_id'])
                    or reg.get('entity_category') or reg.get('disabled_by')
                    or state.get('state') in ('unavailable', 'unknown')):
                    continue
                value = activity_scalar(state)
                if value is None:
                    continue
                operating = value > 0
                same_area = bool(own.get('area_id') and own.get('area_id') == reg.get('area_id'))
                candidates.append((not operating, not same_area, eid, value))
            candidates.sort()
            # Named weights survive rotating discovery without reinterpreting columns.
            for _, _, eid, value in candidates[:32]:
                x['device:' + eid] = value
                prediction_inputs[eid] = value
            self.messages[agent['id']] = f'Scanned {len(candidates)} other devices; {min(32, len(candidates))} trial inputs'
        return selected, x, prediction_inputs

    @staticmethod
    def _presence_outcome_sources(agent, selected, states, registry):
        """Return verified same-area presence sources allowed to settle a presence outcome.

        A predictor is not automatically an outcome source. Both the target and source
        must have explicit HA area mappings, and the source must have a verified binary
        or tracker presence role. Missing mapping deliberately leaves the outcome unknown.
        """
        target_area = (registry.get(agent['target_entity'], {}) or {}).get('area_id')
        if not target_area:
            return {}
        sources = {}
        for name in selected.values():
            eid, _, suffix = str(name).partition(':')
            if suffix != 'value' or eid == agent['target_entity']:
                continue
            state = states.get(eid)
            if not state or state.get('state') in ('unavailable', 'unknown'):
                continue
            reg = registry.get(eid, {}) or {}
            kind, _ = source_kind(eid, state, reg)
            source_area = reg.get('area_id')
            if kind not in {'binary', 'tracker'} or not source_area or source_area != target_area:
                continue
            before = state_scalar(state)
            if before is None:
                continue
            sources[eid] = {'role': kind, 'area_id': source_area, 'before': before, 'anchored_at': None}
        return sources

    @staticmethod
    def _observed_value(trial, eid, state):
        if not state or state.get('state') in ('unavailable', 'unknown'):
            return None
        return activity_scalar(state) if 'device:' + eid in trial.get('x', {}) else state_scalar(state)

    def _rebase_outcome_sources(self, trial, states, at):
        if not states:
            return
        for eid, source in (trial.get('outcome_sources') or {}).items():
            state = states.get(eid)
            value = self._observed_value(trial, eid, state)
            if value is not None:
                source['before'] = value
                source['anchored_at'] = at

    @staticmethod
    def _score(learner, arm, x):
        weights = learner['weights'][arm]
        norm = max(1.0, math.sqrt(sum(v*v for v in x.values())))
        mean, variance = 0., 0.
        for name, raw in x.items():
            value = raw/norm
            a, b = weights.get(name, (1., 0.))
            mean += b/a*value
            variance += value*value/a
        return mean + .25*math.sqrt(variance)

    def propose(self, agent, policy, states, registry, features, labels, chosen, confidence, arms, horizon, rt):
        aid, now = agent['id'], self.clock()
        with self.lock:
            data = self._get(aid)
            cfg = data['config']
            self.prepared.pop(aid, None)
            if not cfg['enabled'] or agent['mode'] != 'control' or not agent['enabled']:
                return None
            active = data.get('active')
            if active:
                if active['policy_version'] != policy.VERSION:
                    self._finish(aid, None, 'policy changed; observation interrupted')
                    return None
                # New ordinary decisions always win. Only retain a probe while its
                # original baseline and selected physical context remain unchanged.
                if active['kind'] == 'probe' and same_value(chosen['value'], active['baseline'], agent['deadband']):
                    # Routine age decay revises the base model every minute; this
                    # must not truncate a thermostat's 15-minute observation.
                    return self._prepare(aid, data, dict(active, model_revision=policy.model_revision), now)
                return None
            if any(other.get('active', {}).get('kind') == 'probe' for other in self.cache.values() if other.get('active')):
                self.messages[aid] = 'Another agent is observing a probe; ordinary control remains active'
                return None
            if rt.get('pending') or now < max(data['last_started']+cfg['interval'], data['cooldown_until'], rt.get('manual_override_until', 0)):
                self.messages[aid] = 'Waiting for feedback, interval or manual correction cooldown'
                return None
            data['trials'] = [t for t in data['trials'] if t > now-86400]
            if len(data['trials']) >= cfg['daily_budget']:
                self.messages[aid] = 'Rolling 24-hour trial budget exhausted'
                return None
            current = target_value(states.get(agent['target_entity']), agent['target_property'])
            if current is None or not same_value(current, chosen['value'], agent['deadband']):
                self.messages[aid] = 'Ordinary control action takes priority before any trial'
                return None
            if confidence < agent['confidence_threshold']:
                self.messages[aid] = 'Baseline confidence below Control threshold'
                return None
            if chosen.get('support', 0) < float(OPTIONS.get('min_historical_support', .2)) or chosen.get('novelty', 1) > float(OPTIONS.get('max_context_novelty', .85)):
                self.messages[aid] = 'Baseline context is outside the supported distribution'
                return None
            if cfg['focus'] == 'devices':
                if now < self.screen_after.get(aid, 0):
                    return None
                self.screen_after[aid] = now+10
            registry = registry() if callable(registry) else registry
            selected, x, prediction_inputs = self._inputs(agent, policy, states, registry, features, labels, cfg['focus'], data)
            outcome_sources = (
                self._presence_outcome_sources(agent, selected, states, registry)
                if cfg['focus'] == 'presence' else {}
            )
            for source in outcome_sources.values():
                source['anchored_at'] = now
            if len(x) <= 1 or (cfg['focus'] == 'presence' and not any(v > .01 for k, v in x.items() if k != 'bias')):
                self.messages[aid] = 'No usable signal for this focus in the selected context'
                return None
            span = max(.01, agent['max_value']-agent['min_value'])
            x['baseline_setting'] = (chosen['value']-agent['min_value'])/span
            x['trial_strength'] = cfg['intensity']
            background_dependencies = {}
            for eid in getattr(getattr(policy, 'schema', None), 'entities', ()):
                state = states.get(eid)
                value = state_scalar(state) if state and state.get('state') not in ('unknown', 'unavailable') else None
                if (value is not None and eid != agent['target_entity']
                    and eid not in prediction_inputs and eid not in outcome_sources):
                    background_dependencies[eid] = value
            # Compatibility view for older diagnostics only. Outcome attribution below
            # consumes the three named manifests, never this merged snapshot.
            snapshot = {**prediction_inputs, **background_dependencies}
            candidates = []
            head = policy.heads[horizon]
            with policy.lock:
                for arm in arms:
                    if abs(arm['index']-chosen['index']) != 1:
                        continue
                    delta = arm['value'] - chosen['value']
                    if agent['target_property'] == 'option_index':
                        continue  # Arbitrary HA enum order is not a physical neighbourhood.
                    if agent['target_property'] == 'power' and delta < 0:
                        continue  # Presence trials never turn an occupied light off early.
                    desired = arm['value']
                    max_delta = cfg['max_step']
                    if agent['target_property'] != 'power':
                        # The online residual may use physical device steps finer
                        # than the historical arm grid. Cap it at 5% of user range.
                        max_delta = min(cfg['max_step'], (agent['max_value']-agent['min_value'])*.05)
                        desired = chosen['value'] + math.copysign(max_delta, delta)
                    try:
                        legal = legal_value(agent, states[agent['target_entity']], desired)
                    except (ValueError, TypeError):
                        continue
                    if (abs(legal-chosen['value']) > max_delta+1e-9
                        or abs(legal-current) > max_delta+1e-9
                        or same_value(legal, current, agent['deadband'])):
                        continue
                    arm = dict(arm, value=legal)
                    gain = cfg['intensity']*.25 if cfg['focus'] == 'devices' else 0.
                    for index in selected:
                        gradient = head.b[arm['index']][index]/head.a[arm['index']][index] - head.b[chosen['index']][index]/head.a[chosen['index']][index]
                        value = features.get(index, 0)
                        shift = min(cfg['intensity'], max(0., 1-value if gradient > 0 else value+1))
                        if cfg['focus'] != 'presence' or gradient > 0:
                            gain += abs(gradient)*shift
                    gap = max(0., chosen['mean']-arm['mean'])
                    if gain <= 0 or gap > gain or arm['support'] < float(OPTIONS.get('min_historical_support', .2)) or arm['novelty'] > float(OPTIONS.get('max_context_novelty', .85)):
                        continue
                    candidates.append((1 if delta > 0 else 2, arm, gap, gain))
            if not candidates:
                self.messages[aid] = 'No nearby action reachable by a small change in this context (or step below action resolution)'
                return None
            learner = self._learner(data)
            candidate = max(candidates, key=lambda c: self._score(learner, c[0], x))
            arm_id, arm, gap, gain = candidate
            # Interleaved controls distinguish doing nothing from a useful change.
            reference = self.rng.random() < .25 or self._score(learner, 0, x) > self._score(learner, arm_id, x)
            timing = timing_for(agent)
            window = max(cfg['observation_seconds'], timing.settling)
            trial = dict(trial_id=str(uuid4()), kind='reference' if reference else 'probe',
                arm=0 if reference else arm_id, value=chosen['value'] if reference else arm['value'],
                baseline=chosen['value'], index=chosen['index'] if reference else arm['index'], x=x,
                prediction_inputs=prediction_inputs, background_dependencies=background_dependencies,
                outcome_sources=outcome_sources, snapshot=snapshot,
                # Deprecated compatibility alias; outcome logic consumes outcome_sources.
                outcome_confirmers=sorted(outcome_sources),
                focus=cfg['focus'], revision=data['revision'], policy_version=policy.VERSION,
                model_revision=policy.model_revision, target=agent['target_entity'], property=agent['target_property'],
                confidence=confidence, support=arm['support'], novelty=arm['novelty'], gap=gap, gain=gain,
                started=now, deadline=now+timing.acknowledgement+window,
                action_at=None, observation_start=now if reference else None,
                observation_end=now+window if reference else None,
                ack=now if reference else None, window=window)
            if reference:
                self._start(aid, trial)
                return None
            return self._prepare(aid, data, trial, now)

    def _prepare(self, aid, data, trial, now):
        item = dict(trial, token=str(uuid4()), expires=now+2)
        self.prepared[aid] = item
        self.messages[aid] = 'Bounded probe; displayed confidence belongs to the baseline policy'
        return copy.deepcopy(item)

    def valid(self, agent, intent):
        with self.lock:
            data = self._get(agent['id'])
            trial = self.prepared.get(agent['id'])
            return bool(trial and trial['token'] == intent.experiment_token and self.clock() < trial['expires']
                and data['config']['enabled'] and data['revision'] == trial['revision']
                and agent['mode'] == 'control' and trial['target'] == agent['target_entity']
                and trial['property'] == agent['target_property'] and trial['value'] == intent.desired_value
                and trial['model_revision'] == intent.model_revision and trial['confidence'] >= agent['confidence_threshold'])

    def begin(self, agent, intent, states=None):
        """Reserve before HTTP, including uncertain transport failures in the budget."""
        with self.lock:
            if not self.valid(agent, intent):
                return False
            if any(aid != agent['id'] and (d.get('active') or {}).get('kind') == 'probe' for aid, d in self.cache.items()):
                return False
            trial = self.prepared[agent['id']]
            if not self._get(agent['id']).get('active'):
                self._rebase_outcome_sources(trial, states, self.clock())
                self._start(agent['id'], trial)
            return True

    def dispatched(self, agent, intent, states=None):
        """Bind a successful probe to the actual Executor dispatch boundary and window."""
        with self.lock:
            now = self.clock()
            data = self._get(agent['id'])
            trial = data.get('active')
            if trial and trial.get('kind') == 'probe' and trial.get('trial_id'):
                self._rebase_outcome_sources(trial, states, now)
                trial['action_at'] = now
                trial['observation_start'] = now
                trial['observation_end'] = now + trial['window']
                self._save(agent['id'])
            self.prepared.pop(agent['id'], None)

    def _start(self, aid, trial):
        data, now = self._get(aid), self.clock()
        duration = trial['deadline'] - trial['started']
        trial = dict(trial, trial_id=trial.get('trial_id') or str(uuid4()),
                     started=now, deadline=now+duration)
        if trial['kind'] == 'reference':
            trial['observation_start'] = now
            trial['observation_end'] = now + trial['window']
            trial['ack'] = now
            for source in (trial.get('outcome_sources') or {}).values():
                source['anchored_at'] = now
        data.update(active=trial, last_started=now)
        data['trials'].append(now)
        self._save(aid)
        self.store.event(aid, 'info', 'experiment_started', trial['kind'] + ': ' + trial['focus'],
                         {k: trial[k] for k in ('trial_id', 'kind', 'value', 'baseline', 'focus', 'gap', 'gain')})

    def observe(self, agent, states, current, manual=False):
        aid, now = agent['id'], self.clock()
        with self.lock:
            data = self._get(aid)
            trial = data.get('active')
            if manual and data['config']['enabled']:
                data['cooldown_until'] = now + max(3600, data['config']['interval']*4)
                if trial:
                    self._finish(aid, -1., 'manual correction')
                else:
                    self._save(aid)
                return
            if not trial:
                return
            if not agent['enabled'] or agent['mode'] != 'control' or agent.get('training_state') != 'qualified' or current is None:
                return self._finish(aid, None, 'observation interrupted: mode, training or availability')
            matches = same_value(current, trial['value'], agent['deadband'])
            if matches and trial['ack'] is None:
                trial['ack'] = now
            if trial['ack'] is None and now-trial['started'] >= timing_for(agent).acknowledgement:
                return self._finish(aid, None, 'no device acknowledgement')
            if trial['ack'] is not None and not matches:
                return self._finish(aid, None, 'external or ordinary control changed the target')

            manifests = {}
            for role in ('prediction_inputs', 'background_dependencies'):
                for eid, before in (trial.get(role) or {}).items():
                    manifests.setdefault(eid, before)
            # Legacy in-flight data is safe-by-default after upgrade: it may abort but
            # cannot become positive outcome evidence without an explicit outcome source.
            if not manifests:
                manifests.update(trial.get('snapshot') or {})

            changes, unavailable = [], []
            for eid, before in manifests.items():
                state = states.get(eid)
                after = self._observed_value(trial, eid, state)
                if after is None:
                    unavailable.append(eid)
                elif abs(after-before) > .05:
                    changes.append((eid, before, after, state))
            if unavailable:
                return self._finish(aid, None, 'context unavailable')

            if changes:
                outcome_sources = trial.get('outcome_sources') or {}
                candidate_events = []
                for eid, _before, after, state in changes:
                    source = outcome_sources.get(eid)
                    source_before = source.get('before') if source else None
                    if (source and trial['focus'] == 'presence' and trial['property'] == 'power'
                        and state.get('state') in ('on', 'home')
                        and source_before is not None and source_before < 0 and after > 0):
                        changed_at = state_change_ts(state)
                        if changed_at is None and source.get('anchored_at') is not None:
                            # The source was re-snapshotted at the action boundary, so a
                            # later observed transition remains attributable even when HA
                            # omitted last_changed from this state payload.
                            changed_at = now
                        candidate_events.append((eid, changed_at))

                if candidate_events and trial['ack'] is not None:
                    start = trial.get('observation_start')
                    end = trial.get('observation_end')
                    valid_events = [(eid, changed_at) for eid, changed_at in candidate_events
                                    if changed_at is not None and start is not None and end is not None
                                    and start <= changed_at <= end]
                    if valid_events:
                        if trial['kind'] == 'reference':
                            return self._finish(aid, None,
                                'reference arrival observed; no comfort-loss outcome defined')
                        if trial.get('action_at') is None:
                            return self._finish(aid, None, 'presence timing unknown: action timestamp unavailable')
                        return self._finish(aid, .6, 'presence confirmed after decision')
                    return self._finish(aid, None,
                        'presence transition outside observable trial window')

                return self._finish(aid, None, 'context changed; ordinary control resumes')

            observation_end = trial.get('observation_end')
            if trial['ack'] is not None and observation_end is not None and now >= observation_end:
                if trial['focus'] == 'presence' and not (trial.get('outcome_sources') or {}):
                    return self._finish(aid, None, 'presence outcome unobservable: no verified local source')
                return self._finish(aid, .02 if trial['kind'] == 'probe' else .05,
                                    'weak preference: observed without correction')
            if now >= trial['deadline']:
                return self._finish(aid, None, 'observation deadline exceeded')

    def cancel(self, aid, reason):
        with self.lock:
            if self._get(aid).get('active'):
                self._finish(aid, None, reason)
            self.prepared.pop(aid, None)

    def _finish(self, aid, reward, reason):
        data = self._get(aid)
        trial = data.get('active')
        if not trial:
            return
        if reward is not None:
            learner = self._learner(data)
            arm, x = trial['arm'], trial['x']
            weights = learner['weights'][arm]
            norm = max(1., math.sqrt(sum(v*v for v in x.values())))
            for name, raw in x.items():
                value = raw/norm
                a, b = weights.get(name, (1., 0.))
                weights[name] = [1+(a-1)*.995+value*value, b*.995+reward*value]
            # Keep memory and disk bounded even with changing device registries.
            if len(weights) > 256:
                learner['weights'][arm] = dict(sorted(weights.items(), key=lambda kv: kv[1][0], reverse=True)[:256])
            learner['counts'][arm] += 1
            learner['rewards'][arm] += reward
        data['last_outcome'] = dict(
            trial_id=trial.get('trial_id'), reason=reason, reward=reward,
            kind=trial['kind'], focus=trial['focus'], at=self.clock(),
            started=trial.get('started'), action_at=trial.get('action_at'), ack_at=trial.get('ack'),
            observation_start=trial.get('observation_start'), observation_end=trial.get('observation_end'))
        data['active'] = None
        self.prepared.pop(aid, None)
        self.messages[aid] = reason
        self._save(aid)
        self.store.event(aid, 'warning' if reward is not None and reward < 0 else 'info',
                         'experiment_outcome', reason, data['last_outcome'])
