from urllib.error import HTTPError
from urllib.request import Request
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError
import json
from urllib.parse import quote
import re
import threading
from urllib.parse import urlencode
from urllib.request import urlopen
from settings import (HA_BASE_URL, HA_TOKEN, OPTIONS, now_ts)
from storage import STORE

class HAClient:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.token = token
        self.last_ok = None
        self.last_error = "Not connected yet"

    def request(self, method, path, payload=None, timeout=10):
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                # Recorder JSON inflates in Python; split oversized history requests
                # before decoding. Together with one worker this bounds peak RSS.
                limit = 8 * 1024 * 1024 if path.startswith('history/') else 32 * 1024 * 1024
                raw = resp.read(limit + 1)
                if len(raw) > limit:
                    raise OSError('HA response too large; split the request')
                self.last_ok = now_ts()
                self.last_error = None
                return json.loads(raw.decode("utf-8")) if raw else None
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            self.last_error = str(exc)
            raise

    def states(self):
        return self.request("GET", "states")

    def service(self, domain, service, data):
        return self.request("POST", f"services/{domain}/{service}", data)

    def history(self, entity_ids, start_dt, end_dt, minimal=True, no_attributes=True, significant=True, timeout=20):
        params = {"filter_entity_id": ",".join(entity_ids), "end_time": end_dt}
        if minimal:
            params["minimal_response"] = ""
        if no_attributes:
            params["no_attributes"] = ""
        if significant:
            params["significant_changes_only"] = ""
        query = urlencode(params, doseq=True)
        # HA treats the presence of flag params as true; urlencode gives = which is accepted.
        return self.request("GET", f"history/period/{quote(start_dt, safe='')}?{query}", timeout=timeout)

    def automation_config(self, automation_id, timeout=8):
        return self.request("GET", f"config/automation/config/{quote(str(automation_id), safe='')}", timeout=timeout)


HA = HAClient(HA_BASE_URL, HA_TOKEN)


ENTITY_ID_RE = re.compile(r"\b[a-z_][a-z0-9_]*\.[a-zA-Z0-9_]+\b")
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{20,64}$", re.I)

def _extract_entities(obj):
    out = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("entity_id", "entity_ids"):
                vals = value if isinstance(value, list) else [value]
                for item in vals:
                    if isinstance(item, str):
                        out.update(ENTITY_ID_RE.findall(item))
            # Service/action names such as light.turn_on match the entity-id shape but
            # are not entities. Their target/data blocks are still recursively parsed.
            if key not in ("action", "service"):
                out.update(_extract_entities(value))
    elif isinstance(obj, list):
        for value in obj:
            out.update(_extract_entities(value))
    elif isinstance(obj, str):
        out.update(ENTITY_ID_RE.findall(obj))
    return out

def _extract_device_ids(obj):
    out = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("device_id", "device_ids"):
                vals = value if isinstance(value, list) else [value]
                for item in vals:
                    if isinstance(item, str) and DEVICE_ID_RE.match(item):
                        out.add(item)
            out.update(_extract_device_ids(value))
    elif isinstance(obj, list):
        for value in obj:
            out.update(_extract_device_ids(value))
    return out


def _duration_seconds(value):
    """Normalize HA duration syntax without assigning policy meaning to it."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if ":" in text:
            try:
                parts = [float(part) for part in text.split(":")]
                if len(parts) == 3:
                    return max(0.0, parts[0] * 3600.0 + parts[1] * 60.0 + parts[2])
            except (TypeError, ValueError):
                return None
        try:
            return max(0.0, float(text))
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    try:
        return max(
            0.0,
            float(value.get("hours") or 0) * 3600.0
            + float(value.get("minutes") or 0) * 60.0
            + float(value.get("seconds") or 0),
        )
    except (TypeError, ValueError):
        return None


def automation_baseline_rules(obj, source="trigger"):
    """Extract lightweight threshold/hysteresis semantics from readable HA config.

    These rows are structural baseline metadata only. They never become rewards or hard
    policy constraints.
    """
    rules = []
    if isinstance(obj, list):
        for item in obj:
            rules.extend(automation_baseline_rules(item, source=source))
        return rules
    if not isinstance(obj, dict):
        return rules

    kind = str(obj.get("trigger", obj.get("platform", obj.get("condition", ""))) or "").lower()
    if kind == "numeric_state":
        values = obj.get("entity_id", obj.get("entity_ids", []))
        values = values if isinstance(values, list) else [values]
        for entity_id in values:
            if not isinstance(entity_id, str) or not ENTITY_ID_RE.fullmatch(entity_id):
                continue
            above = obj.get("above")
            below = obj.get("below")
            try:
                above = None if above is None else float(above)
            except (TypeError, ValueError):
                above = None
            try:
                below = None if below is None else float(below)
            except (TypeError, ValueError):
                below = None
            rules.append({
                "source": str(source),
                "kind": "numeric_state",
                "entity_id": entity_id,
                "above": above,
                "below": below,
                "for_seconds": _duration_seconds(obj.get("for")),
            })

    for key, value in obj.items():
        if key in ("entity_id", "entity_ids", "above", "below", "for"):
            continue
        if isinstance(value, (dict, list)):
            rules.extend(automation_baseline_rules(value, source=source))
    return rules


def automation_action_services(actions):
    """Return literal action/service names for baseline diagnostics."""
    found = set()
    if isinstance(actions, list):
        for item in actions:
            found.update(automation_action_services(item))
    elif isinstance(actions, dict):
        command = actions.get("action", actions.get("service"))
        if isinstance(command, str) and re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", command):
            found.add(command)
        device_domain = actions.get("domain")
        device_type = actions.get("type")
        if (
            isinstance(device_domain, str)
            and isinstance(device_type, str)
            and re.fullmatch(r"[a-z_]+", device_domain)
            and re.fullmatch(r"[a-z0-9_]+", device_type)
            and device_type in {"turn_on", "turn_off", "toggle", "open", "close"}
        ):
            found.add(f"{device_domain}.{device_type}")
        for value in actions.values():
            if isinstance(value, (dict, list)):
                found.update(automation_action_services(value))
    return found


def automation_action_targets(actions, registry):
    """Only literal command destinations, never condition/template references."""
    found = set()
    if isinstance(actions, list):
        for item in actions:
            found.update(automation_action_targets(item, registry))
    elif isinstance(actions, dict):
        command = actions.get("action", actions.get("service"))
        device_action = actions.get("device_id") and actions.get("domain") and actions.get("type")
        if command or device_action:
            destinations = [actions.get("target") or {}, actions.get("data") or {}]
            if device_action:
                destinations.append(actions)
            for destination in destinations:
                if not isinstance(destination, dict):
                    continue
                for key in ("entity_id", "device_id", "area_id"):
                    values = destination.get(key, [])
                    for value in values if isinstance(values, list) else [values]:
                        if not isinstance(value, str) or "{{" in value or "{%" in value:
                            continue
                        if key == "entity_id":
                            found.update(x.strip() for x in value.split(',') if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", x.strip()))
                        else:
                            found.update(eid for eid, reg in registry.items() if reg.get(key) == value)
        for key in ("sequence", "default", "then", "else", "parallel"):
            found.update(automation_action_targets(actions.get(key), registry))
        for choice in actions.get("choose", []) or []:
            found.update(automation_action_targets(choice.get("sequence"), registry))
        repeat = actions.get("repeat")
        if isinstance(repeat, dict):
            found.update(automation_action_targets(repeat.get("sequence"), registry))
    return found


class AutomationKnowledge:
    """Best-effort read-only analysis of existing HA automations.

    Automations never become labels or rewards. They only provide a structural prior:
    entities used in triggers/conditions of an automation that acts on a target get
    duplicated as hint features so offline RL can converge with fewer samples.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.by_target = {}
        self.automations = []
        self.last_scan = None
        self.error = None
        self.config_failures = []
        self.cached_infos = {}
        try:
            saved = json.loads(STORE.meta_get('automation_target_cache_v1', '[]'))
            if isinstance(saved, list):
                for info in saved:
                    if (isinstance(info, dict) and isinstance(info.get('entity_id'), str)
                            and isinstance(info.get('target_entities'), list)
                            and isinstance(info.get('context_entities'), list)):
                        self.cached_infos[info['entity_id']] = info
        except (ValueError, TypeError):
            pass
        for info in self.cached_infos.values():
            cached = {**info, 'config_status': 'cached'}
            self.automations.append(cached)
            for target in cached['target_entities']:
                self.by_target.setdefault(target, []).append(cached)

    def status(self):
        with self.lock:
            return {
                "automation_count": len(self.automations),
                "target_count": len(self.by_target),
                "last_scan": self.last_scan,
                "error": self.error,
                "config_failures": list(self.config_failures),
            }

    def hints_for_target(self, entity_id):
        with self.lock:
            infos = list(self.by_target.get(entity_id, []))
        entities = set()
        for info in infos:
            entities.update(info.get("context_entities") or [])
        return entities, infos

    def scan(self, state_map, entity_registry=None, force=False):
        with self.scan_lock:
            return self._scan(state_map, entity_registry, force)

    def _scan(self, state_map, entity_registry=None, force=False):
        if not force and not OPTIONS.get("automation_scan_enabled", True):
            return 0
        entity_registry = entity_registry or {}
        device_entities = {}
        for eid, reg in entity_registry.items():
            did = reg.get("device_id") if isinstance(reg, dict) else None
            if did:
                device_entities.setdefault(did, set()).add(eid)
        autos = [s for eid, s in state_map.items() if eid.startswith("automation.")]
        by_target = {}
        parsed = []
        failures = {}
        configs = {}
        ids = [(st, (st.get("attributes") or {}).get("id")) for st in autos]
        ids_to_fetch = [(st, aid) for st, aid in ids if aid]
        def fetch_one(pair):
            st, aid = pair
            try:
                return st.get("entity_id"), HA.automation_config(aid, timeout=2), None
            except Exception as exc:
                return st.get("entity_id"), None, exc
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(ids_to_fetch)))) as pool:
            for eid, cfg, err in pool.map(fetch_one, ids_to_fetch):
                if isinstance(cfg, dict):
                    configs[eid] = cfg
                else:
                    failures[eid] = str(err) if err is not None else 'Invalid configuration response'
        retained = {}
        for st in autos:
            attrs = st.get("attributes") or {}
            automation_id = attrs.get("id")
            eid = st.get('entity_id')
            previous = self.cached_infos.get(eid, {})
            # Reuse only an identity-matching successful parse; never a previous error.
            if automation_id is not None and previous.get('automation_id') != automation_id:
                previous = {}
            config = configs.get(eid)
            if config is None:
                failures.setdefault(eid, 'Automation has no readable configuration id')
            config = config or {}
            actions = config.get("actions", config.get("action", []))
            triggers = config.get("triggers", config.get("trigger", []))
            conditions = config.get("conditions", config.get("condition", []))
            action_entities = automation_action_targets(actions, entity_registry)
            context_entities = _extract_entities(triggers) | _extract_entities(conditions)
            context_devices = _extract_device_ids(triggers) | _extract_device_ids(conditions)
            for did in context_devices:
                context_entities.update(device_entities.get(did, ()))
            if eid in failures and previous:
                action_entities = set(previous['target_entities'])
                context_entities = set(previous['context_entities'])
            # Do not let the controlled target itself become a prior input merely because
            # it appears in the action block. The ordinary state remains in the context.
            baseline_rules = (
                list(previous.get("baseline_rules") or [])
                if eid in failures and previous
                else (
                    automation_baseline_rules(triggers, source="trigger")
                    + automation_baseline_rules(conditions, source="condition")
                )
            )
            action_services = (
                list(previous.get("action_services") or [])
                if eid in failures and previous
                else sorted(automation_action_services(actions))
            )
            info = {
                "entity_id": st.get("entity_id"),
                "name": attrs.get("friendly_name") or st.get("entity_id"),
                "automation_id": automation_id,
                "enabled": str(st.get("state")).lower() == "on",
                "last_triggered": attrs.get("last_triggered"),
                "target_entities": sorted(action_entities),
                "context_entities": sorted(context_entities - action_entities),
                "baseline_rules": baseline_rules,
                "action_services": action_services,
                "baseline_contract": "structural_prior_not_ground_truth",
                "config_status": 'cached' if eid in failures and previous else 'unavailable' if eid in failures else 'fresh',
                "config_error": failures.get(eid),
            }
            if eid not in failures:
                retained[eid] = dict(info)
            elif previous:
                retained[eid] = previous
            parsed.append(info)
            for target in action_entities:
                by_target.setdefault(target, []).append(info)
        with self.lock:
            self.by_target = by_target
            self.automations = parsed
            self.last_scan = now_ts()
            self.cached_infos = retained
            self.config_failures = [{'entity_id': eid, 'reason': reason} for eid, reason in failures.items()]
            self.error = None if not failures else f"{len(failures)} automation config(s) unavailable; using readable configurations and last known target mappings"
        STORE.meta_set('automation_target_cache_v1', json.dumps(list(retained.values()), separators=(',', ':')))
        STORE.event(None, "info", "automation_scan", f"Scanned {len(parsed)} Home Assistant automation(s) as RL feature priors", {"automations": len(parsed), "targets": len(by_target), "config_failures": len(failures)})
        return len(parsed)


AUTOMATION_KNOWLEDGE = AutomationKnowledge()
