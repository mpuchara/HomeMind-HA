"""Frozen 0.14.171 dispatcher for paired targeted-retry regression benchmarks."""
from engine import OPTIONS, TRAINING_BUDGET, RUNTIME_DEBUG

def previous_process(self, state_map, changed_entities=None, *, event_driven=True):
    changed = set(changed_entities or ())
    if changed and event_driven:
        TRAINING_BUDGET.request_interactive_window(float(OPTIONS.get('training_realtime_inference_priority_seconds', 0.4)), reason='realtime_inference')
    agents = self._active_agents_for_changes(changed)
    groups = {}
    for agent in agents:
        groups.setdefault(agent['target_entity'], []).append(agent)
    with self.lock:
        pass_states = dict(self.state_map) if self.state_map else dict(state_map or {})
        pass_revision = self.state_revision
        revision_snapshot = dict(self.entity_revisions)
        context_revision = self.context.home.revision
        dependency_snapshot = {str(eid): set(agent_ids) for eid, agent_ids in self.dependency_agents.items()}
        event_received_all = {eid: self.entity_event_received_perf.get(eid) for eid in changed if self.entity_event_received_perf.get(eid) is not None} if event_driven else {}
    snapshot = (pass_states, pass_revision, revision_snapshot, context_revision)
    for target, target_agents in groups.items():
        target_agent_ids = {str(agent.get('id') or '') for agent in target_agents}
        target_changed = {str(eid) for eid in changed if target_agent_ids.intersection(dependency_snapshot.get(str(eid), ()))}
        if not target_changed and changed and (not event_driven):
            target_changed = set(changed)
        target_event_received = {eid: event_received_all[eid] for eid in target_changed if eid in event_received_all}
        active = self.in_flight.get(target)
        if active is not None and (not active.done()):
            if event_driven and target_changed:
                install_callback = False
                with self.lock:
                    pending = self.pending_target_changes.setdefault(target, set())
                    pending.update(target_changed)
                    if target not in self.resubmit_targets:
                        self.resubmit_targets.add(target)
                        install_callback = True
                if install_callback:

                    def retry_completed(_future, entity=target):
                        with self.lock:
                            pending_changes = set(self.pending_target_changes.pop(entity, set()))
                            self.resubmit_targets.discard(entity)
                            self.dirty_entities.update(pending_changes)
                        if pending_changes:
                            self.wake_event.set()
                    active.add_done_callback(retry_completed)
            continue
        self.in_flight[target] = self.control_workers.submit(self.process_target, target_agents, target_changed, snapshot, target_event_received)
    for target in list(self.in_flight):
        if target not in groups and self.in_flight[target].done():
            del self.in_flight[target]
