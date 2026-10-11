"""Paired production candidate observations with full model validation and features."""
import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
_scratch = tempfile.TemporaryDirectory(prefix='homemind-shadow-context-')
os.environ.setdefault('ADAPTIVE_AI_DATA', _scratch.name)
from context import TemporalHistory, ExplicitFeatureSchema
from context_engine import ContextEngine
from context_tournament import ContextTournament
import context_tournament_policy_candidate as candidates
import context_tournament_promotion as promotion
from observation_contract import FeatureSchemaV12, install_training_contract
from policy import MultiHorizonPolicy
from settings import DEFAULT_OPTIONS
from storage import Store

STAMP = 1791637200.


@contextmanager
def training_contract():
    contract = install_training_contract()
    try:
        yield
    finally:
        contract['restore']()


def state(eid, value, ts=STAMP, **attrs):
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return dict(entity_id=eid, state=str(value), attributes=attrs,
                last_changed=stamp, last_updated=stamp, context={})


class Fixture:
    def __init__(self, store, *, legacy=False, prefix='paired'):
        self.agent = dict(id=prefix, target_entity='light.room_0', target_property='power',
                          min_value=0, max_value=1, deadband=.5, input_entities=['*'],
                          enabled=True, mode='shadow', action_interval=.3)
        self.challengers = [f'sensor.challenger_{i}' for i in range(4)]
        active = [f'binary_sensor.room_{i}' for i in range(8)]
        self.states = {self.agent['target_entity']: state(self.agent['target_entity'], 'off')}
        registry = {self.agent['target_entity']: dict(area_id='room_0')}
        # Real room forecast workload with 45 presence sources and nine areas.
        for i in range(45):
            eid = f'binary_sensor.room_{i}'
            self.states[eid] = state(eid, 'on' if i == 1 else 'off', device_class='occupancy')
            registry[eid] = dict(area_id=f'room_{i % 9}', device_id=f'radar_{i}')
        for i, eid in enumerate(self.challengers):
            self.states[eid] = state(eid, 20 + i, device_class='temperature', unit_of_measurement='°C')
            registry[eid] = dict(area_id='room_0', device_id=eid)
        self.temporal = TemporalHistory(maxlen=96)
        for eid, value in self.states.items():
            for age in reversed(range(96)):
                self.temporal.add(eid, STAMP-age, copy.deepcopy(value))
        self.home = ContextEngine(DEFAULT_OPTIONS)
        self.home.configure(self.states, entities=registry)
        for eid, value in self.states.items():
            self.home.observe(eid, value, STAMP-1, learn=False)
        self.live = MultiHorizonPolicy(self.agent, self.states, registry, set(), context_engine=self.home)
        schema = ExplicitFeatureSchema if legacy else FeatureSchemaV12
        self.live.schema = schema(self.live.dims, active)
        self.engine = SimpleNamespace(state_map=self.states, models={prefix: self.live},
            entity_registry=registry, context=self.home, temporal_history=self.temporal,
            context_relevance={}, state_revision=1, runtime={prefix: {'last_prediction': 0}},
            lock=threading.RLock())
        self.service = ContextTournament(store, self.engine)
        self.tournament = dict(agent_id=prefix, active_features=active,
            challenger_features=self.challengers, schema_revision=1, previous_schema=[],
            feature_scores={eid: .5 for eid in self.challengers})
        self.service.state = lambda aid: copy.deepcopy(self.tournament)
        self.service.sync_agent = lambda *args, **kwargs: copy.deepcopy(self.tournament)
        self.service._eligible_entities = lambda *args: set()
        # Installation edits module promotion hooks; callers restore them on exit.
        candidates.install_policy_candidates(self.service)

    def observe(self, *, previous=False):
        factory = (lambda temporal, timestamp, **kw: temporal) if previous else candidates.shared_inference_temporal
        with patch.object(candidates, 'shared_inference_temporal', factory):
            return self.service.observe_shadow(self.agent, self.states, set())


def run_mode(mode, passes=24):
    with tempfile.TemporaryDirectory(prefix='shadow-context-bench-') as directory, \
         patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion), \
         patch.object(promotion, '_migrate_schema', promotion._migrate_schema), \
         training_contract(), patch('time.time', return_value=STAMP):
        fixture = Fixture(Store(Path(directory)/'test.db'))
        fixture.observe(previous=mode == 'previous')
        rows, feature_times, times = [], [], []
        features = MultiHorizonPolicy.features
        def record(policy, *args, **kwargs):
            started = time.perf_counter()
            result = features(policy, *args, **kwargs)
            feature_times.append((time.perf_counter()-started)*1000.)
            rows.append(result)
            return result
        with patch.object(fixture.home, 'forecast', wraps=fixture.home.forecast) as forecasts, \
             patch.object(candidates, '_verify_candidate_checksum', wraps=candidates._verify_candidate_checksum) as checks, \
             patch.object(MultiHorizonPolicy, 'features', record):
            for _ in range(passes):
                started = time.perf_counter()
                result = fixture.observe(previous=mode == 'previous')
                times.append((time.perf_counter()-started)*1000.)
                assert len(result['predictions']) == 4
            assert len(rows) == 4*passes
            assert checks.call_count == 4*passes  # Warm payloads still get full SHA.
            assert forecasts.call_count == (4 if mode == 'previous' else 1)*passes
        digest = hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return dict(observation_median_ms=statistics.median(times),
            features_total_ms=sum(feature_times), observation_total_ms=sum(times),
            forecast_calls=forecasts.call_count, checksum_calls=checks.call_count,
            every_features_labels_meta_sha256=digest)


def run(passes=24):
    series=[]
    for repeat in range(3):
        rows={}
        for mode in (('previous','current') if repeat % 2 == 0 else ('current','previous')):
            rows[mode]=run_mode(mode,passes)
        assert rows['previous']['every_features_labels_meta_sha256'] == rows['current']['every_features_labels_meta_sha256']
        series.append(dict(**rows, feature_speedup=rows['previous']['features_total_ms']/rows['current']['features_total_ms'],
                           observation_speedup=rows['previous']['observation_total_ms']/rows['current']['observation_total_ms']))
    return dict(synthetic=True, passes=passes, challengers=4, presence_sources=45, areas=9,
        full_validation_on_every_candidate=True, every_features_labels_meta_identical=True,
        series=series, limit='Real candidate observer, policy validation/features/prediction, room forecast and SQLite. No HA ingress or concurrent storage I/O; not a whole-installation latency forecast.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--passes',type=int,default=24)
    parser.add_argument('--compact',action='store_true')
    args=parser.parse_args()
    if args.passes < 1: parser.error('passes must be positive')
    print(json.dumps(run(args.passes),indent=None if args.compact else 2))
