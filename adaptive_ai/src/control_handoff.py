"""Persistent transactional handoff used when an agent enters/leaves Control."""
from lease_journal import LeaseJournal
from handoff import HandoffError, acquire_transaction, restore_items
from qualification import assess_control_qualification
from control import review_status


class ControlHandoff:
    def __init__(self, store, state_map, refresh, scan, knowledge, disable_one, restore_one):
        self.store = store
        self.state_map = state_map
        self.refresh = refresh
        self.scan = scan
        self.knowledge = knowledge
        self.disable_one = disable_one
        self.restore_one = restore_one
        self.journal = LeaseJournal(store)

    def release(self, agent, reason='mode_change'):
        lease = self.journal.get(agent['target_entity'])
        if not lease:
            return []
        if lease.get('agent_id') != agent['id']:
            raise RuntimeError('Control lease belongs to another agent')
        current = self.state_map()
        owned = list(lease.get('disabled_automations') or [])
        pending = [eid for eid in owned if (current.get(eid) or {}).get('state') != 'on']
        restored, failed = restore_items(pending, self.restore_one)
        if restored:
            try:
                self.refresh()
            except Exception:
                pass
        if failed:
            remaining = [x['item'] for x in failed]
            self.journal.keep_remaining(agent['target_entity'], remaining, reason)
            self.store.event(agent['id'], 'error', 'control_restore_partial',
                             'Some previous controllers could not be restored',
                             {'reason': reason, 'restored': restored, 'failed': failed})
            raise RuntimeError('Could not restore: ' + ', '.join(remaining))
        self.journal.clear(agent['target_entity'])
        with self.knowledge.lock:
            for info in self.knowledge.automations:
                if info.get('entity_id') in set(owned):
                    info['enabled'] = True
        if owned:
            self.store.event(agent['id'], 'info', 'control_released',
                             'Control released; previous controllers restored',
                             {'reason': reason, 'restored': restored})
        return restored

    def acquire(self, agent, refresh_scan=True):
        qualification = assess_control_qualification(agent)
        if not qualification['passed']:
            raise ValueError('Control qualification: ' + qualification['reason'])

        lease = self.journal.get(agent['target_entity'])
        if lease:
            if lease.get('agent_id') != agent['id']:
                raise ValueError('Another Control lease owns this entity')
            if agent.get('mode') == 'control':
                return []
            self.release(agent, 'retry_interrupted_handoff')

        if refresh_scan:
            self.refresh()
            self.scan()

        current = self.state_map()
        review = review_status(self.store, agent, current.get(agent['target_entity']))
        if not review['ready']:
            if review['approval_required'] and not review['approved']:
                raise ValueError('Generic number/select targets require explicit Control review')
            raise ValueError('Device capabilities changed since Control review')

        _, infos = self.knowledge.hints_for_target(agent['target_entity'])
        candidates = []
        for info in infos:
            eid = info.get('entity_id')
            if not eid or not eid.startswith('automation.'):
                continue
            state = current.get(eid)
            if state is None:
                raise RuntimeError('Controller state unavailable: ' + eid)
            if state.get('state') != 'off':
                candidates.append(eid)

        def checkpoint(changed):
            self.journal.save(agent, changed)

        def confirm(changed):
            if not changed:
                return
            self.refresh()
            not_off = [eid for eid in changed if self.state_map().get(eid, {}).get('state') != 'off']
            if not_off:
                raise RuntimeError('Controller OFF not confirmed: ' + ', '.join(not_off))

        try:
            changed = acquire_transaction(candidates, self.disable_one, self.restore_one, checkpoint, confirm)
        except HandoffError as exc:
            if exc.failed:
                remaining = [x['item'] for x in exc.failed]
                self.journal.keep_remaining(agent['target_entity'], remaining, 'takeover_rollback')
                self.store.event(agent['id'], 'error', 'control_handoff_rollback_partial',
                                 'Control handoff failed and rollback was incomplete',
                                 {'error': str(exc.cause), 'restored': exc.restored, 'failed': exc.failed})
                raise RuntimeError(str(exc.cause) + '; rollback incomplete for: ' + ', '.join(remaining)) from exc
            self.journal.clear(agent['target_entity'])
            if exc.changed:
                self.store.event(agent['id'], 'warning', 'control_handoff_rolled_back',
                                 'Control handoff failed; previous controllers were restored',
                                 {'error': str(exc.cause), 'restored': exc.restored})
            raise exc.cause

        self.journal.save(agent, changed)
        with self.knowledge.lock:
            for info in self.knowledge.automations:
                if info.get('entity_id') in set(changed):
                    info['enabled'] = False
        if changed:
            self.store.event(agent['id'], 'info', 'control_handoff_committed',
                             'Control handoff committed with persistent lease',
                             {'disabled': changed})
        return changed

    def reconcile(self):
        kept, released, failed = [], [], []
        for lease in list(self.journal.all()):
            agent = self.store.get_agent_config(lease.get('agent_id'))
            current = self.state_map()
            qualification = assess_control_qualification(agent) if agent else {'passed': False}
            review = review_status(self.store, agent, current.get(lease.get('target_entity'))) if agent else {'ready': False}
            active = bool(agent and agent.get('enabled') and agent.get('mode') == 'control'
                          and agent.get('training_state') == 'qualified'
                          and qualification.get('passed') and review.get('ready'))
            if active:
                kept.append(lease.get('target_entity'))
                continue
            release_agent = agent or {'id': lease.get('agent_id'), 'target_entity': lease.get('target_entity')}
            try:
                self.release(release_agent, 'startup_reconcile')
                released.append(lease.get('target_entity'))
            except Exception as exc:
                failed.append({'target_entity': lease.get('target_entity'), 'error': str(exc)})
        return {'kept': kept, 'released': released, 'failed': failed}

    def release_all(self, reason='shutdown'):
        errors = []
        for lease in list(self.journal.all()):
            agent = self.store.get_agent_config(lease.get('agent_id')) or {
                'id': lease.get('agent_id'), 'target_entity': lease.get('target_entity')
            }
            try:
                self.release(agent, reason)
            except Exception as exc:
                errors.append({'agent_id': agent.get('id'), 'error': str(exc)})
        return errors
