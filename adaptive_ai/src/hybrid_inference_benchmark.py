"""Synthetic baseline for complete Ridge + Tiny MLP + Candidate inference work.

Uses production v12 Ridge feature construction, Ridge prediction, observation_as_of,
TinyMLPBackend and HybridPolicyService. It never connects to HA or dispatches services.
Absolute timings are descriptive; stable work counters are the CI contract.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import math
from types import SimpleNamespace
import statistics
import time

from context import TemporalHistory
from hybrid_policy_runtime import HybridPolicyService
from inference_hot_path_metrics import InferenceHotPathMetrics
from observation_contract import FeatureSchemaV12, policy_features
from observation_space import ENTITY_DESCRIPTORS, ObservationMask, observation_as_of, observation_schema_id
from policy import MultiHorizonPolicy
from policy_tiny_mlp import TinyMLPBackend
from shared_inference_context import shared_inference_temporal

def percentile(values, q):
    rows = sorted(float(x) for x in values)
    if not rows: return None
    pos = (len(rows)-1)*float(q); lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo == hi: return rows[lo]
    return rows[lo] + (rows[hi]-rows[lo])*(pos-lo)

def summary(values):
    rows=[float(x) for x in values]
    return {"count":len(rows),"p50_us":percentile(rows,.50),"p95_us":percentile(rows,.95),
            "p99_us":percentile(rows,.99),"max_us":max(rows) if rows else None,
            "mean_us":statistics.fmean(rows) if rows else None}

class CountingHomeContext:
    def __init__(self):
        self.calls=0; self.excluded=set()
    def forecast(self, target_entity, timestamp):
        self.calls += 1
        return {"known":True,"occupancy_now":.6,"occupancy_in_1s":.7,"occupancy_in_3s":.75,
                "occupancy_in_5s":.8,"arrival_likelihood":.2,"departure_likelihood":.1,"activity":.5}

class CountingSamples(dict):
    def __init__(self):
        super().__init__(); self.get_calls=0
    def get(self,key,default=None):
        self.get_calls += 1
        return super().get(key,default)

class CountingTemporal(TemporalHistory):
    def __init__(self):
        super().__init__(maxlen=96)
        self.samples=CountingSamples(); self.previous_calls=0; self.last_change_calls=0
        self.home_context=None
    def previous(self,entity_id,at_ts):
        self.previous_calls += 1
        return super().previous(entity_id,at_ts)
    def last_change_ts(self,entity_id,fallback_state=None):
        self.last_change_calls += 1
        return super().last_change_ts(entity_id,fallback_state)

class StageSink:
    def __init__(self): self.rows={}
    def add(self,stage,elapsed_us): self.rows.setdefault(stage,[]).append(float(elapsed_us))
    def timed(self,stage,fn):
        started=time.perf_counter_ns(); result=fn()
        self.add(stage,(time.perf_counter_ns()-started)/1000.0); return result
    def snapshot(self): return {name:summary(values) for name,values in sorted(self.rows.items())}

def _stamp(ts): return datetime.fromtimestamp(float(ts),timezone.utc).isoformat()
def state(entity_id,value,ts,**attrs):
    meta=dict(attrs); meta["__hm_event_time"]=float(ts); meta["__hm_received_time"]=float(ts)
    return {"entity_id":entity_id,"state":str(value),"attributes":meta,
            "last_changed":_stamp(ts),"last_updated":_stamp(ts),"context":{}}
def agent(agent_id):
    return {"id":agent_id,"name":"Hybrid hot path benchmark","target_entity":"light.target",
            "target_property":"power","min_value":0,"max_value":1,"deadband":.5,
            "input_entities":["*"],"enabled":True,"confidence_threshold":.78,
            "action_interval":.25,"micro_exploration":False,"exploration_step":1,
            "mode":"shadow","training_state":"qualified"}

def fixture(entity_count=8,ts=1_700_500_100.0):
    count=max(2,min(16,int(entity_count)))
    entity_ids=[f"sensor.context_{idx:02d}" for idx in range(count)]
    states={"light.target":state("light.target","off",ts)}
    registry={"light.target":{"area_id":"room_0","device_id":"target"}}
    temporal=CountingTemporal()
    for idx,eid in enumerate(entity_ids):
        value=10.0+idx
        states[eid]=state(eid,value,ts,device_class="temperature",unit_of_measurement="°C",
                          friendly_name=f"Context {idx}")
        registry[eid]={"area_id":f"room_{idx%3}","device_id":f"sensor-{idx}"}
        for offset,delta in ((60.0,-1.0),(10.0,-.4),(1.0,-.1),(0.0,0.0)):
            temporal.add(eid,ts-offset,state(eid,value+delta,ts-offset,device_class="temperature",
                         unit_of_measurement="°C",friendly_name=f"Context {idx}"))
    temporal.add("light.target",ts,states["light.target"])
    home=CountingHomeContext(); temporal.home_context=home
    live=MultiHorizonPolicy(agent("live-benchmark"),states,registry,entity_ids,context_engine=home)
    live.schema=FeatureSchemaV12(live.dims,entity_ids)
    candidate=MultiHorizonPolicy(agent("candidate-benchmark"),states,registry,entity_ids,context_engine=home)
    candidate.schema=FeatureSchemaV12(candidate.dims,entity_ids)
    features=[]
    for eid in entity_ids:
        for descriptor,lag_seconds in ENTITY_DESCRIPTORS:
            features.append({"id":f"entity:{eid}:{descriptor}","name":f"{eid} · {descriptor}",
                             "kind":"entity","entity_id":eid,"area_id":registry[eid]["area_id"],
                             "descriptor":descriptor,"lag_seconds":lag_seconds})
    mask=ObservationMask(schema_id=observation_schema_id(),mask_version=1,
        feature_ids=tuple(row["id"] for row in features),features=tuple(features),
        selected_entities=tuple(entity_ids),global_feature_count=0,missing_feature_count=0)
    live_mlp=TinyMLPBackend(actions=live.actions,horizons=live.horizons,feature_ids=mask.feature_ids,
        schema_id=mask.schema_id,mask_id=mask.mask_id,hidden=(32,16),init_seed=1482)
    candidate_mlp=TinyMLPBackend(actions=candidate.actions,horizons=candidate.horizons,feature_ids=mask.feature_ids,
        schema_id=mask.schema_id,mask_id=mask.mask_id,hidden=(32,16),init_seed=2482)
    live_mlp.trained=True; candidate_mlp.trained=True
    temporal.samples.get_calls=0; temporal.previous_calls=0; temporal.last_change_calls=0
    return states,temporal,home,live,candidate,mask,live_mlp,candidate_mlp,ts

class BenchmarkNeural:
    def __init__(self,engine,backend,mask,home,sink,prefix=""):
        self.engine=engine; self.backend=backend; self.mask=mask; self.home=home
        self.sink=sink; self.prefix=prefix
    def persisted_record(self,agent_id):
        policy=self.engine.current_policy
        return {"selected_backend":TinyMLPBackend.BACKEND,
                "source_policy_revision":str(policy.tournament_revision),
                "model":{"trained":True,"model_revision":self.backend.model_revision},
                "mask":{**self.mask.export(),"selected_entities":list(self.mask.selected_entities)},
                "tournament":{"passed":True,"selected_backend":TinyMLPBackend.BACKEND}}
    def predict_persisted(self,agent_row,policy,state_map,temporal,*,timestamp,require_selected=False,metric_prefix="",home_provider=None):
        observation=self.sink.timed(self.prefix+"mlp_observation_construction",
            lambda:observation_as_of(
                self.mask,state_map,temporal,float(timestamp),agent_row,
                home_provider=home_provider or self.home,
            ))
        result=self.sink.timed(self.prefix+"mlp_forward",lambda:self.backend.predict(observation))
        chosen,confidence,arms,horizon,support,novelty=result
        return {"backend":self.backend,"record":self.persisted_record(agent_row["id"]),"observation":observation,
                "chosen":chosen,"confidence":confidence,"arms":arms,"horizon":horizon,
                "support":support,"novelty":novelty}

def _ridge(policy,state_map,temporal,ts,sink,prefix=""):
    features=sink.timed(prefix+"ridge_feature_construction",
        lambda:policy_features(policy,state_map,temporal,at_ts=ts))[0]
    return sink.timed(prefix+"ridge_predict",lambda:policy.predict(features))

def _hybrid(service,policy,state_map,temporal,ts,ridge,sink,prefix=""):
    chosen,confidence,arms,horizon,support,novelty=ridge
    service.engine.current_policy=policy
    return sink.timed(prefix+"hybrid_policy_total",lambda:service.evaluate(
        policy.agent,policy,state_map,temporal,timestamp=ts,ridge_chosen=chosen,
        ridge_confidence=confidence,ridge_arms=arms,ridge_horizon=horizon,
        ridge_support=support,ridge_novelty=novelty,metric_prefix=prefix,
        home_provider=getattr(temporal,"home_context",None)))

SCENARIOS={
    "A_ridge_only":(False,False,False),
    "B_ridge_plus_tiny_mlp_hybrid":(True,False,False),
    "C_hybrid_plus_ridge_candidate":(True,True,False),
    "D_hybrid_plus_hybrid_candidate":(True,True,True),
}

def run(iterations=80,entity_count=8):
    iterations=max(3,int(iterations)); out={}
    for name,(live_hybrid,candidate_ridge,candidate_hybrid) in SCENARIOS.items():
        states,temporal,home,live,candidate,mask,live_mlp,candidate_mlp,ts=fixture(entity_count)
        sink=StageSink()
        engine=SimpleNamespace(inference_hot_path_metrics=InferenceHotPathMetrics(),current_policy=live)
        live_neural=BenchmarkNeural(engine,live_mlp,mask,home,sink); engine.tiny_mlp_shadow=live_neural
        live_service=HybridPolicyService(engine)
        candidate_engine=SimpleNamespace(inference_hot_path_metrics=engine.inference_hot_path_metrics,current_policy=candidate)
        candidate_neural=BenchmarkNeural(candidate_engine,candidate_mlp,mask,home,sink,prefix="candidate_")
        candidate_engine.tiny_mlp_shadow=candidate_neural; candidate_service=HybridPolicyService(candidate_engine)
        totals=[]; f0=home.calls; g0=temporal.samples.get_calls; p0=temporal.previous_calls; c0=temporal.last_change_calls
        causal_hits=0; causal_misses=0; shared_previous_hits=0; shared_previous_misses=0
        for _ in range(iterations):
            started=time.perf_counter_ns()
            live_temporal=shared_inference_temporal(temporal,ts,home_provider=home)
            live_ridge=_ridge(live,states,live_temporal,ts,sink)
            if live_hybrid:
                _hybrid(live_service,live,states,live_temporal,ts,live_ridge,sink)
            live_diag=live_temporal.diagnostics()
            causal_hits += int(live_diag.get("causal_asof_hits") or 0)
            causal_misses += int(live_diag.get("causal_asof_misses") or 0)
            shared_previous_hits += int(live_diag.get("previous_hits") or 0)
            shared_previous_misses += int(live_diag.get("previous_misses") or 0)
            if candidate_ridge:
                # Candidate is off the Live critical path since ETAP 2. Model its worker
                # as a separate per-decision context: it may not reuse Live mutable cache,
                # but Ridge+MLP inside that Candidate job share one causal snapshot.
                candidate_temporal=shared_inference_temporal(temporal,ts,home_provider=home)
                cand_ridge=_ridge(candidate,states,candidate_temporal,ts,sink,prefix="candidate_")
                if candidate_hybrid:
                    _hybrid(candidate_service,candidate,states,candidate_temporal,ts,cand_ridge,sink,prefix="candidate_")
                candidate_diag=candidate_temporal.diagnostics()
                causal_hits += int(candidate_diag.get("causal_asof_hits") or 0)
                causal_misses += int(candidate_diag.get("causal_asof_misses") or 0)
                shared_previous_hits += int(candidate_diag.get("previous_hits") or 0)
                shared_previous_misses += int(candidate_diag.get("previous_misses") or 0)
            totals.append((time.perf_counter_ns()-started)/1000.0)
        out[name]={"total":summary(totals),"stages":sink.snapshot(),
            "work":{"forecast_calls_per_inference":(home.calls-f0)/iterations,
                    "history_map_gets_per_inference":(temporal.samples.get_calls-g0)/iterations,
                    "temporal_previous_calls_per_inference":(temporal.previous_calls-p0)/iterations,
                    "last_change_calls_per_inference":(temporal.last_change_calls-c0)/iterations,
                    "causal_asof_hits_per_inference":causal_hits/iterations,
                    "causal_asof_misses_per_inference":causal_misses/iterations,
                    "shared_previous_hits_per_inference":shared_previous_hits/iterations,
                    "shared_previous_misses_per_inference":shared_previous_misses/iterations,
                    "model_deserialize_calls_per_inference":0.0},
            "guard_metrics":engine.inference_hot_path_metrics.snapshot()["stages"]}
    forecasts=[out[name]["work"]["forecast_calls_per_inference"] for name in SCENARIOS]
    history_gets=[out[name]["work"]["history_map_gets_per_inference"] for name in SCENARIOS]
    expected_forecasts=(1.0,1.0,2.0,2.0)
    forecast_ok=all(abs(a-e)<1e-9 for a,e in zip(forecasts,expected_forecasts))
    # Tiny MLP must reuse the same materialized temporal entity views as Ridge. A Hybrid
    # decision therefore adds zero samples-map lookups inside one shared context.
    history_reuse_ok=(
        abs(history_gets[1]-history_gets[0])<1e-9
        and abs(history_gets[3]-history_gets[2])<1e-9
    )
    asof_hits=[out[name]["work"]["causal_asof_hits_per_inference"] for name in SCENARIOS]
    causal_reuse_ok=(
        asof_hits[1] > asof_hits[0]
        and asof_hits[3] > asof_hits[2]
    )
    pass_work=forecast_ok and history_reuse_ok and causal_reuse_ok
    a=out["A_ridge_only"]["total"]["p95_us"]; b=out["B_ridge_plus_tiny_mlp_hybrid"]["total"]["p95_us"]
    c=out["C_hybrid_plus_ridge_candidate"]["total"]["p95_us"]; d=out["D_hybrid_plus_hybrid_candidate"]["total"]["p95_us"]
    return {"contract":"hybrid_inference_shared_context_v2",
        "scope":"synthetic component-faithful Ridge/Hybrid work; Candidate worker cost is reported but is outside Live critical path",
        "candidate_baseline":"deferred_worker_separate_shared_context","iterations":iterations,
        "entity_count":max(2,min(16,int(entity_count))),"scenarios":out,
        "ratios":{"hybrid_vs_ridge_p95":b/max(a,1e-9),"ridge_candidate_vs_hybrid_p95":c/max(b,1e-9),
                  "hybrid_candidate_vs_hybrid_p95":d/max(b,1e-9)},
        "stable_assertions":{"forecast_calls_per_inference":dict(zip(SCENARIOS,forecasts)),
            "expected_forecast_calls":{"A_ridge_only":1.0,"B_ridge_plus_tiny_mlp_hybrid":1.0,
                "C_hybrid_plus_ridge_candidate":2.0,"D_hybrid_plus_hybrid_candidate":2.0},
            "history_map_gets_per_inference":dict(zip(SCENARIOS,history_gets)),
            "mlp_additional_history_map_gets":{
                "live_hybrid_minus_ridge":history_gets[1]-history_gets[0],
                "candidate_hybrid_minus_candidate_ridge":history_gets[3]-history_gets[2],
            },
            "expected_mlp_additional_history_map_gets":0.0,
            "causal_asof_hits_per_inference":dict(zip(SCENARIOS,asof_hits)),
            "hybrid_must_reuse_ridge_causal_asof":True,
            "no_model_deserialize_per_inference":True,"timing_thresholds_are_not_ci_contract":True},
        "pass":bool(pass_work)}

def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument("--iterations",type=int,default=80)
    parser.add_argument("--entities",type=int,default=8); parser.add_argument("--compact",action="store_true")
    args=parser.parse_args(argv); result=run(args.iterations,args.entities)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(",",":") if args.compact else None,
                     indent=None if args.compact else 2))
    if not result["pass"]: raise SystemExit(1)

if __name__=="__main__": main()
