"""Bounded read-only diagnostics for historical agent training balance.

The audit observes the existing replay pipeline. It must never change sample selection,
reward values, policy updates, validation semantics, or physical authority.
"""
from __future__ import annotations

import math


def _finite(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _ratio(numerator, denominator):
    numerator = _finite(numerator)
    denominator = _finite(denominator)
    return {
        "value": (numerator / denominator) if denominator > 0.0 else None,
        "status": "ok" if denominator > 0.0 else "zero_denominator",
        "numerator": numerator,
        "denominator": denominator,
    }


def _ordered_action_ratio(values, action_order):
    rows = [(str(key), _finite(values.get(str(key), 0.0))) for key in action_order]
    if not rows:
        return {
            "value": None, "status": "no_actions",
            "numerator": 0.0, "denominator": 0.0,
        }
    if len(rows) == 1:
        key, value = rows[0]
        out = _ratio(value, value)
        out.update({"numerator_action": key, "denominator_action": key})
        return out
    if len(rows) == 2:
        (den_key, den), (num_key, num) = rows
    else:
        num_key, num = max(rows, key=lambda item: item[1])
        den_key, den = min(rows, key=lambda item: item[1])
    out = _ratio(num, den)
    out.update({"numerator_action": num_key, "denominator_action": den_key})
    return out


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = max(0.0, min(1.0, float(fraction))) * (len(sorted_values) - 1)
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return float(sorted_values[lo])
    weight = position - lo
    return float(sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight)


def _mass_bucket():
    return {
        "raw_sample_mass": 0.0,
        "time_decayed_sample_mass": 0.0,
        "positive_reward_mass": 0.0,
        "negative_reward_mass": 0.0,
        "absolute_reward_mass": 0.0,
    }


def _sample_bucket():
    return {"total": 0, "train": 0, "validation": 0, "deferred_train": 0}


class TrainingBalanceAudit:
    """Collect a bounded summary while the normal historical replay already runs."""

    CONTRACT = "training_balance_audit_v1"

    def __init__(self, agent, policy, *, dwell_sample_cap=4096, prior_state=None):
        self.agent_id = str(agent.get("id") or "")
        self.actions = [float(value) for value in getattr(policy, "actions", ())]
        self.action_keys = [str(value) for value in self.actions]
        self.dwell_sample_cap = max(32, int(dwell_sample_cap))
        self.dwells = {
            key: {
                "count": 0,
                "total_seconds": 0.0,
                "durations": [],
                "quantiles_approximate": False,
            }
            for key in self.action_keys
        }
        self.samples = {
            source: _sample_bucket()
            for source in ("onset", "persistence", "upstream")
        }
        self.excluded = {"own_command": 0, "validation_boundary": 0}
        self.mass = _mass_bucket()
        self.mass_by_source = {
            source: _mass_bucket()
            for source in ("onset", "persistence", "upstream")
        }
        self.mass_by_provenance = {}
        self.mass_by_action = {key: _mass_bucket() for key in self.action_keys}
        self.sample_count_by_action = {key: 0 for key in self.action_keys}
        self.per_horizon = {}
        self.chunks = 0

        prior = dict(prior_state or {})
        if prior.get("contract") == self.CONTRACT:
            self.chunks = int(prior.get("chunks") or 0)
            for key, raw in dict(prior.get("dwells") or {}).items():
                slot = self.dwells.setdefault(
                    str(key),
                    {
                        "count": 0,
                        "total_seconds": 0.0,
                        "durations": [],
                        "quantiles_approximate": False,
                    },
                )
                slot["count"] = int(raw.get("count") or 0)
                slot["total_seconds"] = _finite(raw.get("total_seconds"))
                slot["durations"] = [
                    max(0.0, _finite(x))
                    for x in list(raw.get("durations") or ())[:self.dwell_sample_cap]
                ]
                slot["quantiles_approximate"] = bool(
                    raw.get("quantiles_approximate")
                )
            for name, raw in dict(prior.get("samples") or {}).items():
                slot = self.samples.setdefault(str(name), _sample_bucket())
                for field, value in dict(raw or {}).items():
                    slot[str(field)] = int(value or 0)
            for name, value in dict(prior.get("excluded") or {}).items():
                self.excluded[str(name)] = int(value or 0)
            self.mass = {
                **_mass_bucket(),
                **{
                    key: _finite(value)
                    for key, value in dict(prior.get("mass") or {}).items()
                },
            }
            for attr, source in (
                ("mass_by_source", prior.get("mass_by_source")),
                ("mass_by_provenance", prior.get("mass_by_provenance")),
                ("mass_by_action", prior.get("mass_by_action")),
            ):
                target = getattr(self, attr)
                for name, raw in dict(source or {}).items():
                    target[str(name)] = {
                        **_mass_bucket(),
                        **{
                            key: _finite(value)
                            for key, value in dict(raw or {}).items()
                        },
                    }
            for key, value in dict(
                prior.get("sample_count_by_action") or {}
            ).items():
                self.sample_count_by_action[str(key)] = int(value or 0)
            self.per_horizon = dict(prior.get("per_horizon") or {})

    def begin_chunk(self):
        self.chunks += 1

    def export_state(self):
        return {
            "contract": self.CONTRACT,
            "chunks": int(self.chunks),
            "dwells": self.dwells,
            "samples": self.samples,
            "excluded": self.excluded,
            "mass": self.mass,
            "mass_by_source": self.mass_by_source,
            "mass_by_provenance": self.mass_by_provenance,
            "mass_by_action": self.mass_by_action,
            "sample_count_by_action": self.sample_count_by_action,
            "per_horizon": self.per_horizon,
        }

    def _action_key(self, action_idx):
        try:
            idx = int(action_idx)
        except (TypeError, ValueError):
            return str(action_idx)
        if 0 <= idx < len(self.action_keys):
            return self.action_keys[idx]
        return str(idx)

    def record_dwell(self, action_idx, dwell_seconds, *, excluded_reason=None):
        key = self._action_key(action_idx)
        slot = self.dwells.setdefault(
            key,
            {
                "count": 0,
                "total_seconds": 0.0,
                "durations": [],
                "quantiles_approximate": False,
            },
        )
        dwell = max(0.0, _finite(dwell_seconds))
        slot["count"] += 1
        slot["total_seconds"] += dwell
        durations = slot["durations"]
        if len(durations) < self.dwell_sample_cap:
            durations.append(dwell)
        else:
            seen = int(slot["count"])
            replace_every = max(
                2, int(math.ceil(seen / float(self.dwell_sample_cap)))
            )
            if seen % replace_every == 0:
                durations[
                    (seen // replace_every) % self.dwell_sample_cap
                ] = dwell
            slot["quantiles_approximate"] = True
        if excluded_reason:
            reason = str(excluded_reason)
            self.excluded[reason] = int(self.excluded.get(reason) or 0) + 1

    def record_excluded(self, reason, count=1):
        reason = str(reason)
        self.excluded[reason] = int(self.excluded.get(reason) or 0) + int(count)

    @staticmethod
    def _add_mass(bucket, *, raw, decayed, reward):
        bucket["raw_sample_mass"] += raw
        bucket["time_decayed_sample_mass"] += decayed
        weighted_reward = decayed * reward
        if weighted_reward >= 0.0:
            bucket["positive_reward_mass"] += weighted_reward
        else:
            bucket["negative_reward_mass"] += -weighted_reward
        bucket["absolute_reward_mass"] += abs(weighted_reward)

    def record_sample(
        self,
        source,
        action_idx,
        reward,
        sample_ts,
        head,
        *,
        split="train",
        provenance="unknown",
        horizon=None,
        raw_mass=1.0,
    ):
        source = str(source)
        split = str(split)
        provenance = str(provenance or "unknown")
        raw = max(0.0, _finite(raw_mass, 1.0))
        if raw <= 0.0:
            return
        reward = max(-1.0, min(1.0, _finite(reward)))
        decay = max(0.0, _finite(head.sample_weight(sample_ts), 1.0))
        decayed = raw * decay

        sample_slot = self.samples.setdefault(source, _sample_bucket())
        sample_slot["total"] += 1
        if split not in sample_slot:
            sample_slot[split] = 0
        sample_slot[split] += 1

        action_key = self._action_key(action_idx)
        self.sample_count_by_action[action_key] = int(
            self.sample_count_by_action.get(action_key) or 0
        ) + 1
        self.mass_by_action.setdefault(action_key, _mass_bucket())
        self.mass_by_source.setdefault(source, _mass_bucket())
        self.mass_by_provenance.setdefault(provenance, _mass_bucket())
        for bucket in (
            self.mass,
            self.mass_by_action[action_key],
            self.mass_by_source[source],
            self.mass_by_provenance[provenance],
        ):
            self._add_mass(
                bucket, raw=raw, decayed=decayed, reward=reward
            )

        if horizon is not None:
            hkey = str(int(horizon))
            h = self.per_horizon.setdefault(
                hkey,
                {
                    "samples": {
                        name: _sample_bucket()
                        for name in ("onset", "persistence", "upstream")
                    },
                    "mass": _mass_bucket(),
                    "mass_by_action": {
                        key: _mass_bucket() for key in self.action_keys
                    },
                },
            )
            hsample = h["samples"].setdefault(source, _sample_bucket())
            hsample["total"] += 1
            if split not in hsample:
                hsample[split] = 0
            hsample[split] += 1
            h["mass_by_action"].setdefault(action_key, _mass_bucket())
            self._add_mass(
                h["mass"], raw=raw, decayed=decayed, reward=reward
            )
            self._add_mass(
                h["mass_by_action"][action_key],
                raw=raw,
                decayed=decayed,
                reward=reward,
            )

    @staticmethod
    def _mlp_summary(
        train_rows, holdout_rows, artifact, action_count
    ):
        train_rows = list(train_rows or ())
        holdout_rows = list(holdout_rows or ())
        counts = [0] * max(0, int(action_count))
        for row in train_rows:
            try:
                idx = int(row.get("action_idx"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(counts):
                counts[idx] += 1
        present = sum(1 for count in counts if count > 0)
        total = sum(counts)
        weights = [1.0] * len(counts)
        if total > 0 and present > 1:
            for idx, count in enumerate(counts):
                if count > 0:
                    weights[idx] = min(
                        4.0, max(0.25, total / float(present * count))
                    )
        artifact = dict(artifact or {})
        trainer = dict(artifact.get("trainer") or {})
        tournament = dict(artifact.get("tournament") or {})
        class_counts = trainer.get("class_counts") or {
            str(i): int(value) for i, value in enumerate(counts)
        }
        class_weight = trainer.get("class_weight") or {
            str(i): float(value) for i, value in enumerate(weights)
        }
        return {
            "train_sample_count": int(
                trainer.get("samples") or len(train_rows)
            ),
            "retained_train_rows": len(train_rows),
            "holdout_sample_count": len(holdout_rows),
            "class_counts": {
                str(key): int(value or 0)
                for key, value in dict(class_counts).items()
            },
            "class_weight": {
                str(key): _finite(value, 1.0)
                for key, value in dict(class_weight).items()
            },
            "tournament": tournament,
        }

    def finalize(
        self,
        policy,
        *,
        benchmark=None,
        neural_train_rows=None,
        neural_holdout_rows=None,
        neural_artifact=None,
    ):
        dwell = {}
        action_time = {}
        for key, raw in self.dwells.items():
            values = sorted(
                float(value) for value in raw.get("durations") or ()
            )
            dwell[key] = {
                "count": int(raw.get("count") or 0),
                "total_seconds": _finite(raw.get("total_seconds")),
                "median_seconds": _percentile(values, 0.50),
                "p10_seconds": _percentile(values, 0.10),
                "p50_seconds": _percentile(values, 0.50),
                "p90_seconds": _percentile(values, 0.90),
                "quantile_sample_count": len(values),
                "quantiles_approximate": bool(
                    raw.get("quantiles_approximate")
                ),
            }
            action_time[key] = dwell[key]["total_seconds"]

        ridge_per_horizon = {}
        ridge_counts = {key: 0.0 for key in self.action_keys}
        ridge_rewards = {key: 0.0 for key in self.action_keys}
        ridge_total_updates = 0.0
        for horizon, head in sorted(
            getattr(policy, "heads", {}).items(),
            key=lambda item: int(item[0]),
        ):
            counts = {
                self.action_keys[i]: _finite(value)
                for i, value in enumerate(head.counts)
            }
            rewards = {
                self.action_keys[i]: _finite(value)
                for i, value in enumerate(head.reward_sums)
            }
            for key, value in counts.items():
                ridge_counts[key] = ridge_counts.get(key, 0.0) + value
            for key, value in rewards.items():
                ridge_rewards[key] = ridge_rewards.get(key, 0.0) + value
            total = _finite(head.total_updates)
            ridge_total_updates += total
            ridge_per_horizon[str(int(horizon))] = {
                "counts": counts,
                "reward_sums": rewards,
                "total_updates": total,
                "covered_actions": sum(
                    1 for value in counts.values() if value > 0.0
                ),
                "action_count": len(counts),
                "count_ratio": _ordered_action_ratio(
                    counts, self.action_keys
                ),
            }

        benchmark = dict(benchmark or {})
        per_action_benchmark = {}
        for key, value in dict(
            benchmark.get("per_action") or {}
        ).items():
            samples = int((value or {}).get("samples") or 0)
            correct = int((value or {}).get("correct") or 0)
            per_action_benchmark[str(key)] = {
                "samples": samples,
                "correct": correct,
                "accuracy": (
                    correct / samples if samples else None
                ),
            }
        populated_accuracy = [
            row["accuracy"]
            for row in per_action_benchmark.values()
            if row["accuracy"] is not None
        ]
        binary = (
            len(self.actions) <= 2
            or str(
                getattr(policy, "agent", {}).get("target_property")
            ) == "power"
        )
        balanced_accuracy = (
            sum(populated_accuracy) / len(populated_accuracy)
            if binary and populated_accuracy
            else None
        )

        mlp = self._mlp_summary(
            neural_train_rows,
            neural_holdout_rows,
            neural_artifact,
            len(self.actions),
        )
        mlp_counts_by_action = {
            self.action_keys[i]: int(
                mlp["class_counts"].get(str(i), 0)
            )
            for i in range(len(self.action_keys))
        }
        action_effective = {
            key: _finite(
                (self.mass_by_action.get(key) or {}).get(
                    "time_decayed_sample_mass"
                )
            )
            for key in self.action_keys
        }
        action_reward = {
            key: _finite(
                (self.mass_by_action.get(key) or {}).get(
                    "absolute_reward_mass"
                )
            )
            for key in self.action_keys
        }
        source_raw = {
            key: _finite(
                (value or {}).get("raw_sample_mass")
            )
            for key, value in self.mass_by_source.items()
        }
        onset_raw = source_raw.get("onset", 0.0)
        persistence_raw = source_raw.get("persistence", 0.0)
        upstream_raw = source_raw.get("upstream", 0.0)

        return {
            "contract": self.CONTRACT,
            "diagnostic_only": True,
            "chunks": int(self.chunks),
            "agent_id": self.agent_id,
            "actions": list(self.actions),
            "dwell": dwell,
            "samples": {
                key: dict(value)
                for key, value in self.samples.items()
            },
            "excluded_samples": {
                key: int(value or 0)
                for key, value in self.excluded.items()
            },
            "mass": {
                key: _finite(value)
                for key, value in self.mass.items()
            },
            "mass_by_source": {
                str(key): {
                    name: _finite(value)
                    for name, value in dict(bucket).items()
                }
                for key, bucket in self.mass_by_source.items()
            },
            "mass_by_provenance": {
                str(key): {
                    name: _finite(value)
                    for name, value in dict(bucket).items()
                }
                for key, bucket in self.mass_by_provenance.items()
            },
            "mass_by_action": {
                str(key): {
                    name: _finite(value)
                    for name, value in dict(bucket).items()
                }
                for key, bucket in self.mass_by_action.items()
            },
            "per_horizon": self.per_horizon,
            "ridge": {
                "counts": ridge_counts,
                "reward_sums": ridge_rewards,
                "total_updates": ridge_total_updates,
                "covered_actions": sum(
                    1 for value in ridge_counts.values()
                    if value > 0.0
                ),
                "action_count": len(ridge_counts),
                "per_horizon": ridge_per_horizon,
            },
            "qualification": {
                "samples": int(benchmark.get("samples") or 0),
                "correct": int(benchmark.get("correct") or 0),
                "per_action": per_action_benchmark,
                "balanced_accuracy": balanced_accuracy,
            },
            "tiny_mlp": mlp,
            "derived": {
                "effective_update_ratio": _ordered_action_ratio(
                    action_effective, self.action_keys
                ),
                "effective_reward_ratio": _ordered_action_ratio(
                    action_reward, self.action_keys
                ),
                "persistence_to_onset_ratio": _ratio(
                    persistence_raw, onset_raw
                ),
                "upstream_share": _ratio(
                    upstream_raw,
                    max(0.0, sum(source_raw.values())),
                ),
                "action_sample_ratio": _ordered_action_ratio(
                    self.sample_count_by_action,
                    self.action_keys,
                ),
                "action_time_ratio": _ordered_action_ratio(
                    action_time, self.action_keys
                ),
                "ridge_count_ratio": _ordered_action_ratio(
                    ridge_counts, self.action_keys
                ),
                "mlp_class_ratio": _ordered_action_ratio(
                    mlp_counts_by_action, self.action_keys
                ),
            },
        }
