"""Stage 15: registry-backed DeviceAgent/DeviceCapabilities and shared-resource arbitration.

The existing logical agent id remains the owner of policy/history/generation data. This
module adds a device/resource identity above Home Assistant entities so two properties or
two entities of one physical device cannot independently believe that they own it.

Identity is deliberately conservative:
- an explicit mapping row wins;
- otherwise Home Assistant Entity Registry ``device_id`` is used;
- otherwise the exact entity_id is the fallback.
Friendly names are never used for identity.

Executor remains the only Home Assistant service dispatcher. The arbiter only returns
capabilities, legal-action masks and durable reservations/holds.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, asdict
import json
import threading
import time

from context import target_call, target_options_for_state
from control import legal_value, timing_for


CONTRACT_VERSION = 2
PROCESS_MODEL_CONTRACT_VERSION = 1
PERCEPTION_OWNER = "perception_service"
CONFIG_CATEGORIES = {"config", "diagnostic"}
GENERIC_UNDESCRIBED_DOMAINS = {"switch", "number", "input_number", "select", "input_select"}
PROCESS_DOMAINS = {"climate", "cover", "water_heater", "humidifier"}


@dataclass(frozen=True)
class ProcessModelBackendContract:
    version: int = PROCESS_MODEL_CONTRACT_VERSION
    role: str = "process_model"
    required_for_long_horizon_autonomy: bool = True
    supports_state_estimation: bool = True
    supports_constraints: bool = True
    supports_rollout_horizon: bool = True
    lighting_bandit_is_sufficient: bool = False
    implementation_ready: bool = False

    def export(self):
        return asdict(self)


def _ensure_column(c, table, column, ddl):
    cols = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_tables(store):
    """Additive migration only; existing model vectors and agent ids are untouched."""
    with store.lock, store.conn() as c:
        _ensure_column(c, "agents", "logical_device_id", "TEXT")
        _ensure_column(c, "agents", "device_property", "TEXT")
        _ensure_column(c, "agents", "device_contract_version", "INTEGER NOT NULL DEFAULT 1")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS device_explicit_mappings (
                entity_id TEXT PRIMARY KEY,
                logical_device_id TEXT NOT NULL,
                property_name TEXT,
                autonomy_enabled INTEGER NOT NULL DEFAULT 0,
                resource_group TEXT,
                zone_id TEXT,
                updated_ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device_resource_state (
                resource_key TEXT PRIMARY KEY,
                owner_agent_id TEXT,
                owner_intent_id TEXT,
                lease_until REAL NOT NULL DEFAULT 0,
                last_dispatch_ts REAL,
                last_dispatch_agent_id TEXT,
                last_action_json TEXT,
                manual_hold_until REAL NOT NULL DEFAULT 0,
                control_owner_agent_id TEXT,
                control_acquired_ts REAL,
                updated_ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS perception_resource_leases (
                resource_key TEXT PRIMARY KEY,
                owner_service TEXT NOT NULL,
                consumers_json TEXT NOT NULL DEFAULT '[]',
                lease_until REAL NOT NULL,
                config_snapshot_json TEXT NOT NULL DEFAULT '{}',
                updated_ts REAL NOT NULL
            );
            """
        )
        _ensure_column(c, "device_resource_state", "last_dispatch_agent_id", "TEXT")
        _ensure_column(c, "device_resource_state", "control_owner_agent_id", "TEXT")
        _ensure_column(c, "device_resource_state", "control_acquired_ts", "REAL")


def _safe_json(value, fallback):
    if isinstance(value, type(fallback)):
        return value
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except Exception:
        return fallback


class DeviceAgentService:
    """Device capabilities plus deterministic, persistent shared-resource arbitration."""

    def __init__(self, engine, store):
        self.engine = engine
        self.store = store
        self._locks = {}
        self._locks_guard = threading.RLock()
        ensure_tables(store)

    # ------------------------------------------------------------------
    # Explicit mapping / identity
    # ------------------------------------------------------------------
    def set_explicit_mapping(self, entity_id, logical_device_id, *, property_name=None,
                             autonomy_enabled=False, resource_group=None, zone_id=None):
        entity_id = str(entity_id)
        logical_device_id = str(logical_device_id).strip()
        if not logical_device_id:
            raise ValueError("logical_device_id is required")
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO device_explicit_mappings
                   (entity_id,logical_device_id,property_name,autonomy_enabled,resource_group,zone_id,updated_ts)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                     logical_device_id=excluded.logical_device_id,
                     property_name=excluded.property_name,
                     autonomy_enabled=excluded.autonomy_enabled,
                     resource_group=excluded.resource_group,
                     zone_id=excluded.zone_id,
                     updated_ts=excluded.updated_ts""",
                (entity_id, logical_device_id, property_name, int(bool(autonomy_enabled)),
                 resource_group, zone_id, time.time()),
            )
        # Explicit operator mapping is authoritative immediately. Physical target_property
        # is untouched; only the additive logical property/device metadata is migrated.
        self.migrate_agent_identities(entity_ids={entity_id})
        self.reconcile_control_resources()
        return self.explicit_mapping(entity_id)

    def explicit_mapping(self, entity_id):
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM device_explicit_mappings WHERE entity_id=?", (str(entity_id),)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["autonomy_enabled"] = bool(out.get("autonomy_enabled"))
        return out

    def _registry(self):
        context = getattr(self.engine, "context", None)
        if context is None:
            return {}, {}
        try:
            entities = dict(context.resolved_registry())
        except Exception:
            entities = dict(getattr(self.engine, "entity_registry", {}) or {})
        devices = dict(getattr(context, "devices", {}) or {})
        return entities, devices

    def identity_for_entity(self, entity_id):
        entity_id = str(entity_id)
        explicit = self.explicit_mapping(entity_id)
        entities, devices = self._registry()
        reg = dict(entities.get(entity_id) or {})
        device_id = reg.get("device_id")
        device = dict(devices.get(device_id) or {}) if device_id else {}
        area_id = (explicit or {}).get("zone_id") or reg.get("area_id") or device.get("area_id")
        if explicit:
            logical = str(explicit["logical_device_id"])
            source = "explicit_mapping"
        elif device_id:
            logical = "ha-device:" + str(device_id)
            source = "ha_device_registry"
        else:
            logical = "entity:" + entity_id
            source = "exact_entity_fallback"
        return {
            "entity_id": entity_id,
            "logical_device_id": logical,
            "identity_source": source,
            "device_id": device_id,
            "area_id": area_id,
            "entity_category": reg.get("entity_category"),
            "disabled_by": reg.get("disabled_by"),
            "hidden_by": reg.get("hidden_by"),
            "explicit_mapping": explicit,
        }

    def logical_property_for(self, agent, identity=None):
        identity = identity or self.identity_for_entity(agent["target_entity"])
        explicit = identity.get("explicit_mapping") or {}
        return str(explicit.get("property_name") or agent.get("target_property") or "")

    def primary_resource_for_entity(self, entity_id):
        return "device:" + self.identity_for_entity(entity_id)["logical_device_id"]

    def _entities_for_identity(self, identity):
        logical = identity["logical_device_id"]
        entities, _ = self._registry()
        out = []
        for eid, reg in entities.items():
            mapped = self.explicit_mapping(eid)
            if mapped and str(mapped.get("logical_device_id")) == logical:
                out.append(eid)
                continue
            if not mapped and identity.get("device_id") and reg.get("device_id") == identity.get("device_id"):
                out.append(eid)
        if identity["entity_id"] not in out:
            out.append(identity["entity_id"])
        return sorted(set(out))

    def migrate_agent_identity(self, agent):
        """Persist additive logical metadata without changing physical target semantics."""
        if not agent:
            return None
        identity = self.identity_for_entity(agent["target_entity"])
        logical = identity["logical_device_id"]
        prop = self.logical_property_for(agent, identity)
        if (agent.get("logical_device_id") != logical or agent.get("device_property") != prop
                or int(agent.get("device_contract_version") or 0) != CONTRACT_VERSION):
            with self.store.lock, self.store.conn() as c:
                c.execute(
                    """UPDATE agents SET logical_device_id=?,device_property=?,device_contract_version=? WHERE id=?""",
                    (logical, prop, CONTRACT_VERSION, str(agent["id"])),
                )
        return {"agent_id": str(agent["id"]), "logical_device_id": logical, "device_property": prop}

    def migrate_agent_identities(self, entity_ids=None):
        wanted = set(str(x) for x in (entity_ids or ()))
        migrated = []
        for agent in self.store.list_agent_configs():
            if wanted and str(agent.get("target_entity")) not in wanted:
                continue
            result = self.migrate_agent_identity(agent)
            if result:
                migrated.append(result)
        return migrated

    # ------------------------------------------------------------------
    # Capabilities / backend semantics
    # ------------------------------------------------------------------
    def backend_contract(self, agent):
        domain = str(agent.get("target_entity") or "").split(".", 1)[0]
        if domain in PROCESS_DOMAINS:
            return {
                "kind": "process_model_required",
                "current_fast_bandit_role": "shadow_or_explicit_compatibility_only",
                "process_model": ProcessModelBackendContract().export(),
            }
        return {"kind": "contextual_bandit", "process_model": None, "long_horizon_comfort_claim": False}

    def descriptor(self, agent):
        # Read-only on the decision path. Registry/mapping callbacks perform persistence.
        identity = self.identity_for_entity(agent["target_entity"])
        domain = str(agent["target_entity"]).split(".", 1)[0]
        explicit = identity.get("explicit_mapping") or {}
        states = dict(getattr(self.engine, "state_map", {}) or {})
        entity_ids = self._entities_for_identity(identity)
        properties = []
        for eid in entity_ids:
            state = states.get(eid)
            for option in target_options_for_state(state) if state else []:
                properties.append({"entity_id": eid, "property": option.get("property")})
        properties = [
            {"entity_id": eid, "property": prop}
            for eid, prop in sorted({(x["entity_id"], x["property"]) for x in properties})
        ]

        auto_created = bool(agent.get("auto_created"))
        config_owned = identity.get("entity_category") in CONFIG_CATEGORIES
        explicit_autonomy = bool(explicit.get("autonomy_enabled"))
        if config_owned:
            autonomy_allowed = False
            autonomy_reason = "configuration entities belong to the perception service"
        elif auto_created and domain in GENERIC_UNDESCRIBED_DOMAINS and not explicit_autonomy:
            autonomy_allowed = False
            autonomy_reason = "generic switch/number/select needs an explicit device description"
        elif auto_created and domain in PROCESS_DOMAINS and not explicit_autonomy:
            autonomy_allowed = False
            autonomy_reason = "long-horizon device requires a process-model backend or explicit operator description"
        else:
            autonomy_allowed = True
            autonomy_reason = "manual agent or described domain contract"

        resource_keys = ["device:" + identity["logical_device_id"]]
        if explicit.get("resource_group"):
            resource_keys.append("group:" + str(explicit["resource_group"]))
        zone = identity.get("area_id")
        if zone and domain in PROCESS_DOMAINS:
            resource_keys.append(f"zone:{zone}:solar_shading" if domain == "cover" else f"zone:{zone}:thermal")

        return {
            "contract_version": CONTRACT_VERSION,
            **identity,
            "device_property": self.logical_property_for(agent, identity),
            "physical_target_property": str(agent.get("target_property") or ""),
            "entities": entity_ids,
            "properties": properties,
            "resource_keys": sorted(set(resource_keys)),
            "configuration_owner": PERCEPTION_OWNER if config_owned else None,
            "autonomy_allowed": autonomy_allowed,
            "autonomy_reason": autonomy_reason,
            "backend": self.backend_contract(agent),
            "compound_action": {
                "light_brightness_is_complete_policy": domain == "light",
                "brightness_zero_means_power_off": domain == "light",
                "separate_power_and_brightness_owners_allowed": False if domain == "light" else None,
            },
        }

    def control_eligibility(self, agent):
        desc = self.descriptor(agent)
        return {"allowed": bool(desc["autonomy_allowed"]),
                "reason": None if desc["autonomy_allowed"] else desc["autonomy_reason"],
                "descriptor": desc}

    def conflicts(self, left, right):
        if not left or not right or str(left.get("id")) == str(right.get("id")):
            return False
        return bool(set(self.descriptor(left)["resource_keys"]) & set(self.descriptor(right)["resource_keys"]))

    # ------------------------------------------------------------------
    # Locks / durable reservations
    # ------------------------------------------------------------------
    def _lock_for(self, resource_key):
        with self._locks_guard:
            return self._locks.setdefault(str(resource_key), threading.RLock())

    @contextmanager
    def lock_resources(self, resource_keys):
        keys = sorted(set(str(x) for x in resource_keys if x))
        # Canonical ordering prevents A(device,zone) / B(zone,device) deadlocks.
        with ExitStack() as stack:
            for key in keys:
                stack.enter_context(self._lock_for(key))
            yield

    def lock_for_entity(self, entity_id):
        return self._lock_for(self.primary_resource_for_entity(entity_id))

    def _resource_rows(self, keys):
        with self.store.conn() as c:
            rows = c.execute(
                "SELECT * FROM device_resource_state WHERE resource_key IN (%s)" % ",".join("?" for _ in keys),
                tuple(keys),
            ).fetchall() if keys else []
        return {row["resource_key"]: dict(row) for row in rows}

    def claim_control_resources(self, agent, *, now=None):
        """Durably reserve every conflicting resource before the caller commits mode=control."""
        now = time.time() if now is None else float(now)
        keys = self.descriptor(agent)["resource_keys"]
        newly_claimed = []
        with self.lock_resources(keys):
            with self.store.lock, self.store.conn() as c:
                rows = {row["resource_key"]: dict(row) for row in c.execute(
                    "SELECT * FROM device_resource_state WHERE resource_key IN (%s)" % ",".join("?" for _ in keys),
                    tuple(keys),
                ).fetchall()} if keys else {}
                for key in keys:
                    owner = str((rows.get(key) or {}).get("control_owner_agent_id") or "")
                    if owner and owner != str(agent["id"]):
                        return None
                    if owner != str(agent["id"]):
                        newly_claimed.append(key)
                for key in keys:
                    c.execute(
                        """INSERT INTO device_resource_state
                           (resource_key,control_owner_agent_id,control_acquired_ts,updated_ts)
                           VALUES(?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET
                           control_owner_agent_id=excluded.control_owner_agent_id,
                           control_acquired_ts=CASE
                             WHEN device_resource_state.control_owner_agent_id=excluded.control_owner_agent_id
                               THEN device_resource_state.control_acquired_ts
                             ELSE excluded.control_acquired_ts END,
                           updated_ts=excluded.updated_ts""",
                        (key, str(agent["id"]), now, now),
                    )
        return {
            "resource_keys": keys,
            "newly_claimed_keys": newly_claimed,
            "agent_id": str(agent["id"]),
            "acquired_ts": now,
        }

    def release_control_resources(self, agent, *, now=None, resource_keys=None):
        now = time.time() if now is None else float(now)
        aid = str(agent["id"])
        with self.store.conn() as c:
            owned = [
                str(row["resource_key"]) for row in c.execute(
                    "SELECT resource_key FROM device_resource_state WHERE control_owner_agent_id=?",
                    (aid,),
                ).fetchall()
            ]
        keys = sorted(set(str(x) for x in (resource_keys if resource_keys is not None else owned)))
        if not keys:
            return True
        with self.lock_resources(keys):
            with self.store.lock, self.store.conn() as c:
                for key in keys:
                    c.execute(
                        """UPDATE device_resource_state
                           SET control_owner_agent_id=NULL,control_acquired_ts=NULL,updated_ts=?
                           WHERE resource_key=? AND control_owner_agent_id=?""",
                        (now, key, aid),
                    )
        return True

    def control_owner_conflict(self, agent):
        keys = self.descriptor(agent)["resource_keys"]
        rows = self._resource_rows(keys)
        owners = sorted({
            str(row.get("control_owner_agent_id"))
            for row in rows.values()
            if row.get("control_owner_agent_id")
            and str(row.get("control_owner_agent_id")) != str(agent["id"])
        })
        return owners

    def reconcile_control_resources(self):
        """Repair stale claims conservatively; never choose between conflicting Control agents."""
        agents = [a for a in self.store.list_agent_configs() if a.get("enabled")]
        by_id = {str(a["id"]): a for a in agents}
        control_ids = {str(a["id"]) for a in agents if a.get("mode") == "control"}
        desired_resources = {
            aid: set(self.descriptor(by_id[aid])["resource_keys"])
            for aid in control_ids
        }
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            rows = c.execute(
                "SELECT resource_key,control_owner_agent_id FROM device_resource_state "
                "WHERE control_owner_agent_id IS NOT NULL"
            ).fetchall()
            stale_keys = []
            for row in rows:
                key = str(row["resource_key"])
                owner = str(row["control_owner_agent_id"] or "")
                if owner not in control_ids or key not in desired_resources.get(owner, set()):
                    stale_keys.append(key)
            for key in stale_keys:
                c.execute(
                    """UPDATE device_resource_state
                       SET control_owner_agent_id=NULL,control_acquired_ts=NULL,updated_ts=?
                       WHERE resource_key=?""",
                    (now, key),
                )
        conflicts = []
        claimed = []
        for agent in sorted((by_id[x] for x in control_ids), key=lambda a: str(a["id"])):
            peers = [
                other for other in agents
                if other.get("mode") == "control" and str(other["id"]) != str(agent["id"])
                and self.conflicts(agent, other)
            ]
            if peers:
                conflicts.append({
                    "agent_id": str(agent["id"]),
                    "conflicts_with": sorted(str(x["id"]) for x in peers),
                })
                continue
            if self.claim_control_resources(agent, now=now):
                claimed.append(str(agent["id"]))
        return {"claimed": claimed, "conflicts": conflicts}

    def _persist_runtime_manual_holds(self, agent, runtime_by_agent=None):
        now = time.time()
        desc = self.descriptor(agent)
        hold = 0.0
        for other in self.store.list_agent_configs():
            if not self.conflicts(agent, other):
                continue
            runtime = (runtime_by_agent or {}).get(other["id"], {}) if runtime_by_agent is not None else {}
            value = float(runtime.get("manual_override_until") or 0.0)
            if self.store.meta_get("manual_hold_source:" + other["id"], "") == "explicit_user_v8":
                try:
                    value = max(value, float(self.store.meta_get("manual_hold:" + other["id"], "0") or 0.0))
                except Exception:
                    pass
            hold = max(hold, value)
        if hold > now:
            with self.store.lock, self.store.conn() as c:
                for key in desc["resource_keys"]:
                    c.execute(
                        """INSERT INTO device_resource_state(resource_key,manual_hold_until,updated_ts)
                           VALUES(?,?,?) ON CONFLICT(resource_key) DO UPDATE SET
                           manual_hold_until=MAX(device_resource_state.manual_hold_until,excluded.manual_hold_until),
                           updated_ts=excluded.updated_ts""",
                        (key, hold, now),
                    )
        return hold

    def shared_manual_hold_until(self, agent, runtime_by_agent=None):
        observed = self._persist_runtime_manual_holds(agent, runtime_by_agent)
        keys = self.descriptor(agent)["resource_keys"]
        rows = self._resource_rows(keys)
        durable = max([float((rows.get(key) or {}).get("manual_hold_until") or 0.0) for key in keys] or [0.0])
        return max(observed, durable)

    def min_dwell_seconds(self, agent):
        timing = timing_for(agent)
        return max(float(agent.get("action_interval") or 0.0), float(timing.settling))

    def active_lease(self, agent, now=None):
        now = time.time() if now is None else float(now)
        rows = self._resource_rows(self.descriptor(agent)["resource_keys"])
        return [row for row in rows.values() if float(row.get("lease_until") or 0.0) > now]

    def legal_action_mask(self, agent, state, values, *, runtime_by_agent=None, now=None):
        now = time.time() if now is None else float(now)
        desc = self.descriptor(agent)
        eligibility = self.control_eligibility(agent)
        hold_until = self.shared_manual_hold_until(agent, runtime_by_agent)
        leases = self.active_lease(agent, now)
        rows = self._resource_rows(desc["resource_keys"])
        control_conflict = any(
            row.get("control_owner_agent_id")
            and str(row.get("control_owner_agent_id")) != str(agent["id"])
            for row in rows.values()
        )
        # Same-agent cooldown/pending is already enforced by Executor. Shared dwell exists
        # to keep a sibling property/entity from immediately fighting the last command.
        sibling_dwell = False
        dwell = self.min_dwell_seconds(agent)
        for row in rows.values():
            last = float(row.get("last_dispatch_ts") or 0.0)
            last_agent = str(row.get("last_dispatch_agent_id") or "")
            if last and last_agent and last_agent != str(agent["id"]) and now - last < dwell:
                sibling_dwell = True
                break
        out = []
        for desired in values:
            legal, reason, value = True, None, desired
            if not eligibility["allowed"]:
                legal, reason = False, eligibility["reason"]
            elif hold_until > now:
                legal, reason = False, "manual override has priority for this shared resource"
            elif control_conflict:
                legal, reason = False, "shared resource Control ownership belongs to another agent"
            elif leases:
                legal, reason = False, "shared resource in-flight lease is active"
            elif sibling_dwell:
                legal, reason = False, "shared resource minimum dwell is active"
            else:
                try:
                    value = legal_value(agent, state, desired)
                except (TypeError, ValueError) as exc:
                    legal, reason = False, str(exc)
            out.append({"requested": desired, "value": value, "legal": legal, "reason": reason})
        return {
            "resource_keys": desc["resource_keys"],
            "actions": out,
            "fallback": "abstain" if not any(x["legal"] for x in out) else "policy_choice_within_mask",
            "manual_priority": True,
            "min_dwell_seconds": dwell,
        }

    def reserve_dispatch(self, agent, intent_id, *, now=None, ttl=None):
        now = time.time() if now is None else float(now)
        desc = self.descriptor(agent)
        keys = desc["resource_keys"]
        # This is the authoritative atomic resource check immediately before dispatch.
        # legal_action_mask() is advisory for policy/UI; every hard guard that can race
        # across sibling devices must be repeated while holding the canonical resource locks.
        if not self.control_eligibility(agent)["allowed"]:
            return None
        dwell = self.min_dwell_seconds(agent)
        ttl = max(0.1, float(ttl or dwell))
        until = now + ttl
        with self.lock_resources(keys):
            with self.store.lock, self.store.conn() as c:
                rows = {row["resource_key"]: dict(row) for row in c.execute(
                    "SELECT * FROM device_resource_state WHERE resource_key IN (%s)" % ",".join("?" for _ in keys),
                    tuple(keys),
                ).fetchall()} if keys else {}
                for key in keys:
                    row = rows.get(key) or {}
                    if float(row.get("manual_hold_until") or 0.0) > now:
                        return None
                    control_owner = str(row.get("control_owner_agent_id") or "")
                    if control_owner and control_owner != str(agent["id"]):
                        return None
                    if (float(row.get("lease_until") or 0.0) > now
                            and str(row.get("owner_intent_id") or "") != str(intent_id)):
                        return None
                    last = float(row.get("last_dispatch_ts") or 0.0)
                    last_agent = str(row.get("last_dispatch_agent_id") or "")
                    if last and last_agent and last_agent != str(agent["id"]) and now - last < dwell:
                        return None
                for key in keys:
                    c.execute(
                        """INSERT INTO device_resource_state
                           (resource_key,owner_agent_id,owner_intent_id,lease_until,updated_ts)
                           VALUES(?,?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET
                           owner_agent_id=excluded.owner_agent_id,
                           owner_intent_id=excluded.owner_intent_id,
                           lease_until=excluded.lease_until,
                           updated_ts=excluded.updated_ts""",
                        (key, str(agent["id"]), str(intent_id), until, now),
                    )
        return {"resource_keys": keys, "agent_id": str(agent["id"]),
                "intent_id": str(intent_id), "lease_until": until,
                "min_dwell_seconds": dwell}

    def finish_dispatch(self, reservation, *, success, action=None, now=None):
        if not reservation:
            return
        now = time.time() if now is None else float(now)
        keys = list(reservation.get("resource_keys") or [])
        packed = json.dumps(action or {}, separators=(",", ":"), default=str)
        with self.lock_resources(keys):
            with self.store.lock, self.store.conn() as c:
                for key in keys:
                    row = c.execute(
                        "SELECT owner_intent_id FROM device_resource_state WHERE resource_key=?", (key,)
                    ).fetchone()
                    if not row or str(row[0] or "") != str(reservation.get("intent_id") or ""):
                        continue
                    if success:
                        c.execute(
                            """UPDATE device_resource_state SET
                               owner_agent_id=NULL,owner_intent_id=NULL,lease_until=0,
                               last_dispatch_ts=?,last_dispatch_agent_id=?,last_action_json=?,updated_ts=?
                               WHERE resource_key=?""",
                            (now, str(reservation.get("agent_id") or ""), packed, now, key),
                        )
                    else:
                        c.execute(
                            """UPDATE device_resource_state SET owner_agent_id=NULL,owner_intent_id=NULL,
                               lease_until=0,updated_ts=? WHERE resource_key=?""",
                            (now, key),
                        )

    # ------------------------------------------------------------------
    # Perception-owned configuration lease
    # ------------------------------------------------------------------
    def acquire_perception_lease(self, sensor_entity_id, consumer_id, *, owner_service=PERCEPTION_OWNER,
                                 ttl_seconds=300.0, config_snapshot=None, now=None):
        if str(owner_service) != PERCEPTION_OWNER:
            raise ValueError("sensor configuration may only be owned by the perception service")
        now = time.time() if now is None else float(now)
        ttl = max(1.0, float(ttl_seconds))
        resource = "sensor-config:" + self.identity_for_entity(sensor_entity_id)["logical_device_id"]
        with self.lock_resources([resource]):
            with self.store.lock, self.store.conn() as c:
                row = c.execute(
                    "SELECT * FROM perception_resource_leases WHERE resource_key=?", (resource,)
                ).fetchone()
                existing = dict(row) if row else None
                active = bool(existing and float(existing.get("lease_until") or 0.0) > now)
                if active and existing.get("owner_service") != PERCEPTION_OWNER:
                    raise RuntimeError("sensor configuration lease is owned by another service")
                existing_snapshot = _safe_json((existing or {}).get("config_snapshot_json"), {})
                if active:
                    consumers = set(_safe_json(existing.get("consumers_json"), []))
                    # The snapshot is the pre-change restore point. It is immutable for one
                    # active lease generation; later consumers may not silently replace it.
                    if config_snapshot is not None and existing_snapshot and dict(config_snapshot) != existing_snapshot:
                        raise ValueError("active sensor configuration snapshot is immutable")
                    snapshot = existing_snapshot or dict(config_snapshot or {})
                else:
                    # Expired leases are a new generation: stale consumers/snapshots cannot
                    # leak across restart/reacquisition.
                    consumers = set()
                    snapshot = dict(config_snapshot or {})
                consumers.add(str(consumer_id))
                c.execute(
                    """INSERT INTO perception_resource_leases
                       (resource_key,owner_service,consumers_json,lease_until,config_snapshot_json,updated_ts)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET
                       owner_service=excluded.owner_service,
                       consumers_json=excluded.consumers_json,
                       lease_until=excluded.lease_until,
                       config_snapshot_json=excluded.config_snapshot_json,
                       updated_ts=excluded.updated_ts""",
                    (resource, PERCEPTION_OWNER, json.dumps(sorted(consumers)), now + ttl,
                     json.dumps(snapshot, separators=(",", ":"), default=str), now),
                )
        return self.perception_lease(sensor_entity_id, now=now)

    def release_perception_consumer(self, sensor_entity_id, consumer_id, *, now=None):
        """Detach one consumer; the last detach makes the durable restore snapshot actionable."""
        now = time.time() if now is None else float(now)
        resource = "sensor-config:" + self.identity_for_entity(sensor_entity_id)["logical_device_id"]
        with self.lock_resources([resource]):
            with self.store.lock, self.store.conn() as c:
                row = c.execute(
                    "SELECT * FROM perception_resource_leases WHERE resource_key=?", (resource,)
                ).fetchone()
                if not row:
                    return None
                row = dict(row)
                consumers = set(_safe_json(row.get("consumers_json"), []))
                consumers.discard(str(consumer_id))
                lease_until = float(row.get("lease_until") or 0.0) if consumers else 0.0
                c.execute(
                    """UPDATE perception_resource_leases SET consumers_json=?,lease_until=?,updated_ts=?
                       WHERE resource_key=?""",
                    (json.dumps(sorted(consumers)), lease_until, now, resource),
                )
        return self.perception_lease(sensor_entity_id, now=now)

    def perception_lease(self, sensor_entity_id, *, now=None):
        now = time.time() if now is None else float(now)
        resource = "sensor-config:" + self.identity_for_entity(sensor_entity_id)["logical_device_id"]
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM perception_resource_leases WHERE resource_key=?", (resource,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["consumers"] = _safe_json(out.pop("consumers_json", "[]"), [])
        out["config_snapshot"] = _safe_json(out.pop("config_snapshot_json", "{}"), {})
        out["active"] = bool(float(out.get("lease_until") or 0.0) > now and out["consumers"])
        out["restore_required"] = bool(out["config_snapshot"] and not out["active"])
        out["authoritative_owner"] = PERCEPTION_OWNER
        return out

    def perception_adapter_contract(self, sensor_entity_id):
        return {
            "resource_key": "sensor-config:" + self.identity_for_entity(sensor_entity_id)["logical_device_id"],
            "owner_service": PERCEPTION_OWNER,
            "durable_lease_authority": "DeviceAgentService.perception_resource_leases",
            "local_adapter_lease_role": "planning_token_only_not_resource_ownership",
            "requires_snapshot": True,
            "restore_required_on_last_release_or_expiry": True,
            "physical_io": False,
        }

    # ------------------------------------------------------------------
    # Dispatch plan semantics
    # ------------------------------------------------------------------
    def dispatch_plan(self, agent, state, value):
        domain, service, data = target_call(agent["target_entity"], agent["target_property"], value, state)
        semantics = "single_property"
        if domain == "light" and agent["target_property"] == "brightness_pct":
            semantics = "compound_power_brightness" if float(value) > 0.5 else "compound_power_off"
        return {"domain": domain, "service": service, "data": data, "semantics": semantics,
                "double_dispatch_required": False}

    def contract(self):
        return {
            "version": CONTRACT_VERSION,
            "identity": "explicit mapping > HA registry device_id > exact entity_id; never friendly-name guessing",
            "agent_identity_migration": "agent.id/history/generations unchanged; logical_device_id + device_property are additive",
            "property_mapping": "explicit property_name may define logical property; physical target_property is never rewritten",
            "shared_resources": ["device", "explicit group", "thermal/solar-shading zone", "sensor configuration"],
            "control_ownership": "durable pre-commit resource claim closes mode=control transition races and is reconciled on restart",
            "manual_priority": "hard guard independent of reward",
            "action_mask": "legal_value + autonomy description + shared manual hold + in-flight lease + cross-agent min dwell",
            "dispatch_reservation": "rechecks manual hold, Control owner, in-flight lease and cross-agent dwell atomically under all shared-resource locks",
            "perception_owner": PERCEPTION_OWNER,
            "perception_lease": "durable authority; active snapshot immutable; expired generation drops stale consumers; last release requires restore",
            "power_brightness": "one resource owner; brightness command is a single compound light.turn_on/off action",
            "process_model": ProcessModelBackendContract().export(),
            "executor_boundary": "arbiter never sends HA services; Executor remains sole dispatcher",
        }


def install_runtime(engine):
    """Compose diagnostics and registry-triggered identity migration before workers start."""
    service = getattr(getattr(engine, "executor", None), "device_agents", None)
    if service is None or getattr(engine, "_device_agent_runtime_installed", False):
        return engine

    original_runtime_for = engine.runtime_for
    original_entity_registry = engine.update_entity_registry
    original_device_registry = engine.update_device_registry

    def runtime_for(agent):
        payload = dict(original_runtime_for(agent) or {})
        desc = service.descriptor(agent)
        with engine.lock:
            state = engine.state_map.get(agent["target_entity"])
        payload["device_agent"] = desc
        payload["device_action_mask"] = service.legal_action_mask(
            agent, state, [agent.get("min_value"), agent.get("max_value")],
            runtime_by_agent=engine.runtime,
        ) if state is not None else {"actions": [], "fallback": "abstain", "reason": "target unavailable"}
        payload["device_agent_contract"] = service.contract()
        return payload

    def update_entity_registry(entries):
        result = original_entity_registry(entries)
        service.migrate_agent_identities()
        service.reconcile_control_resources()
        return result

    def update_device_registry(entries):
        result = original_device_registry(entries)
        service.migrate_agent_identities()
        service.reconcile_control_resources()
        return result

    engine.runtime_for = runtime_for
    engine.update_entity_registry = update_entity_registry
    engine.update_device_registry = update_device_registry
    service.migrate_agent_identities()
    engine.device_control_reconciliation = service.reconcile_control_resources()
    engine.device_agents = service
    engine._device_agent_runtime_installed = True
    return engine
