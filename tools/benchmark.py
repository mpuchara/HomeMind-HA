"""Run on the actual Pi 4 for meaningful hardware results; no HA device writes."""
import json
import platform
import statistics
import time
from unittest.mock import patch, Mock
from simulate_anticipation import fixture
from engine import Engine
import engine as engine_module
import executor as executor_module
from telemetry import rss_mb, TELEMETRY
from policy import DiagonalLinUCB


def stats(samples):
    ordered=sorted(samples)
    return {'count':len(samples),'avg_ms':statistics.mean(samples),'p95_ms':ordered[int(len(samples)*.95)]}


def run(iterations=1000):
    agent,states,context,policy,example=fixture()
    features=example('arriving',time.time()-3)
    samples=[]
    for _ in range(iterations):
        start=time.perf_counter();policy.predict(features);samples.append((time.perf_counter()-start)*1000)
    dense=DiagonalLinUCB(128,list(range(31)))
    x={i:.5 for i in range(128)}
    for arm in range(31):dense.update(arm,x,1)
    analog=[]
    for _ in range(iterations):
        start=time.perf_counter();dense.choose(x);analog.append((time.perf_counter()-start)*1000)
    e=Engine();e.context=context;e.state_map=states;e.models[agent['id']]=policy
    store=Mock();store.get_agent_config.return_value=agent;store.list_agent_configs.return_value=[agent]
    store.meta_get.side_effect=lambda key,default=None:default
    events=[]
    try:
        with patch.object(engine_module,'STORE',store),patch.object(executor_module,'STORE',store), \
             patch.object(executor_module.HA,'service',side_effect=AssertionError('Shadow must never send a service')), \
             patch.object(executor_module.AUTOMATION_KNOWLEDGE,'hints_for_target',return_value=(set(),[])):
            for i in range(iterations):
                e.runtime.setdefault(agent['id'],{})['last_inference_ts']=0
                st={'entity_id':'sensor.lux','state':str(i%2),'attributes':{'unit_of_measurement':'lx'}}
                start=time.perf_counter()
                e.on_state_changed({'entity_id':'sensor.lux','new_state':st})
                e.process_agent(agent,e.state_map,{'sensor.lux'})
                events.append((time.perf_counter()-start)*1000)
        return {'platform':platform.platform(),'python':platform.python_version(),
                'rss_mb':rss_mb(),'binary_policy':stats(samples),'analog_31_arms_dense':stats(analog),
                'event_to_shadow_intent_mock_store':stats(events),
                'limits':'Microbenchmarks exclude WebSocket delivery, debounce, real SQLite and physical device/HA response time.',
                'pi4_thresholds_measured':False,
                'targets':{'idle_rss_mb':150,'preferred_training_rss_mb':400,'hard_training_rss_mb':500,
                           'idle_cpu_percent':5,'inference_p95_ms':10,'preferred_event_intent_ms':25,'hard_event_intent_ms':50}}
    finally:
        e.control_workers.shutdown();e.poll_worker.shutdown()


if __name__=='__main__':print(json.dumps(run(),indent=2))
