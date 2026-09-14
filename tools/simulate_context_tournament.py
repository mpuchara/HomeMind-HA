"""Deterministic Sensor Tournament simulator for the 0.12 release contract.

The simulator exercises two production pieces directly:

1. ``ContextTournament.sync_agent`` discovers live challenger candidates without a
   manual Rebuild.
2. ``context_tournament_promotion.install_promotion`` applies the production
   future-only promotion gates and migrates the real ``MultiHorizonPolicy`` schema.

It intentionally does not call Executor or Home Assistant services.  The shadow/service
boundary has dedicated integration tests; this executable focuses on whether future
prequential evidence is strong enough to change the active sensor schema.
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adaptive_ai" / "src"))

from context import ExplicitFeatureSchema
from context_tournament import ContextTournament
from context_tournament_promotion import (
    PROMOTION_EPOCH_VERSION,
    absorb_new_scored_evidence,
    advance_windows,
    install_promotion,
    promotion_eligibility,
    tournament_config,
)
from policy import MultiHorizonPolicy
from storage import Store


ACTIONS = [0.0, 1.0]
DAY = 86400.0


def _state(entity_id, value="off", **attributes):
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": attributes,
        "context": {},
    }


def _agent_payload(name="Tournament simulator"):
    return {
        "name": name,
        "target_entity": "light.target",
        "target_property": "power",
        "min_value": 0,
        "max_value": 1,
        "deadband": 0.5,
        "confidence_threshold": 0.78,
        "action_interval": 1,
        "exploration_step": 1,
        "input_entities": ["*"],
        "mode": "shadow",
    }


def _base_model(start_ts):
    model = {
        "version": 1,
        "action_count": 2,
        "counts": {},
        "samples": 0,
        "active_correct": 0,
        "shadow_correct": 0,
        "class_totals": [0, 0],
        "active_correct_by_class": [0, 0],
        "shadow_correct_by_class": [0, 0],
        "active_abs_error_sum": 0.0,
        "shadow_abs_error_sum": 0.0,
        "observation_opportunities": 0,
        "available_observations": 0,
        "first_observed_ts": float(start_ts),
        "last_observed_ts": float(start_ts),
        "last_scored_ts": None,
        "evaluation_started_ts": float(start_ts),
        "promotion_epoch_version": PROMOTION_EPOCH_VERSION,
        "promotion_window_hours": 24.0,
        "promotion_window_start_ts": float(start_ts),
        "promotion_window_end_ts": float(start_ts) + DAY,
        "promotion_window_samples": 0,
        "promotion_window_class_totals": [0, 0],
        "promotion_window_active_correct_by_class": [0, 0],
        "promotion_window_shadow_correct_by_class": [0, 0],
        "promotion_window_active_abs_error_sum": 0.0,
        "promotion_window_shadow_abs_error_sum": 0.0,
        "promotion_seen_samples": 0,
        "promotion_seen_class_totals": [0, 0],
        "promotion_seen_active_correct_by_class": [0, 0],
        "promotion_seen_shadow_correct_by_class": [0, 0],
        "promotion_seen_active_abs_error_sum": 0.0,
        "promotion_seen_shadow_abs_error_sum": 0.0,
        "promotion_consecutive_wins": 0,
        "promotion_completed_windows": 0,
        "promotion_window_history": [],
    }
    return model


def _score(model, actual, active_correct, challenger_correct, ts):
    idx = int(actual)
    model["samples"] += 1
    model["class_totals"][idx] += 1
    model["active_correct"] += int(bool(active_correct))
    model["shadow_correct"] += int(bool(challenger_correct))
    model["active_correct_by_class"][idx] += int(bool(active_correct))
    model["shadow_correct_by_class"][idx] += int(bool(challenger_correct))
    model["observation_opportunities"] += 1
    model["available_observations"] += 1
    model["last_observed_ts"] = float(ts)
    model["last_scored_ts"] = float(ts)
    absorb_new_scored_evidence(model, ACTIONS, ts, tournament_config())


def _feed_three_winning_windows(model, start_ts, active=(8, 7), challenger=(9, 9)):
    """Feed 60 balanced future outcomes across three non-overlapping 24 h windows."""
    for window in range(3):
        per_class_seen = [0, 0]
        for i in range(20):
            actual = i % 2
            occurrence = per_class_seen[actual]
            per_class_seen[actual] += 1
            active_ok = occurrence < int(active[actual])
            challenger_ok = occurrence < int(challenger[actual])
            # Keep every scored event strictly inside its current window.
            ts = float(start_ts) + window * DAY + (i + 1) * (DAY / 24.0)
            _score(model, actual, active_ok, challenger_ok, ts)
    # Exactly three days of observed availability and three completed windows.
    model["last_observed_ts"] = float(start_ts) + 3 * DAY
    advance_windows(model, ACTIONS, float(start_ts) + 3 * DAY + 1.0, tournament_config())
    return model


class _PromotionService:
    """Small harness around the production promotion state machine."""

    def __init__(self, store, engine, tournament_state):
        self.store = store
        self.engine = engine
        self._state = dict(tournament_state)
        self._models = {}
        self._auto_promotion_installed = False

    def state(self, agent_id):
        return dict(self._state)

    def sync_agent(self, agent, **kwargs):
        policy = kwargs.get("policy") or self.engine.models[agent["id"]]
        old = list(self._state.get("active_features") or [])
        new = list(policy.schema.entities)
        if new != old:
            self._state["previous_schema"] = old
            self._state["schema_revision"] = int(self._state.get("schema_revision") or 0) + 1
        self._state["active_features"] = new
        self._state["challenger_features"] = [
            x for x in self._state.get("challenger_features", []) if x not in set(new)
        ]
        return dict(self._state)

    def observe_shadow(self, agent, state_map=None, changed_entities=None):
        return {"mode": "shadow_only", "controls_device": False}

    def shadow_status(self, agent):
        return {
            "mode": "shadow_only",
            "controls_device": False,
            "challengers": [
                {"entity_id": x} for x in self._state.get("challenger_features", [])
            ],
        }

    def _load_shadow_model(self, agent_id, challenger, action_count):
        return self._models.setdefault(str(challenger), _base_model(0.0))

    def _save_shadow_model(self, agent_id, challenger, model):
        self._models[str(challenger)] = model


def _build_harness(root, *, active_features, extra_states, feature_scores):
    store = Store(Path(root) / "tournament.db")
    agent = store.create_agent(_agent_payload())
    states = {"light.target": _state("light.target", "off")}
    for entity_id in active_features:
        states[entity_id] = _state(entity_id, "off", device_class="occupancy")
    states.update(extra_states)
    engine = SimpleNamespace(
        state_map=states,
        entity_registry={},
        context_relevance={agent["id"]: dict(feature_scores)},
        models={},
        runtime={},
        lock=threading.RLock(),
        wake_event=threading.Event(),
    )
    policy = MultiHorizonPolicy(agent, states, {}, set())
    policy.schema = ExplicitFeatureSchema(policy.dims, list(active_features))
    policy.selection_meta = {
        "selection_reasons": {x: ["simulator"] for x in active_features}
    }
    engine.models[agent["id"]] = policy
    store.save_model(agent["id"], policy.serialize())
    discovery = ContextTournament(store, engine)
    tournament = discovery.sync_agent(
        agent, policy=policy, feature_scores=feature_scores, evaluated_at=1000.0
    )
    service = install_promotion(_PromotionService(store, engine, tournament))
    return store, agent, engine, policy, tournament, service


def scenario_good_new_sensor():
    """A is the champion; B appears later, proves +15 pp future value and is promoted."""
    start = 1_000_000.0
    active = ["binary_sensor.sensor_a"] + [f"sensor.filler_{i}" for i in range(7)]
    b = "binary_sensor.sensor_b"
    with tempfile.TemporaryDirectory(prefix="hm-tournament-good-") as root:
        initial_scores = {entity: 0.55 + i * 0.01 for i, entity in enumerate(active)}
        store, agent, engine, policy, initial, _ = _build_harness(
            root, active_features=active, extra_states={}, feature_scores=initial_scores
        )
        if initial["challenger_features"]:
            raise AssertionError("B must not exist in the first half")

        # B appears in live HA state. No Rebuild is called; the normal tournament sync
        # sees its relevance score and admits it as a challenger.
        engine.state_map[b] = _state(b, "off", device_class="occupancy")
        scores = dict(initial_scores)
        scores[b] = 0.95
        discovery = ContextTournament(store, engine)
        tournament = discovery.sync_agent(
            agent, policy=policy, feature_scores=scores, evaluated_at=start
        )
        if b not in tournament["challenger_features"]:
            raise AssertionError("B did not become a challenger")

        service = install_promotion(_PromotionService(store, engine, tournament))
        model = _feed_three_winning_windows(_base_model(start), start)
        service._models[b] = model
        eligibility = promotion_eligibility(model, ACTIONS, start + 3 * DAY + 1.0)
        if not eligibility["ready"]:
            raise AssertionError({"eligibility": eligibility, "model": model})
        with patch("context_tournament_promotion.time.time", return_value=start + 3 * DAY + 1.0):
            service.observe_shadow(agent, engine.state_map, {"light.target"})
        promoted = b in policy.schema.entities
        if not promoted:
            raise AssertionError("B won future validation but was not promoted")
        return {
            "scenario": "A_good_new_sensor",
            "initial_challengers": initial["challenger_features"],
            "challengers_after_b_appears": tournament["challenger_features"],
            "baseline_score": 0.75,
            "challenger_score": 0.90,
            "gain": eligibility["gain"],
            "samples": eligibility["samples"],
            "days_observed": eligibility["days_observed"],
            "consecutive_wins": eligibility["consecutive_wins"],
            "promoted": promoted,
            "schema": list(policy.schema.entities),
        }


def scenario_random_correlation(noise_sensors=300):
    """Hundreds of tiny-sample false correlations may challenge, but cannot enter schema."""
    start = 2_000_000.0
    active = ["binary_sensor.sensor_a"] + [f"sensor.filler_{i}" for i in range(7)]
    extra = {
        f"sensor.noise_{i:03d}": _state(f"sensor.noise_{i:03d}", "0")
        for i in range(int(noise_sensors))
    }
    # Four sensors look perfect on a deliberately tiny discovery sample.  This mimics
    # the multiple-comparisons trap the future-only tournament is meant to contain.
    scores = {entity: 0.70 for entity in active}
    for i in range(int(noise_sensors)):
        scores[f"sensor.noise_{i:03d}"] = 1.0 if i < 4 else 0.20 + (i % 17) / 100.0

    with tempfile.TemporaryDirectory(prefix="hm-tournament-noise-") as root:
        _, agent, engine, policy, tournament, service = _build_harness(
            root, active_features=active, extra_states=extra, feature_scores=scores
        )
        challengers = list(tournament["challenger_features"])
        if len(challengers) != 4:
            raise AssertionError({"challengers": challengers})
        before = list(policy.schema.entities)
        reports = []
        for offset, challenger in enumerate(challengers):
            model = _feed_three_winning_windows(
                _base_model(start + offset * 10.0), start + offset * 10.0,
                active=(8, 7), challenger=(5, 5),
            )
            service._models[challenger] = model
            reports.append(promotion_eligibility(
                model, ACTIONS, start + offset * 10.0 + 3 * DAY + 1.0
            ))
        with patch("context_tournament_promotion.time.time", return_value=start + 3 * DAY + 100.0):
            service.observe_shadow(agent, engine.state_map, {"light.target"})
        if list(policy.schema.entities) != before:
            raise AssertionError("A noise challenger entered the active schema")
        if any(x["ready"] for x in reports):
            raise AssertionError(reports)
        return {
            "scenario": "B_random_correlation",
            "noise_sensors": int(noise_sensors),
            "tiny_sample_false_positive_score": 1.0,
            "challengers": challengers,
            "future_gain": [x["gain"] for x in reports],
            "promotions": 0,
            "schema_unchanged": True,
        }


def scenario_concept_drift():
    """A starts best; after drift B becomes the live challenger and replaces A."""
    start = 3_000_000.0
    a = "binary_sensor.sensor_a"
    b = "binary_sensor.sensor_b"
    active = [a] + [f"sensor.filler_{i}" for i in range(7)]
    extra = {b: _state(b, "off", device_class="occupancy")}
    initial_scores = {entity: 0.80 for entity in active}
    initial_scores[b] = 0.0

    with tempfile.TemporaryDirectory(prefix="hm-tournament-drift-") as root:
        store, agent, engine, policy, initial, _ = _build_harness(
            root, active_features=active, extra_states=extra, feature_scores=initial_scores
        )
        if b in initial["challenger_features"]:
            raise AssertionError("B should be irrelevant before concept drift")

        # After the behavioural change the live relevance stream makes B discoverable.
        # A has decayed to the weakest discovery-ranked active slot. No Rebuild occurs.
        drift_scores = {entity: 0.70 for entity in active}
        drift_scores[a] = 0.10
        drift_scores[b] = 0.97
        discovery = ContextTournament(store, engine)
        tournament = discovery.sync_agent(
            agent, policy=policy, feature_scores=drift_scores, evaluated_at=start
        )
        if b not in tournament["challenger_features"]:
            raise AssertionError("B did not enter the challenger set after drift")

        service = install_promotion(_PromotionService(store, engine, tournament))
        model = _feed_three_winning_windows(_base_model(start), start)
        service._models[b] = model
        with patch("context_tournament_promotion.time.time", return_value=start + 3 * DAY + 1.0):
            service.observe_shadow(agent, engine.state_map, {"light.target"})
        after = list(policy.schema.entities)
        if a in after or b not in after:
            raise AssertionError({"before": active, "after": after})
        return {
            "scenario": "C_concept_drift",
            "before": active,
            "challenger_after_drift": b,
            "after": after,
            "transition": f"{a} -> {b}",
            "automatic": True,
            "manual_rebuild_called": False,
        }


def run_all():
    report = {
        "version_contract": "0.12.0",
        "A": scenario_good_new_sensor(),
        "B": scenario_random_correlation(300),
        "C": scenario_concept_drift(),
    }
    assert report["A"]["promoted"]
    assert report["B"]["promotions"] == 0
    assert report["C"]["automatic"] and not report["C"]["manual_rebuild_called"]
    return report


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2, sort_keys=True))
