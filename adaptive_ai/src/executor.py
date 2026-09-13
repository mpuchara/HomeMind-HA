"""The ONLY module permitted to send Home Assistant service actions.

All predictions, including Shadow, arrive as immutable ActionIntent instances.
Each target has a serial dispatch lock; validation uses fresh store/state values.
"""
import math
import threading
from collections import OrderedDict
from control import timing_for, legal_value, same_value
from context import target_call, target_value
from settings import (OPTIONS, now_ts)
from storage import STORE
from ha import HA, AUTOMATION_KNOWLEDGE
from telemetry import TELEMETRY
from rewards import RewardEngine


class Executor:
    def __init__(self, engine):
        self.engine = engine
        self.reward_engine = RewardEngine()
        self.locks = {}
        self.locks_guard = threading.Lock()
        self.dispatched = OrderedDict()

    def target_lock(self, entity):
        with self.locks_guard:
            return self.locks.setdefault(entity, threading.RLock())

    def _service(self, domain, action, data):
        return HA.service(domain, action, data)

    def take_control(self, agent, refresh=False):
        """Explicit Control transition or incumbent ownership enforcement."""
        with self.target_lock(agent['target_entity']):
            current = STORE.get_agent_config(agent['id'])
            if not current or not current['enabled'] or current.get('training_state') != 'qualified':
                raise ValueError('Control requires an enabled, qualified agent')
            if refresh:
                self.engine.refresh_states()
                AUTOMATION_KNOWLEDGE.scan(dict(self.engine.state_map), self.engine.context.resolved_registry(), force=True)
                warning = AUTOMATION_KNOWLEDGE.error
                self.engine.runtime.setdefault(agent['id'], {})['automation_scan_warning'] = warning
                if warning:
                    STORE.event(agent['id'], 'warning', 'automation_scan_partial',
                                'Control continues with known target automations; scan is incomplete',
                                {'warning': warning})
            conflicts = [a for a in STORE.list_agent_configs() if a['id'] != agent['id'] and a['enabled']
                         and a['mode'] == 'control' and a['target_entity'] == agent['target_entity']]
            if conflicts:
                raise ValueError('Another Control agent owns this entity')
            _, infos = AUTOMATION_KNOWLEDGE.hints_for_target(agent['target_entity'])
            disabled = []
            for info in infos:
                eid = info.get('entity_id')
                if not eid or not eid.startswith('automation.'):
                    continue
                state = self.engine.state_map.get(eid)
                if (state or {}).get('state') == 'off':
                    continue
                # Unknown automation state cannot certify ownership.
                if state is None:
                    raise RuntimeError('Automation state unavailable: ' + eid)
                self._service('automation', 'turn_off', {'entity_id': eid, 'stop_actions': True})
                disabled.append(eid)
            if disabled:
                self.engine.refresh_states()
                if any(self.engine.state_map.get(eid, {}).get('state') != 'off' for eid in disabled):
                    raise RuntimeError('Automation OFF not confirmed')
                with AUTOMATION_KNOWLEDGE.lock:
                    for info in AUTOMATION_KNOWLEDGE.automations:
                        if info.get('entity_id') in disabled:
                            info['enabled'] = False
                STORE.event(agent['id'], 'info', 'automation_takeover', 'Control disabled matching automations', {'disabled': disabled})
            return disabled

    def _result(self, intent, rt, status, reason, decision='blocked'):
        result = {'intent_id': intent.intent_id, 'status': status, 'reason': reason,
                  'created_at': intent.created_at, 'desired_value': intent.desired_value}
        rt['intent'] = result
        rt['decision_state'], rt['decision_reason'] = decision, reason
        TELEMETRY.intent(status, reason)
        return result

    def submit(self, intent, features=None, action_index=None):
        with self.target_lock(intent.target_entity):
            return self._submit(intent, features or {}, action_index)

    def _submit(self, intent, features, action_index):
        engine = self.engine
        rt = engine.runtime.setdefault(intent.agent_id, {})
        def reject(reason, status='REJECTED', decision='blocked'):
            return self._result(intent, rt, status, reason, decision)
        timestamp = now_ts()
        if intent.expired(timestamp):
            return reject('expired: intent TTL exceeded', 'EXPIRED')
        agent = STORE.get_agent_config(intent.agent_id)
        if not agent or not agent['enabled']:
            return reject('disabled: agent missing or disabled')
        if agent['target_entity'] != intent.target_entity or agent['target_property'] != intent.target_property:
            return reject('target: intent target does not match agent')
        if agent.get('training_state') != 'qualified':
            return reject('qualification: Train or Rebuild required')
        if agent['mode'] not in ('shadow', 'control'):
            return reject('paused: agent is paused', decision='paused')
        model = engine.models.get(agent['id'])
        if not model or model.VERSION != intent.policy_version or model.model_revision != intent.model_revision:
            return reject('model: stale policy version or revision')
        if intent.policy_head not in model.heads:
            return reject('model: unavailable policy head')
        with engine.lock:
            state = engine.state_map.get(intent.target_entity)
            if engine.entity_revisions.get(intent.target_entity, 0) != intent.target_revision:
                return reject('state: target changed since prediction', decision='waiting')
            if any(engine.entity_revisions.get(eid, 0) != rev for eid, rev in intent.context_dependencies):
                return reject('context: selected input changed since prediction', decision='waiting')
            if engine.context.home.revision != intent.context_revision:
                return reject('context: home state changed since prediction', decision='waiting')
        current = target_value(state, intent.target_property)
        if current is None or not math.isfinite(current):
            return reject('unavailable: target state/value unavailable')
        if agent['mode'] == 'shadow':
            return self._result(intent, rt, 'SHADOW', intent.reason, 'shadow')
        if intent.confidence < float(agent['confidence_threshold']):
            return reject('confidence: below configured threshold')
        if intent.support < float(OPTIONS.get('min_historical_support', .2)):
            return reject('support: insufficient historical support')
        if intent.novelty > float(OPTIONS.get('max_context_novelty', .85)):
            return reject('novelty: context outside supported distribution')
        if timestamp < rt.get('manual_override_until', 0):
            return reject('manual: explicit user override active')
        if timestamp < rt.get('takeover_retry_after', 0):
            return reject('takeover: retry backoff', decision='waiting')
        if state.get('state') in ('opening', 'closing') and agent['target_property'] == 'position':
            return reject('settling: cover moving', decision='waiting')
        try:
            value = legal_value(agent, state, intent.desired_value)
        except (TypeError, ValueError) as exc:
            return reject('limits: ' + str(exc))
        timing = timing_for(agent)
        pending = rt.get('pending')
        supersede = bool(pending and pending.get('acknowledged_ts') is None
                         and intent.target_entity.split('.')[0] in ('light', 'input_boolean')
                         and not same_value(value, pending['action_value'], agent['deadband']))
        # Ownership is enforced even when the desired value already matches reality.
        try:
            if self.take_control(agent):
                engine.wake_event.set()
                return reject('takeover: automations disabled; fresh prediction required', decision='waiting')
        except Exception as exc:
            rt['takeover_retry_after'] = timestamp + 30
            return reject('takeover: ' + str(exc), decision='error')
        if intent.intent_id in self.dispatched:
            return reject('duplicate: intent already dispatched', decision='hold')
        if same_value(value, current, agent['deadband']) and not supersede:
            return reject('duplicate: desired value already set', decision='hold')
        if pending and not supersede:
            ack = pending.get('acknowledged_ts')
            if ack is None or timestamp - ack < timing.settling:
                return reject('acknowledgement: waiting for device / settling', decision='waiting')
        if timestamp < rt.get('retry_after', 0):
            return reject('retry: device backoff', decision='waiting')
        if timestamp - rt.get('last_ai_ts', 0) < max(float(agent['action_interval']), timing.settling):
            return reject('cooldown: minimum action interval', decision='waiting')
        # Slow takeover and HTTP work can outlive the intent. Recheck all revisions.
        if intent.expired(now_ts()):
            return reject('expired: TTL exceeded before dispatch', 'EXPIRED')
        fresh = STORE.get_agent_config(agent['id'])
        if fresh != agent:
            return reject('settings: agent changed before dispatch', decision='waiting')
        with engine.lock:
            if engine.state_map.get(intent.target_entity) != state or engine.context.home.revision != intent.context_revision or any(
                    engine.entity_revisions.get(eid, 0) != rev for eid, rev in intent.context_dependencies):
                return reject('context: state changed before dispatch', decision='waiting')
        domain, service, data = target_call(intent.target_entity, intent.target_property, value, state)
        rt.update(last_service_ts=now_ts(), last_service=f'{domain}.{service}', last_service_data=data)
        engine.record_command(agent, value)
        started = now_ts()
        try:
            response = self._service(domain, service, data)
            engine.record_command(agent, value, response)
        except Exception as exc:
            rt.update(retry_after=now_ts()+max(2, timing.settling), last_service_ok=False,
                      last_service_error=f'{type(exc).__name__}: {exc}')
            return reject('service: ' + str(exc), decision='error')
        self.dispatched[intent.intent_id] = started
        if len(self.dispatched) > 1024:
            self.dispatched.popitem(last=False)
        rt.update(last_ai_ts=started, last_ai_value=value, last_service_ok=True,
                  last_service_error=None, last_service_latency_ms=(now_ts()-started)*1000)
        forecast = rt.get('context_meta', {}).get('home_forecast', {})
        if pending and pending.get('anticipated') and pending.get('acknowledged_ts') is not None:
            # Replacing a command must not erase its unresolved anticipation reward.
            outcomes = rt.setdefault('outcomes', [])
            outcomes.append({**pending, 'ended_ts': started})
            if len(outcomes) > 16:
                outcomes.pop(0)  # bounded audit window, never invent a reward
        rt['pending'] = {'action_index': action_index, 'action_value': value,
                         'horizon': intent.prediction_horizon, 'policy_head': intent.policy_head, 'features': features,
                         'started_ts': started, 'acknowledged_ts': None, 'no_service': False,
                         'area_id': forecast.get('area_id'), 'anticipated': bool(value >= .5
                            and agent['target_property'] == 'power' and forecast.get('known')
                            and forecast.get('occupancy_now', 0) < .5),
                         'observation_known': bool(forecast.get('known')),
                         'chatter': started-rt.get('previous_action_ts', 0) < max(2, timing.settling)}
        rt['previous_action_ts'] = started
        STORE.event(agent['id'], 'info', 'ai_action', f'Control → {value}', intent.export())
        return self._result(intent, rt, 'ACCEPTED', f'Sent {domain}.{service}; waiting for acknowledgement', 'acted')

    def verify(self, agent):
        """A manual service-access probe is Control-only and uses the same boundary."""
        with self.target_lock(agent['target_entity']):
            agent = STORE.get_agent_config(agent['id'])
            if not agent or agent['mode'] != 'control' or not agent['enabled'] or agent.get('training_state') != 'qualified':
                raise ValueError('Verify sends a service and is available only in qualified Control')
            rt = self.engine.runtime.setdefault(agent['id'], {})
            if now_ts() < rt.get('manual_override_until', 0) or rt.get('pending'):
                raise ValueError('Manual override or pending command')
            self.take_control(agent)
            state = self.engine.state_map.get(agent['target_entity'])
            value = target_value(state, agent['target_property'])
            if value is None:
                raise ValueError('Target unavailable')
            value = legal_value(agent, state, value)
            domain, service, data = target_call(agent['target_entity'], agent['target_property'], value, state)
            self.engine.record_command(agent, value)
            response = self._service(domain, service, data)
            self.engine.record_command(agent, value, response)
            rt.update(last_service_ts=now_ts(), last_service=f'{domain}.{service}', last_service_ok=True)
            return {'ok': True, 'service': f'{domain}.{service}', 'service_data': data,
                    'current_value': value, 'ha_response': response}
