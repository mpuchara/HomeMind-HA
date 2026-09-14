import json
import math
import random
import threading
import uuid
from settings import (OPTIONS, clamp, now_ts, sigmoid)
from context import (ExplicitFeatureSchema, action_values, build_explicit_features, controllable_context_exclusions, electrical_context_exclusions, parse_horizons, select_context_entities)
from policy_backend import PolicyBackend
from home_state import FEATURE_NAMES

class DiagonalLinUCB:
    """Per-horizon lightweight RL head with context-support statistics."""
    def __init__(self, dims, actions, alpha=0.65, model=None):
        self.dims = int(dims); self.actions = [float(x) for x in actions]; self.alpha = float(alpha)
        n = len(self.actions)
        valid = bool(model and int(model.get("dims", -1)) == self.dims and len(model.get("actions", [])) == n)
        if valid:
            self.a = model["a"]; self.b = model["b"]
            self.counts = model.get("counts", [0] * n); self.reward_sums = model.get("reward_sums", [0.0] * n)
            self.ctx_sum = model.get("ctx_sum", [[0.0] * self.dims for _ in range(n)])
            self.ctx_sq = model.get("ctx_sq", [[0.0] * self.dims for _ in range(n)])
            self.total_updates = float(model.get("total_updates", sum(self.counts)))
            self.validation_weight = float(model.get("validation_weight", 0.0))
            self.validation_correct_weight = float(model.get("validation_correct_weight", 0.0))
            self.validation_samples = float(model.get("validation_samples", 0))
            self.validation_pred_weight = list(model.get("validation_pred_weight", [0.0] * n))
            self.validation_pred_correct_weight = list(model.get("validation_pred_correct_weight", [0.0] * n))
            if len(self.validation_pred_weight) != n:
                self.validation_pred_weight = [0.0] * n
            if len(self.validation_pred_correct_weight) != n:
                self.validation_pred_correct_weight = [0.0] * n
        else:
            self.a = [[1.0] * self.dims for _ in range(n)]; self.b = [[0.0] * self.dims for _ in range(n)]
            self.counts = [0] * n; self.reward_sums = [0.0] * n
            self.ctx_sum = [[0.0] * self.dims for _ in range(n)]; self.ctx_sq = [[0.0] * self.dims for _ in range(n)]
            self.total_updates = 0
            self.validation_weight = 0.0
            self.validation_correct_weight = 0.0
            self.validation_samples = 0
            self.validation_pred_weight = [0.0] * n
            self.validation_pred_correct_weight = [0.0] * n

        self.last_decay_ts = float((model or {}).get("last_decay_ts", now_ts()))
        self.half_life_days = float(OPTIONS.get("policy_half_life_days", 30))

    def decay(self, now=None):
        now = now_ts() if now is None else float(now)
        elapsed = max(0, now - self.last_decay_ts)
        if elapsed < 60:
            return
        factor = math.exp(-math.log(2)*elapsed/(max(1,self.half_life_days)*86400))
        for arm in range(len(self.actions)):
            for idx in range(self.dims):
                # Ridge prior remains 1, only empirical evidence decays.
                self.a[arm][idx] = 1 + (self.a[arm][idx]-1)*factor
                self.b[arm][idx] *= factor
                self.ctx_sum[arm][idx] *= factor
                self.ctx_sq[arm][idx] *= factor
            self.counts[arm] *= factor
            self.reward_sums[arm] *= factor
            self.validation_pred_weight[arm] *= factor
            self.validation_pred_correct_weight[arm] *= factor
        self.total_updates *= factor
        self.validation_weight *= factor
        self.validation_correct_weight *= factor
        self.validation_samples *= factor
        self.last_decay_ts = now

    def sample_weight(self, sample_ts=None):
        if sample_ts is None:
            return 1.0
        return math.exp(-math.log(2)*max(0, self.last_decay_ts-float(sample_ts))/(max(1,self.half_life_days)*86400))

    def _arm(self, action_idx, x):
        aa, bb = self.a[action_idx], self.b[action_idx]
        mean = 0.0; uncertainty_sq = 0.0; active = 0
        for idx, value in x.items():
            if idx >= self.dims: continue
            inv = 1.0 / max(aa[idx], 1e-9)
            mean += (bb[idx] * inv) * value
            uncertainty_sq += value * value * inv; active += 1
        uncertainty = math.sqrt(max(0.0, uncertainty_sq) / max(1, active))
        return mean, uncertainty

    def context_support(self, action_idx, x):
        # Novelty is judged against the whole historical context distribution, while
        # local action coverage still matters. This avoids unfairly marking a continuous
        # brightness/setpoint bin as OOD merely because that exact bin was used rarely.
        total = max(0, self.total_updates)
        if total < 3:
            return (0.0, 1.0)
        local_n = self.counts[action_idx]
        z2 = 0.0; used = 0
        for idx, value in x.items():
            if idx <= 0 or idx >= self.dims: continue
            ss = sum(a[idx] for a in self.ctx_sum)
            sq = sum(a[idx] for a in self.ctx_sq)
            mean = ss / total
            var = max(0.020, sq / total - mean * mean)
            z = (value - mean) / math.sqrt(var)
            z2 += min(16.0, z * z); used += 1
        dist = math.sqrt(z2 / max(1, used))
        novelty = clamp(1.0 - math.exp(-dist / 2.6), 0.0, 1.0)
        global_coverage = 1.0 - math.exp(-total / 20.0)
        local_coverage = 1.0 - math.exp(-local_n / 3.0)
        support = clamp(global_coverage * (1.0 - novelty) * (0.55 + 0.45 * local_coverage), 0.0, 1.0)
        return support, novelty

    def evaluate(self, x):
        arms = []
        _, novelty = self.context_support(0, x)
        global_coverage = 1.0 - math.exp(-max(0, self.total_updates) / 20.0)
        for i, value in enumerate(self.actions):
            mean, uncertainty = self._arm(i, x)
            support = clamp(global_coverage * (1-novelty) * (0.55 + 0.45*(1-math.exp(-self.counts[i]/3.0))), 0, 1)
            arms.append({"index": i, "value": value, "mean": mean, "uncertainty": uncertainty,
                         "ucb": mean + self.alpha * uncertainty, "count": int(self.counts[i]),
                         "support": support, "novelty": novelty})
        return arms

    def choose(self, x, explore=False, allowed_indices=None):
        arms = self.evaluate(x)
        allowed = set(allowed_indices) if allowed_indices is not None else set(range(len(arms)))
        candidates = [a for a in arms if a["index"] in allowed] or arms
        key = "ucb" if explore else "mean"
        best_score = max(a[key] for a in candidates)
        tied = [a for a in candidates if abs(a[key] - best_score) < 1e-12]
        chosen = dict(random.choice(tied) if explore else min(tied, key=lambda a: a["index"]))
        structural = self.structural_confidence(arms, chosen["index"])
        calibration = self.calibration(chosen["index"])
        confidence = min(structural, calibration["ceiling"])
        chosen["structural_confidence"] = structural
        chosen["validation_accuracy"] = calibration["accuracy"]
        chosen["validation_lower_bound"] = calibration["ceiling"]
        chosen["validation_samples"] = calibration["samples"]
        return chosen, confidence, arms

    def structural_confidence(self, arms, chosen_idx):
        if self.total_updates <= 0: return 0.0
        chosen = arms[chosen_idx]
        ranked = sorted((a["mean"] for a in arms), reverse=True)
        margin = ranked[0] - ranked[1] if len(ranked) > 1 else abs(ranked[0])
        margin_score = 2.0 * abs(sigmoid(3.5 * margin) - 0.5)
        uncertainty_score = math.exp(-1.6 * chosen["uncertainty"])
        coverage = 1.0 - math.exp(-self.total_updates / max(18.0, len(self.actions) * 1.8))
        local_coverage = 1.0 - math.exp(-chosen["count"] / 4.0)
        raw = coverage * (0.38 * uncertainty_score + 0.27 * margin_score + 0.18 * local_coverage + 0.17 * chosen["support"])
        return clamp(raw, 0.0, 0.995)

    @staticmethod
    def _wilson_lower(correct, total, z=1.0):
        if total <= 0:
            return 0.0
        p = clamp(float(correct) / float(total), 0.0, 1.0)
        den = 1.0 + (z * z) / total
        centre = p + (z * z) / (2.0 * total)
        spread = z * math.sqrt(max(0.0, p * (1.0 - p) / total + (z * z) / (4.0 * total * total)))
        return clamp((centre - spread) / den, 0.0, 1.0)

    def calibration(self, predicted_idx):
        min_samples = max(4, int(OPTIONS.get("confidence_min_validation_samples", 12)))
        action_total = float(self.validation_pred_weight[predicted_idx]) if predicted_idx < len(self.validation_pred_weight) else 0.0
        action_correct = float(self.validation_pred_correct_weight[predicted_idx]) if predicted_idx < len(self.validation_pred_correct_weight) else 0.0
        # Use action-specific reliability when it has enough held-out support, otherwise
        # fall back to the global held-out reliability. Confidence is never allowed above
        # a conservative Wilson lower bound. This makes "90% confidence" mean the policy
        # has actually been close to that reliable on unseen recent history.
        # A common OFF action cannot certify an untested ON action.
        total, correct = action_total, action_correct
        accuracy = (correct / total) if total > 0 else 0.0
        if total < min_samples:
            # No out-of-sample proof yet: cap confidence below Control defaults.
            progress = clamp(total / max(1.0, float(min_samples)), 0.0, 1.0)
            ceiling = 0.25 + 0.35 * progress
        else:
            ceiling = self._wilson_lower(correct, total, z=1.0)
        return {"accuracy": accuracy, "ceiling": clamp(ceiling, 0.0, 0.995), "samples": int(round(total))}

    def validate(self, action_idx, x, reward, sample_ts=None):
        self.decay()
        reward = clamp(float(reward), -1.0, 1.0)
        # Preserve the existing positive-validation threshold: weak/neutral evidence is
        # ignored, while accepted positive examples (>= 0.15) keep the old behavior.
        if 0.0 <= reward < 0.15:
            return
        arms = self.evaluate(x)
        predicted = max(arms, key=lambda a: a["mean"])["index"]
        weight = max(0.25, abs(reward)) * self.sample_weight(sample_ts)

        if reward < 0.0:
            # A negative example identifies only the rejected action. If the held-out
            # policy predicted that exact action, record a validation failure for that
            # predicted arm. Predicting another action is not evidence that it was right.
            if predicted != action_idx:
                return
            self.validation_weight += weight
            self.validation_samples += 1
            self.validation_pred_weight[predicted] += weight
            return

        # Positive reward means the logged desired state was accepted.
        correct = predicted == action_idx
        self.validation_weight += weight
        self.validation_correct_weight += weight if correct else 0.0
        self.validation_samples += 1
        self.validation_pred_weight[predicted] += weight
        if correct:
            self.validation_pred_correct_weight[predicted] += weight

    def update(self, action_idx, x, reward, sample_ts=None):
        self.decay()
        weight = self.sample_weight(sample_ts)
        reward = clamp(float(reward), -1.0, 1.0)
        aa, bb = self.a[action_idx], self.b[action_idx]
        ss, sq = self.ctx_sum[action_idx], self.ctx_sq[action_idx]
        for idx, value in x.items():
            if idx >= self.dims: continue
            aa[idx] += weight * value * value; bb[idx] += weight * reward * value
            ss[idx] += weight * value; sq[idx] += weight * value * value
        self.counts[action_idx] += weight; self.reward_sums[action_idx] += weight * reward; self.total_updates += weight

    def export(self):
        return {"last_decay_ts": self.last_decay_ts, "version": 5, "dims": self.dims, "actions": self.actions, "alpha": self.alpha,
                "a": self.a, "b": self.b, "counts": self.counts, "reward_sums": self.reward_sums,
                "ctx_sum": self.ctx_sum, "ctx_sq": self.ctx_sq, "total_updates": self.total_updates,
                "validation_weight": self.validation_weight,
                "validation_correct_weight": self.validation_correct_weight,
                "validation_samples": self.validation_samples,
                "validation_pred_weight": self.validation_pred_weight,
                "validation_pred_correct_weight": self.validation_pred_correct_weight}


class MultiHorizonPolicy(PolicyBackend):
    VERSION = 10
    def __init__(self, agent, state_map, registry, hint_entities, model=None, relevance_scores=None, context_engine=None):
        self.agent = agent
        self.lock = threading.RLock()
        self.context_engine = context_engine
        self.model_revision = (model or {}).get("model_revision") or str(uuid.uuid4())
        self.dims = int(OPTIONS.get("feature_dimensions", 128))
        self.actions = action_values(agent)
        self.alpha = float(OPTIONS.get("rl_alpha", 0.65))
        self.horizons = parse_horizons(agent)
        self.registry = registry or {}
        excluded_control, control_meta = controllable_context_exclusions(state_map, self.registry)
        excluded_electrical, electrical_meta = electrical_context_exclusions(state_map, self.registry)
        self.excluded_context_entities = excluded_control | excluded_electrical
        self.context_exclusion_meta = {**control_meta, **electrical_meta}
        valid_model = bool(model and int(model.get("version", 0)) == self.VERSION)
        raw_schema = (model or {}).get("schema") if valid_model else None
        self.schema = ExplicitFeatureSchema.from_export(raw_schema, self.dims)
        selection_meta = dict((model or {}).get("selection_meta") or {}) if valid_model else {}
        if self.schema is None:
            selected, selection_meta = select_context_entities(agent, state_map, registry, hint_entities, relevance_scores=relevance_scores)
            self.schema = ExplicitFeatureSchema(self.dims, selected)
        if not selection_meta or not selection_meta.get("selection_reasons"):
            _, fresh_meta = select_context_entities(agent, state_map, registry, hint_entities, max_entities=len(self.schema.entities), relevance_scores=relevance_scores)
            fresh_meta["selected_entities"] = len(self.schema.entities)
            fresh_meta["selection_reasons"] = {k: v for k, v in fresh_meta.get("selection_reasons", {}).items() if k in set(self.schema.entities)}
            fresh_meta["primary_local_sensors"] = [x for x in fresh_meta.get("primary_local_sensors", []) if x in set(self.schema.entities)]
            fresh_meta["primary_local_sensor"] = next(iter(fresh_meta["primary_local_sensors"]), None)
            if fresh_meta.get("primary_occupancy_sensor") not in set(self.schema.entities):
                fresh_meta["primary_occupancy_sensor"] = fresh_meta.get("primary_local_sensor")
            fresh_meta["causal_presence_scores"] = {k:v for k,v in (fresh_meta.get("causal_presence_scores") or {}).items() if k in set(self.schema.entities)}
            fresh_meta["causal_behaviour_scores"] = {k:v for k,v in (fresh_meta.get("causal_behaviour_scores") or {}).items() if k in set(self.schema.entities)}
            fresh_meta["primary_behavioural_drivers"] = [x for x in fresh_meta.get("primary_behavioural_drivers", []) if x in set(self.schema.entities)]
            fresh_meta["upstream_sensors"] = [x for x in fresh_meta.get("upstream_sensors", []) if x in set(self.schema.entities)]
            selection_meta = fresh_meta
        selection_meta.update(self.context_exclusion_meta)
        self.selection_meta = selection_meta
        raw_heads = (model or {}).get("heads", {}) if model and int(model.get("version", 0)) == self.VERSION else {}
        self.heads = {h: DiagonalLinUCB(self.dims, self.actions, self.alpha, raw_heads.get(str(h))) for h in self.horizons}

    @property
    def total_updates(self):
        return max([h.total_updates for h in self.heads.values()] or [0])

    def features(self, state_map, temporal, at_ts=None):
        excluded = self.context_engine.excluded if self.context_engine else self.excluded_context_entities
        vector, labels, meta = build_explicit_features(self.schema, state_map, temporal, at_ts,
            self.agent, excluded_entities=excluded)
        provider = getattr(temporal, "home_context", None) or self.context_engine
        if provider:
            forecast = provider.forecast(self.agent['target_entity'], at_ts if at_ts is not None else now_ts())
            for offset, name in enumerate(FEATURE_NAMES):
                index = self.dims - 7 + offset
                vector[index] = float(forecast.get(name, 0))
                labels[index] = ['home:' + name]
            meta['home_forecast'] = forecast
        return vector, labels, meta

    def choose(self, features, explore=False, allowed_indices=None):
        # v0.6 defaults to one reactive head. Multiple heads remain configurable for
        # experimentation, but the normal product path evaluates the current event-time
        # context and acts immediately.
        self.decay()
        candidates = []
        for h, head in self.heads.items():
            chosen, conf, arms = head.choose(features, explore=explore, allowed_indices=allowed_indices)
            utility = chosen["mean"] + 0.20 * conf + 0.18 * chosen["support"] - 0.12 * chosen["novelty"] - 0.012 * (h / 60.0)
            candidates.append((utility, h, chosen, conf, arms))
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, horizon, chosen, conf, arms = candidates[0]
        return chosen, conf, arms, horizon, chosen.get("support", 0.0), chosen.get("novelty", 1.0)

    def update(self, horizon, action_idx, features, reward, sample_ts=None):
        with self.lock:
            self.heads[int(horizon)].update(action_idx, features, reward, sample_ts)
            self.model_revision = str(uuid.uuid4())

    def update_all(self, action_idx, features_by_horizon, reward):
        for h, features in features_by_horizon.items():
            if int(h) in self.heads:
                self.heads[int(h)].update(action_idx, features, reward)

    def export(self):
        return {"model_revision": self.model_revision, "version": self.VERSION, "dims": self.dims, "actions": self.actions, "horizons": self.horizons,
                "schema": self.schema.export(), "selection_meta": self.selection_meta,
                "heads": {str(h): head.export() for h, head in self.heads.items()}}



    def predict(self, features):
        with self.lock:
            return self.choose(features, explore=False)

    def serialize(self):
        with self.lock:
            return json.loads(json.dumps(self.export()))

    @classmethod
    def deserialize(cls, raw, **kwargs):
        if raw.get('version') != cls.VERSION or raw.get('schema', {}).get('version') != ExplicitFeatureSchema.VERSION:
            raise ValueError('NEEDS_RETRAIN: incompatible policy/schema')
        return cls(model=raw, **kwargs)

    def decay(self, now=None):
        with self.lock:
            before = [head.last_decay_ts for head in self.heads.values()]
            for head in self.heads.values():
                head.decay(now)
            if before != [head.last_decay_ts for head in self.heads.values()]:
                self.model_revision = str(uuid.uuid4())

    def diagnostics(self):
        return {'backend': 'diagonal_linucb', 'policy_version': self.VERSION,
                'model_revision': self.model_revision, 'effective_updates': self.total_updates,
                'policy_half_life_days': OPTIONS.get('policy_half_life_days', 30)}

    def inference_export(self):
        """Deployment state without training matrices b/A or per-arm raw moments."""
        with self.lock:
            self.decay()
            return {'format': 'homemind-inference-v1', 'policy_version': self.VERSION,
                    'model_revision': self.model_revision, 'schema': self.schema.export(),
                    'home_feature_names': list(FEATURE_NAMES), 'actions': self.actions,
                    'heads': {str(h): {'theta': [[b/a for a,b in zip(aa,bb)] for aa,bb in zip(head.a,head.b)],
                        'inverse_a': [[1/a for a in aa] for aa in head.a],
                        'effective_updates': head.total_updates, 'counts': head.counts,
                        'context_sum': [sum(a[i] for a in head.ctx_sum) for i in range(self.dims)],
                        'context_sq': [sum(a[i] for a in head.ctx_sq) for i in range(self.dims)],
                        'calibration': [head.calibration(i) for i in range(len(self.actions))]}
                        for h,head in self.heads.items()}}
