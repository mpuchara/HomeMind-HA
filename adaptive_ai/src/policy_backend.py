"""Versioned policy-backend contract and persistence metadata.

Stage 1 keeps the shipped diagonal policy as the only production-capable backend.  The
registry describes future backends without enabling them, so persisted models can be
identified safely before any backend-specific constructor is called.
"""
from abc import ABC, abstractmethod
import hashlib
import json


MODEL_FORMAT = "homemind-policy-model"
MODEL_FORMAT_VERSION = 1
LEGACY_DEFAULT_BACKEND = "diagonal_linucb"

BACKEND_CAPABILITIES = {
    "diagonal_linucb": {
        "implemented": True,
        "production_active_capable": True,
        "shadow_only": False,
        "historical_training": True,
    },
    "full_ridge_linucb": {
        "implemented": True,
        "production_active_capable": False,
        "shadow_only": True,
        "historical_training": True,
    },
    # Reserved contract only.  Stage 3 will provide the real implementation.
    "tiny_mlp": {
        "implemented": False,
        "production_active_capable": False,
        "shadow_only": True,
        "historical_training": False,
    },
}


class UnsupportedPolicyBackendError(ValueError):
    """Persisted model names a backend that this runtime must not reinterpret."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def model_checksum(raw):
    """Stable checksum of the serialized model, excluding the checksum field itself."""
    # Store-owned bookkeeping (for example _history_watermark/_benchmark_counts) is
    # appended after backend serialization and is intentionally outside model identity.
    clean = {
        key: value for key, value in dict(raw or {}).items()
        if key != "model_checksum" and not str(key).startswith("_")
    }
    return hashlib.sha256(_canonical(clean).encode("utf-8")).hexdigest()


def verify_model_checksum(raw):
    expected = (raw or {}).get("model_checksum")
    return True if not expected else str(expected) == model_checksum(raw)


def backend_id(raw):
    """Resolve persisted backend while preserving all pre-Stage-1 models as diagonal."""
    raw = dict(raw or {})
    explicit = raw.get("policy_backend")
    if explicit is None:
        explicit = raw.get("backend")
    return str(explicit or LEGACY_DEFAULT_BACKEND)


def backend_capabilities(name):
    row = BACKEND_CAPABILITIES.get(str(name))
    return dict(row) if row is not None else None


def require_backend(raw, *, expected=None, require_implemented=True):
    name = backend_id(raw)
    capabilities = BACKEND_CAPABILITIES.get(name)
    if capabilities is None:
        raise UnsupportedPolicyBackendError(f"NEEDS_RETRAIN: unknown policy backend {name!r}")
    if require_implemented and not capabilities.get("implemented"):
        raise UnsupportedPolicyBackendError(f"NEEDS_RETRAIN: policy backend {name!r} is not implemented")
    if expected is not None and name != str(expected):
        raise UnsupportedPolicyBackendError(
            f"NEEDS_RETRAIN: model backend {name!r} cannot be loaded as {str(expected)!r}"
        )
    return name


def feature_schema_id(raw):
    """Semantic schema identity, deliberately separate from the per-agent entity mask."""
    raw = dict(raw or {})
    schema = dict(raw.get("schema") or {})
    version = schema.get("version", "unknown")
    contract = schema.get("feature_contract_version", schema.get("contract_version", 1))
    dims = raw.get("dims", schema.get("dims", "unknown"))
    return f"schema-v{version}:contract-{contract}:dims-{dims}"


def feature_mask_id(raw):
    """Stable identity for the selected per-agent feature/entity mask."""
    raw = dict(raw or {})
    schema = dict(raw.get("schema") or {})
    payload = {}
    if "entities" in schema:
        payload["entities"] = list(schema.get("entities") or [])
    if "feature_indices" in raw:
        payload["feature_indices"] = [int(x) for x in (raw.get("feature_indices") or [])]
    if not payload:
        return None
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def serialize_backend_model(raw, *, policy_backend, backend_version,
                            schema_id=None, mask_id=None):
    """Attach the common Stage-1 envelope without changing backend-specific payloads."""
    name = str(policy_backend)
    capabilities = BACKEND_CAPABILITIES.get(name)
    if capabilities is None:
        raise UnsupportedPolicyBackendError(f"unknown policy backend {name!r}")
    out = dict(raw or {})
    out["model_format"] = MODEL_FORMAT
    out["model_format_version"] = MODEL_FORMAT_VERSION
    out["policy_backend"] = name
    out["backend_version"] = int(backend_version)
    out["feature_schema_id"] = str(schema_id or feature_schema_id(out))
    resolved_mask = mask_id if mask_id is not None else feature_mask_id(out)
    if resolved_mask is not None:
        out["feature_mask_id"] = str(resolved_mask)
    out["model_checksum"] = model_checksum(out)
    return out


class PolicyBackend(ABC):
    """Small common surface shared by current and future policy families."""

    BACKEND = None
    VERSION = None

    @abstractmethod
    def predict(self, features): ...

    @abstractmethod
    def update(self, horizon, action_idx, features, reward, sample_ts=None): ...

    @abstractmethod
    def serialize(self): ...

    @classmethod
    @abstractmethod
    def deserialize(cls, raw, **kwargs): ...

    @abstractmethod
    def decay(self, now=None): ...

    @abstractmethod
    def diagnostics(self): ...

    @classmethod
    def capabilities(cls):
        name = getattr(cls, "BACKEND", None)
        return backend_capabilities(name) if name else None
