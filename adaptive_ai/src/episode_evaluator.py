"""Episode-level outcome evaluation for fast light power policies.

The evaluator deliberately separates four things that older HomeMind fast-light paths
could conflate: occupancy evidence, an independent "light is needed" label, replay of
the previous automation, and the physical result of an action. Shadow/Candidate and
Tournament policies are evaluated on the *same* episode, but their comfort-related
numbers are explicitly counterfactual proxies because those policies did not operate the
device.

Contract v1 is additive. It does not reinterpret historical feature vectors and it does
not turn missing evidence into zero/success. A finalized episode is immutable and may be
recorded repeatedly only when the payload is identical (idempotent restart/retry).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import time
from typing import Any, Iterable, Optional

CONTRACT_VERSION = 1
DOMAIN_LIGHT_POWER = "light_power"
RETRIGGER_SECONDS = 15.0

UNKNOWN = "unknown"
OBSERVED = "observed"
PHYSICAL = "physical"
COUNTERFACTUAL_PROXY = "counterfactual_proxy"
AUTOMATION_REPLAY_PROXY = "automation_replay_proxy"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _finite(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return None
        return float(value) >= 0.5
    text = str(value).strip().lower()
    if text in {"on", "home", "open", "occupied", "detected", "active", "true", "1"}:
        return True
    if text in {"off", "not_home", "closed", "unoccupied", "clear", "inactive", "false", "0"}:
        return False
    return None


def _label_summary(values: Iterable[Optional[bool]]) -> str:
    known = {value for value in values if value is not None}
    if not known:
        return UNKNOWN
    if len(known) > 1:
        return "mixed"
    return "true" if True in known else "false"


def _latest(events: list[dict], ts: float, key: str) -> Optional[bool]:
    value = None
    for row in events:
        if row["ts"] > ts:
            break
        if row.get(key) is not None:
            value = _bool(row.get(key))
    return value


def _latest_decision(events: list[dict], ts: float) -> Optional[bool]:
    value = None
    for row in events:
        if row["ts"] > ts:
            break
        if row.get("power") is not None:
            value = _bool(row.get("power"))
    return value


def _transition_times(events: list[dict], key: str) -> list[tuple[float, bool]]:
    out: list[tuple[float, bool]] = []
    previous = None
    have_previous = False
    for row in events:
        value = _bool(row.get(key))
        if value is None:
            continue
        if not have_previous:
            previous = value
            have_previous = True
            continue
        if value != previous:
            out.append((float(row["ts"]), value))
            previous = value
    return out


def _rapid_transition_count(transitions: list[tuple[float, bool]], window: float) -> int:
    if len(transitions) < 2:
        return 0
    return sum(1 for (prev_ts, _), (ts, _) in zip(transitions, transitions[1:])
               if 0.0 <= ts - prev_ts <= window)


def _normalise_observations(rows: Iterable[dict], start: float, end: float) -> list[dict]:
    out: list[dict] = []
    for raw in rows or ():
        ts = _finite(raw.get("ts"))
        if ts is None or ts < start or ts > end:
            continue
        out.append({
            "ts": ts,
            "presence": _bool(raw.get("presence")),
            "light_need": _bool(raw.get("light_need")),
            "power": _bool(raw.get("power")),
            "observable": bool(raw.get("observable", True)),
        })
    out.sort(key=lambda row: row["ts"])
    return out


def _normalise_decisions(rows: Iterable[dict], start: float, end: float) -> list[dict]:
    out: list[dict] = []
    for raw in rows or ():
        ts = _finite(raw.get("ts"))
        if ts is None or ts < start or ts > end:
            continue
        out.append({
            "ts": ts,
            "power": _bool(raw.get("power")),
            "anticipatory": bool(raw.get("anticipatory", False)),
            "decision_id": raw.get("decision_id"),
        })
    out.sort(key=lambda row: row["ts"])
    return out


def _normalise_automation(rows: Iterable[dict], start: float, end: float) -> list[dict]:
    return _normalise_decisions(rows or (), start, end)


def _metric(value, source, *, complete=True, known_seconds=0.0, unknown_seconds=0.0):
    return {
        "value": value,
        "source": source if value is not None else UNKNOWN,
        "complete": bool(complete and value is not None),
        "known_seconds": float(known_seconds),
        "unknown_seconds": float(unknown_seconds),
    }


@dataclass(frozen=True)
class PolicyEvaluation:
    policy_key: str
    role: str
    executed: bool
    counterfactual: bool
    metrics: dict
    evidence: dict


class EpisodeEvaluator:
    """Evaluate and persist immutable light-power episodes.

    ``store`` is optional so deterministic unit fixtures can use the evaluator without
    SQLite. Production passes the normal HomeMind Store and receives additive tables.
    """

    def __init__(self, store=None, clock=time.time):
        self.store = store
        self.clock = clock
        if store is not None:
            self._migrate()

    def _migrate(self):
        with self.store.lock, self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS episode_evaluator_episodes (
                    episode_id TEXT PRIMARY KEY,
                    contract_version INTEGER NOT NULL,
                    domain TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    start_ts REAL NOT NULL,
                    end_ts REAL NOT NULL,
                    context_json TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    observability_json TEXT NOT NULL,
                    automation_replay_json TEXT,
                    physical_outcome_json TEXT,
                    end_reason TEXT,
                    fingerprint TEXT NOT NULL,
                    created_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_episode_evaluator_agent_time
                    ON episode_evaluator_episodes(agent_id,start_ts,end_ts);

                CREATE TABLE IF NOT EXISTS episode_evaluator_policy_results (
                    episode_id TEXT NOT NULL,
                    policy_key TEXT NOT NULL,
                    role TEXT NOT NULL,
                    executed INTEGER NOT NULL,
                    counterfactual INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    PRIMARY KEY(episode_id,policy_key),
                    FOREIGN KEY(episode_id) REFERENCES episode_evaluator_episodes(episode_id)
                );
                CREATE INDEX IF NOT EXISTS idx_episode_evaluator_policy
                    ON episode_evaluator_policy_results(policy_key,episode_id);
                """
            )

    @staticmethod
    def _timeline(start: float, end: float, observations: list[dict], decisions: list[dict],
                  automation: list[dict]) -> list[float]:
        points = {start, end}
        points.update(row["ts"] for row in observations)
        points.update(row["ts"] for row in decisions)
        points.update(row["ts"] for row in automation)
        return sorted(ts for ts in points if start <= ts <= end)

    @staticmethod
    def _physical_power(observations: list[dict], ts: float) -> Optional[bool]:
        value = None
        for row in observations:
            if row["ts"] > ts:
                break
            if not row.get("observable", True):
                return None
            if row.get("power") is not None:
                value = row["power"]
        return value

    def _evaluate_policy(self, *, policy: dict, start: float, end: float,
                         observations: list[dict], automation: list[dict],
                         manual_corrections: list[dict]) -> PolicyEvaluation:
        key = str(policy.get("policy_key") or policy.get("id") or policy.get("role") or "policy")
        role = str(policy.get("role") or "policy")
        executed = bool(policy.get("executed", False))
        decisions = _normalise_decisions(policy.get("decisions") or (), start, end)
        initial_power = _bool(policy.get("initial_power"))
        automation_initial = _bool(policy.get("automation_initial"))
        timeline = self._timeline(start, end, observations, decisions, automation)

        off_needed = 0.0
        unnecessary_on = 0.0
        need_known = 0.0
        need_unknown = 0.0
        replay_disagreement = 0.0
        replay_known = 0.0
        physical_known = 0.0
        physical_unknown = 0.0

        previous_need = None
        first_need = None

        def policy_power(ts):
            if executed:
                return self._physical_power(observations, ts)
            value = _latest_decision(decisions, ts)
            return initial_power if value is None else value

        for left, right in zip(timeline, timeline[1:]):
            if right <= left:
                continue
            duration = right - left
            sample_ts = left + min(1e-6, duration / 2.0)
            need = _latest(observations, sample_ts, "light_need")
            power = policy_power(sample_ts)
            if executed:
                if power is None:
                    physical_unknown += duration
                else:
                    physical_known += duration
            if need is None or power is None:
                need_unknown += duration
            else:
                need_known += duration
                if need and not power:
                    off_needed += duration
                if not need and power:
                    unnecessary_on += duration
            if need is True and previous_need is not True and first_need is None:
                first_need = left
            if need is not None:
                previous_need = need

            auto = _latest_decision(automation, sample_ts)
            if auto is None:
                auto = automation_initial
            if auto is not None and power is not None:
                replay_known += duration
                if auto != power:
                    replay_disagreement += duration

        delay = None
        if first_need is not None:
            probe_points = sorted(set(timeline + [first_need]))
            on_at = None
            for ts in probe_points:
                if ts < first_need:
                    continue
                if policy_power(ts) is True:
                    on_at = ts
                    break
            delay = max(0.0, (on_at if on_at is not None else end) - first_need)

        anticipatory = [row for row in decisions if row.get("anticipatory") and row.get("power") is True]
        false_arrival = None
        confirmed_arrival = None
        if anticipatory:
            pred_ts = min(row["ts"] for row in anticipatory)
            relevant = [row for row in observations if row["ts"] >= pred_ts]
            known_presence = [row["presence"] for row in relevant
                              if row.get("observable", True) and row.get("presence") is not None]
            has_unobservable = any(not row.get("observable", True) or row.get("presence") is None for row in relevant)
            if known_presence and not has_unobservable:
                confirmed_arrival = any(known_presence)
                false_arrival = not confirmed_arrival

        if executed:
            transitions = _transition_times(observations, "power")
        else:
            transitions = _transition_times(decisions, "power")
        retriggers = 0
        last_off = None
        for ts, value in transitions:
            if value is False:
                last_off = ts
            elif value is True and last_off is not None and 0.0 <= ts - last_off <= RETRIGGER_SECONDS:
                retriggers += 1
                last_off = None
        chatter = _rapid_transition_count(transitions, RETRIGGER_SECONDS)

        corrections = 0
        for row in manual_corrections:
            target = row.get("policy_key")
            if target is None:
                if executed:
                    corrections += 1
            elif str(target) == key:
                corrections += 1

        source = PHYSICAL if executed else COUNTERFACTUAL_PROXY
        metrics = {
            "needed_on_delay_seconds": delay,
            "off_while_needed_seconds": off_needed if need_known > 0 else None,
            "unnecessary_on_seconds": unnecessary_on if need_known > 0 else None,
            "false_arrival_prediction": None if false_arrival is None else int(false_arrival),
            "arrival_prediction_confirmed": None if confirmed_arrival is None else int(confirmed_arrival),
            "retrigger_count": int(retriggers),
            "chatter_count": int(chatter),
            "manual_correction_count": int(corrections),
            "automation_disagreement_seconds": replay_disagreement if replay_known > 0 else None,
            "physical_power_observed_seconds": physical_known if executed else None,
        }
        evidence = {
            "needed_on_delay_seconds": _metric(delay, source, complete=need_unknown <= 1e-9,
                                                 known_seconds=need_known, unknown_seconds=need_unknown),
            "off_while_needed_seconds": _metric(metrics["off_while_needed_seconds"], source,
                                                   complete=need_unknown <= 1e-9,
                                                   known_seconds=need_known, unknown_seconds=need_unknown),
            "unnecessary_on_seconds": _metric(metrics["unnecessary_on_seconds"], source,
                                                 complete=need_unknown <= 1e-9,
                                                 known_seconds=need_known, unknown_seconds=need_unknown),
            "false_arrival_prediction": _metric(metrics["false_arrival_prediction"],
                                                   OBSERVED if false_arrival is not None else UNKNOWN),
            "retrigger_count": _metric(metrics["retrigger_count"], source),
            "chatter_count": _metric(metrics["chatter_count"], source),
            "manual_correction_count": _metric(metrics["manual_correction_count"], OBSERVED),
            "automation_disagreement_seconds": _metric(
                metrics["automation_disagreement_seconds"], AUTOMATION_REPLAY_PROXY,
                complete=replay_known >= max(0.0, end - start) - 1e-9,
                known_seconds=replay_known,
                unknown_seconds=max(0.0, end - start - replay_known),
            ),
            "physical_power_observed_seconds": _metric(
                metrics["physical_power_observed_seconds"], PHYSICAL,
                complete=physical_unknown <= 1e-9,
                known_seconds=physical_known, unknown_seconds=physical_unknown,
            ),
        }
        comfort_values = (
            metrics["needed_on_delay_seconds"],
            metrics["off_while_needed_seconds"],
            metrics["unnecessary_on_seconds"],
        )
        meaningful_comfort = any(value is not None for value in comfort_values)
        meaningful_prediction = metrics["false_arrival_prediction"] is not None
        meaningful_proxy = metrics["automation_disagreement_seconds"] is not None
        harmful = bool(
            (metrics["off_while_needed_seconds"] or 0.0) > 1e-9
            or (metrics["unnecessary_on_seconds"] or 0.0) > 1e-9
            or (metrics["false_arrival_prediction"] or 0) > 0
            or metrics["manual_correction_count"] > 0
        )
        proxy_harmful = bool((metrics["automation_disagreement_seconds"] or 0.0) > 1e-9)
        metrics.update({
            "meaningful": bool(meaningful_comfort or meaningful_prediction or corrections),
            "meaningful_proxy": bool(meaningful_proxy),
            "harmful": harmful,
            "proxy_harmful": proxy_harmful,
            "switch_count": len(transitions),
        })
        return PolicyEvaluation(
            policy_key=key,
            role=role,
            executed=executed,
            counterfactual=not executed,
            metrics=metrics,
            evidence=evidence,
        )

    def evaluate_episode(self, *, episode_id: str, agent_id: str, start_ts: float,
                         end_ts: float, observations: Iterable[dict], policies: Iterable[dict],
                         context: Optional[dict] = None, automation_replay: Optional[Iterable[dict]] = None,
                         automation_initial: Any = None, manual_corrections: Optional[Iterable[dict]] = None,
                         end_reason: Optional[str] = None, persist: bool = True) -> dict:
        start = _finite(start_ts)
        end = _finite(end_ts)
        if start is None or end is None or end < start:
            raise ValueError("Episode start/end must be finite and end >= start")
        episode_id = str(episode_id)
        agent_id = str(agent_id)
        observations = _normalise_observations(observations or (), start, end)
        automation = _normalise_automation(automation_replay or (), start, end)
        corrections = [dict(row) for row in (manual_corrections or ())]
        normalised_policies = []
        for raw in policies or ():
            row = dict(raw)
            row.setdefault("automation_initial", automation_initial)
            normalised_policies.append(row)
        if not normalised_policies:
            raise ValueError("Episode requires at least one policy result")

        policy_results = [
            self._evaluate_policy(
                policy=row, start=start, end=end, observations=observations,
                automation=automation, manual_corrections=corrections,
            )
            for row in normalised_policies
        ]
        labels = {
            "presence": _label_summary(row.get("presence") for row in observations),
            "light_need": _label_summary(row.get("light_need") for row in observations),
        }
        observable_rows = [row for row in observations if row.get("observable", True)]
        observability = {
            "observations": len(observations),
            "observable_observations": len(observable_rows),
            "presence_known": sum(row.get("presence") is not None and row.get("observable", True) for row in observations),
            "light_need_known": sum(row.get("light_need") is not None and row.get("observable", True) for row in observations),
            "physical_power_known": sum(row.get("power") is not None and row.get("observable", True) for row in observations),
        }
        physical_outcome = {
            "observed": bool(observability["physical_power_known"]),
            "executed_policy_keys": [row.policy_key for row in policy_results if row.executed],
        }
        payload = {
            "contract_version": CONTRACT_VERSION,
            "domain": DOMAIN_LIGHT_POWER,
            "episode_id": episode_id,
            "agent_id": agent_id,
            "start_ts": start,
            "end_ts": end,
            "context": dict(context or {}),
            "labels": labels,
            "observability": observability,
            "automation_replay": automation,
            "physical_outcome": physical_outcome,
            "end_reason": end_reason,
            "policies": [
                {
                    "policy_key": row.policy_key,
                    "role": row.role,
                    "executed": row.executed,
                    "counterfactual": row.counterfactual,
                    "metrics": row.metrics,
                    "evidence": row.evidence,
                }
                for row in policy_results
            ],
        }
        if persist and self.store is not None:
            self._persist(payload)
        return payload

    def _persist(self, payload: dict):
        fingerprint = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        with self.store.lock, self.store.conn() as c:
            previous = c.execute(
                "SELECT fingerprint FROM episode_evaluator_episodes WHERE episode_id=?",
                (payload["episode_id"],),
            ).fetchone()
            if previous:
                if str(previous[0]) != fingerprint:
                    raise ValueError("Episode id already finalized with a different payload")
                return False
            c.execute(
                """INSERT INTO episode_evaluator_episodes
                   (episode_id,contract_version,domain,agent_id,start_ts,end_ts,context_json,
                    labels_json,observability_json,automation_replay_json,physical_outcome_json,
                    end_reason,fingerprint,created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    payload["episode_id"], CONTRACT_VERSION, DOMAIN_LIGHT_POWER, payload["agent_id"],
                    payload["start_ts"], payload["end_ts"], _json(payload["context"]),
                    _json(payload["labels"]), _json(payload["observability"]),
                    _json(payload["automation_replay"]), _json(payload["physical_outcome"]),
                    payload.get("end_reason"), fingerprint, float(self.clock()),
                ),
            )
            for row in payload["policies"]:
                c.execute(
                    """INSERT INTO episode_evaluator_policy_results
                       (episode_id,policy_key,role,executed,counterfactual,metrics_json,evidence_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        payload["episode_id"], row["policy_key"], row["role"], int(row["executed"]),
                        int(row["counterfactual"]), _json(row["metrics"]), _json(row["evidence"]),
                    ),
                )
        return True

    def record_automation_proxy(self, *, episode_id: str, agent_id: str, start_ts: float,
                                end_ts: float, baseline_power: Any,
                                policies: Iterable[dict], presence: Any = None,
                                light_need: Any = None, physical_power: Any = None,
                                observable: bool = True, end_reason: str = "automation proxy") -> dict:
        """Persist one compatibility episode without claiming baseline == light need."""
        start = float(start_ts)
        end = float(end_ts)
        observations = [
            {"ts": start, "presence": presence, "light_need": light_need,
             "power": physical_power, "observable": observable},
            {"ts": end, "presence": presence, "light_need": light_need,
             "power": physical_power, "observable": observable},
        ]
        automation = [
            {"ts": start, "power": baseline_power},
            {"ts": end, "power": baseline_power},
        ]
        return self.evaluate_episode(
            episode_id=episode_id, agent_id=agent_id, start_ts=start, end_ts=end,
            observations=observations, policies=policies, automation_replay=automation,
            automation_initial=baseline_power, end_reason=end_reason,
        )

    def _policy_rows(self, agent_id: str, policy_key: str) -> dict[str, dict]:
        if self.store is None:
            return {}
        with self.store.conn() as c:
            rows = c.execute(
                """SELECT e.episode_id,e.start_ts,e.end_ts,e.labels_json,e.observability_json,
                          r.role,r.executed,r.counterfactual,r.metrics_json,r.evidence_json
                   FROM episode_evaluator_episodes e
                   JOIN episode_evaluator_policy_results r ON r.episode_id=e.episode_id
                   WHERE e.agent_id=? AND r.policy_key=? ORDER BY e.start_ts,e.episode_id""",
                (str(agent_id), str(policy_key)),
            ).fetchall()
        out = {}
        for raw in rows:
            row = dict(raw)
            row["labels"] = json.loads(row.pop("labels_json") or "{}")
            row["observability"] = json.loads(row.pop("observability_json") or "{}")
            row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
            row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
            out[row["episode_id"]] = row
        return out

    def compare_policies(self, agent_id: str, parent_policy_key: str,
                         candidate_policy_key: str) -> dict:
        """Compare two policies only on the exact intersection of finalized episodes."""
        parent = self._policy_rows(agent_id, parent_policy_key)
        candidate = self._policy_rows(agent_id, candidate_policy_key)
        episode_ids = sorted(set(parent) & set(candidate), key=lambda eid: (parent[eid]["start_ts"], eid))

        def aggregate(rows: dict[str, dict]):
            meaningful = harmful = proxy_meaningful = proxy_harmful = 0
            executed = counterfactual = 0
            per_action = {"0.0": 0, "1.0": 0}
            sums = {
                "needed_on_delay_seconds": 0.0,
                "off_while_needed_seconds": 0.0,
                "unnecessary_on_seconds": 0.0,
                "false_arrival_prediction": 0.0,
                "retrigger_count": 0.0,
                "chatter_count": 0.0,
                "manual_correction_count": 0.0,
                "automation_disagreement_seconds": 0.0,
            }
            counts = {name: 0 for name in sums}
            for eid in episode_ids:
                row = rows[eid]
                metrics = row["metrics"]
                meaningful += int(bool(metrics.get("meaningful")))
                harmful += int(bool(metrics.get("meaningful")) and bool(metrics.get("harmful")))
                proxy_meaningful += int(bool(metrics.get("meaningful_proxy")))
                proxy_harmful += int(bool(metrics.get("meaningful_proxy")) and bool(metrics.get("proxy_harmful")))
                executed += int(bool(row.get("executed")))
                counterfactual += int(bool(row.get("counterfactual")))
                label = (row.get("labels") or {}).get("light_need")
                if label == "true":
                    per_action["1.0"] += 1
                elif label == "false":
                    per_action["0.0"] += 1
                for name in sums:
                    value = metrics.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                        sums[name] += float(value)
                        counts[name] += 1
            means = {name: (sums[name] / counts[name] if counts[name] else None) for name in sums}
            return {
                "meaningful_episodes": meaningful,
                "harmful_episodes": harmful,
                "episode_success_rate": (meaningful - harmful) / meaningful if meaningful else None,
                "proxy_meaningful_episodes": proxy_meaningful,
                "proxy_harmful_episodes": proxy_harmful,
                "proxy_success_rate": ((proxy_meaningful - proxy_harmful) / proxy_meaningful
                                       if proxy_meaningful else None),
                "executed_episodes": executed,
                "counterfactual_episodes": counterfactual,
                "per_action_episodes": per_action,
                "metric_sums": sums,
                "metric_means": means,
            }

        p = aggregate(parent)
        c = aggregate(candidate)
        independently_observed = min(p["meaningful_episodes"], c["meaningful_episodes"])
        proxy_only = independently_observed == 0 and min(p["proxy_meaningful_episodes"], c["proxy_meaningful_episodes"]) > 0
        return {
            "contract_version": CONTRACT_VERSION,
            "domain": DOMAIN_LIGHT_POWER,
            "agent_id": str(agent_id),
            "parent_policy_key": str(parent_policy_key),
            "candidate_policy_key": str(candidate_policy_key),
            "episode_ids": episode_ids,
            "matched_episodes": len(episode_ids),
            "independently_observed_episodes": independently_observed,
            "evidence_mode": "automation_replay_proxy" if proxy_only else "independent_labels" if independently_observed else "insufficient",
            "parent": p,
            "candidate": c,
        }
