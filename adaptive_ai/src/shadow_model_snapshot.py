"""Private frozen Shadow metadata plus an owned, immutable policy JSON source.

These objects never come from disk/network. Only our own metadata bytes are
unpickled; persistence remains plain JSON. Mutable/custom sources use the legacy
complete pickle snapshot. Policy JSON is budgeted on SealedPolicy admission.
"""
from dataclasses import dataclass
import pickle

from sealed_policy_snapshot import SealedPolicy
from shadow_model_json import shadow_model_json, _json


@dataclass(frozen=True, slots=True)
class ShadowModelSnapshot:
    metadata_bytes: bytes
    source: SealedPolicy

    def to_json(self):
        metadata = pickle.loads(self.metadata_bytes)
        # Restore the same sorted outer fields and body-then-bookkeeping source
        # encoding as shadow_model_json, retaining every field and every label.
        return '{' + ','.join(
            _json(key) + ':' + (self.source._persistence_json if key == 'candidate_policy' else _json(value))
            for key, value in sorted(dict(metadata, candidate_policy=None).items())
        ) + '}'


def freeze_shadow_model(model):
    if (type(model) is dict and all(type(key) is str for key in model)
            and type(model.get('candidate_policy')) is SealedPolicy
            and model['candidate_policy']._persistence_json is not None):
        metadata = dict(model)
        source = metadata.pop('candidate_policy')
        return ShadowModelSnapshot(pickle.dumps(metadata, protocol=5), source)
    return pickle.dumps(model, protocol=5)


def snapshot_json(raw):
    if isinstance(raw, str):
        return raw
    if type(raw) is ShadowModelSnapshot:
        return raw.to_json()
    return shadow_model_json(pickle.loads(raw))
