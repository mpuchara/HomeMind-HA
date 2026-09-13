"""Persistent ownership journal stored in app_meta."""
import json
from settings import iso_now


class LeaseJournal:
    INDEX_KEY = 'control_lease_index'

    def __init__(self, store):
        self.store = store

    @staticmethod
    def key(target_entity):
        return 'control_lease:' + str(target_entity)

    def index(self):
        try:
            raw = json.loads(self.store.meta_get(self.INDEX_KEY, '[]') or '[]')
            return [str(x) for x in raw] if isinstance(raw, list) else []
        except Exception:
            return []

    def write_index(self, values):
        self.store.meta_set(self.INDEX_KEY, json.dumps(sorted(set(values)), separators=(',', ':')))

    def get(self, target_entity):
        raw = self.store.meta_get(self.key(target_entity), '')
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def save(self, agent, disabled_automations):
        target = agent['target_entity']
        existing = self.get(target)
        if existing and existing.get('agent_id') != agent['id']:
            raise RuntimeError('Target ownership journal belongs to another agent')
        value = {
            'agent_id': agent['id'],
            'target_entity': target,
            'disabled_automations': sorted(set(disabled_automations or [])),
            'created_at': (existing or {}).get('created_at') or iso_now(),
            'updated_at': iso_now(),
        }
        self.store.meta_set(self.key(target), json.dumps(value, separators=(',', ':'), ensure_ascii=False))
        idx = self.index()
        if target not in idx:
            idx.append(target)
            self.write_index(idx)
        return value

    def keep_remaining(self, target_entity, automation_ids, reason=None):
        lease = self.get(target_entity)
        if not lease:
            return
        lease['disabled_automations'] = sorted(set(automation_ids or []))
        lease['updated_at'] = iso_now()
        if reason:
            lease['last_release_reason'] = str(reason)
        self.store.meta_set(self.key(target_entity), json.dumps(lease, separators=(',', ':'), ensure_ascii=False))

    def clear(self, target_entity):
        self.store.meta_set(self.key(target_entity), '')
        self.write_index([x for x in self.index() if x != target_entity])

    def all(self):
        return [lease for target in self.index() if (lease := self.get(target))]
