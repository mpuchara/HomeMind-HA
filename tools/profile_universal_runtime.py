"""Opt-in HOST diagnostics of the existing composed harness, never runtime code.

Cold probes are retained BEFORE the existing warmup reset. Optional bounded
stack sampling records wall occupancy, NOT per-function CPU attribution.
Profiled runs are diagnostic, not acceptance latency measurements.
Raw bounded probes are retained losslessly in gzip; summaries are small JSON.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import platform
import sys
import threading
import time
import weakref
from unittest.mock import patch

import benchmark_runtime_scaling as harness
from runtime_scaling_probe import Probe,TimedLock


def quantiles(values):
    values=sorted(values)
    return dict(count=len(values),**{f'p{p}_ms':values[int((len(values)-1)*p/100)] if values else None
                                  for p in (50,95,99)},max_ms=max(values) if values else None)


def summarize_probe(probe):
    return {name:dict(count=row['count'],discarded_samples=row['dropped'],
        wall=quantiles([s[0] for s in row['samples']]),
        thread_cpu=quantiles([s[1] for s in row['samples'] if s[1] is not None]),
        sampled_wall_sum_ms=sum(s[0] for s in row['samples']),aggregate=row.get('aggregate'))
        for name,row in probe.get('operations',{}).items()}


class StackSampler:
    """Bounded 20 Hz wall-stack observations; no locals/arguments/HA data."""
    def __init__(self,enabled=False):
        self.enabled=enabled;self.stop=threading.Event();self.rows=Counter();self.samples=0;self.overflow=0;self.cpu=0.
    def run(self):
        started=time.thread_time()
        while not self.stop.wait(.05):
            names={t.ident:t.name for t in threading.enumerate()}
            for ident,frame in sys._current_frames().items():
                if ident==threading.get_ident():continue
                path=[]
                for _ in range(18):
                    if frame is None:break
                    path.append((Path(frame.f_code.co_filename).name,frame.f_code.co_name,frame.f_lineno))
                    frame=frame.f_back
                key=(names.get(ident,'unknown'),tuple(path))
                self.samples+=1
                if key in self.rows or len(self.rows)<2048:self.rows[key]+=1
                else:self.overflow+=1
        self.cpu=(time.thread_time()-started)*1000
    def __enter__(self):
        if self.enabled:
            self.thread=threading.Thread(target=self.run,name='host-stack-sampler',daemon=True);self.thread.start()
        return self
    def __exit__(self,*args):
        if self.enabled:self.stop.set();self.thread.join(5)
    def summary(self):
        return dict(enabled=self.enabled,sampling_interval_ms=50,samples=self.samples,overflow=self.overflow,
                    sampler_thread_cpu_ms=self.cpu,meaning='wall occupancy, not CPU attribution; includes setup',
                    stacks=[dict(thread=k[0],stack=k[1],ticks=n) for k,n in self.rows.most_common()])


def run(args):
    cold={}
    class PhaseProbe(Probe):
        def __init__(self):super().__init__(capacity=256);self.totals={}
        def append(self,name,row):
            with self.lock:
                self.rows[name].append(row);self.counts[name]+=1
                total=self.totals.setdefault(name,[0.,0.,0.,0])
                total[0]+=row[0];total[1]+=row[1] or 0.;total[2]=max(total[2],row[0])
                total[3]+=int(row[1] is not None)
        def snapshot(self):
            with self.lock:
                return dict(capacity_per_operation=self.capacity,inclusive=True,process_cpu_additive=False,
                    operations={name:dict(count=self.counts[name],samples=list(rows),
                        dropped=self.counts[name]-len(rows),aggregate=(dict(wall_sum_ms=self.totals[name][0],
                            thread_cpu_sum_ms=self.totals[name][1] if self.totals[name][3] else None,
                            wall_max_ms=self.totals[name][2])
                            if name in self.totals else None)) for name,rows in self.rows.items()})
        def reset(self):
            cold.update(self.snapshot())
            with self.lock:self.rows.clear();self.counts.clear();self.totals.clear()
        def install(self,stack,engine,store):
            super().install(stack,engine,store)
            submit=engine.control_workers.submit
            def admission(fn,*a,**kw):
                receipts=a[3] if len(a)>3 and isinstance(a[3],dict) else {}
                first=min((v for v in receipts.values() if v is not None),default=None)
                if first is not None:
                    self.append('event_receipt_to_target_admission',((time.perf_counter()-first)*1000,None,None))
                def entered():
                    if first is not None:
                        self.append('event_receipt_to_target_entry',((time.perf_counter()-first)*1000,None,None))
                    return fn(*a,**kw)
                return submit(entered)
            stack.enter_context(patch.object(engine.control_workers,'submit',admission))
            if args.schema_once:
                import tiny_mlp_shadow
                original=tiny_mlp_shadow.ensure_tables
                # Establish the real schema before suppressing duplicate checks.
                # This time remains in total_fixture_* (not moved out of the run).
                original(store);ensured=weakref.WeakKeyDictionary({store:True});mutex=threading.Lock()
                def once(target_store):
                    with mutex:
                        if target_store not in ensured:original(target_store);ensured[target_store]=True
                stack.enter_context(patch.object(tiny_mlp_shadow,'ensure_tables',once))
            from universal_history_collector import HistoricalTrainingCollector
            from device_agents import DeviceAgentService
            import tiny_mlp_shadow
            shadow=getattr(engine,'tiny_mlp_shadow',None)
            if shadow is not None:
                stack.enter_context(patch.object(shadow,'lock',TimedLock(shadow.lock,self,'tiny_shadow_lock')))
            for method,name in (('_active_agents_for_changes','dependency_routing'),):
                if hasattr(engine,method):self.wrap(stack,engine,method,name)
            for method,name in (('resolved_registry','resolved_registry'),('forecast','context_forecast')):
                if hasattr(engine.context,method):self.wrap(stack,engine.context,method,name)
            live=getattr(engine,'live_card_updates',None)
            if live is not None:self.wrap(stack,live,'publish','live_card_publish')
            self.wrap(stack,tiny_mlp_shadow,'ensure_tables','shadow_schema_check')
            for owner,method,name in (
                (engine,'_refresh_agent_index','agent_index_refresh'),
                (DeviceAgentService,'descriptor','device_descriptor'),
                (DeviceAgentService,'_registry','device_registry_snapshot'),
                (HistoricalTrainingCollector,'decision','collector_decision_capture'),
                (HistoricalTrainingCollector,'mask','collector_mask_capture'),
                (HistoricalTrainingCollector,'_write','collector_batch_write'),
                (HistoricalTrainingCollector,'_journal_rows','collector_featurejournal_write'),
            ):self.wrap(stack,owner,method,name)
    # A research ablation on a disposable fixed-schema fixture, NOT a production
    # schema cache. No external DDL/replacement is permitted in this experiment.
    from contextlib import ExitStack
    fixture_wall=time.perf_counter();fixture_cpu=time.process_time()
    with ExitStack() as stack,StackSampler(args.stack_sampling) as sampler:
        stack.enter_context(patch.object(harness,'Probe',PhaseProbe))
        raw=harness.run(args.source_root,args.devices,args.events,entities=args.sensors+args.devices,
            warmup=args.warmup,probe=True,candidates=args.candidates,training=args.training,
            correct=args.correct,collector=args.collector,sensor_count=args.sensors,event_timeout=args.event_timeout)
    fixture_wall=(time.perf_counter()-fixture_wall)*1000;fixture_cpu=(time.process_time()-fixture_cpu)*1000
    raw['cold_probe']=cold
    raw['stack_sampling']=sampler.summary()
    source_manifest=[(str(p.relative_to(args.source_root)),hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest())
                     for p in sorted((args.source_root/'adaptive_ai/src').rglob('*.py'))]
    raw['runtime_python_source_manifest']=source_manifest
    source_digest=hashlib.sha256(json.dumps(source_manifest,separators=(',',':')).encode()).hexdigest()
    encoded=(json.dumps(raw,separators=(',',':'))+'\n').encode()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    archive=args.output.with_suffix('.raw.json.gz')
    if archive.exists() or args.output.exists():raise ValueError('choose new output paths; results are immutable')
    with gzip.open(archive,'wb') as stream:stream.write(encoded)
    summary=dict(scope='WSL HOST diagnostic, extra probe overhead; no HA server/network/Pi measurement',
        runtime_revision=args.runtime_revision,runtime_revision_is_baseline_reference=True,
        actual_runtime_python_manifest_sha256=source_digest,
        profiler_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        harness_sha256=hashlib.sha256(Path(harness.__file__).read_bytes()).hexdigest(),
        source_root=str(args.source_root),sensors=args.sensors,devices=args.devices,events=args.events,warmup=args.warmup,
        collector=args.collector,candidates=args.candidates,training=args.training,correct=args.correct,
        research_schema_once=args.schema_once,
        observation_timeout_seconds=args.event_timeout,
        warmup_pipeline=quantiles(raw['warmup_pipeline_samples_ms']),
        warmup_steps=[quantiles(raw['warmup_pipeline_samples_ms'][i*args.devices:(i+1)*args.devices]) for i in range(args.warmup)],
        event_to_intent=quantiles(raw['telemetry_raw_ms'].get('event_to_intent',[])),
        cold_probe=summarize_probe(cold),steady_probe=summarize_probe(raw['probe']),
        stack_sampling={**raw['stack_sampling'],'unique_stacks':len(raw['stack_sampling']['stacks']),
                        'stacks':raw['stack_sampling']['stacks'][:100]},
        cpu_ms=raw['process_cpu_ms'],rss_mb=raw['rss_mb'],peak_rss_mb=raw['peak_rss_mb'],
        total_fixture_wall_ms=fixture_wall,total_fixture_cpu_ms=fixture_cpu,
        collector_diagnostics=raw['collector'],background_work=raw['background_work'],
        input_sha256=raw['input_sha256'],decisions_sha256=raw['decisions_sha256'],ha_calls=raw['ha_calls'],
        raw_archive=archive.name,raw_sha256=hashlib.sha256(encoded).hexdigest(),raw_bytes=len(encoded),
        python=sys.version,platform=platform.platform(),
        limits='Inclusive spans are not additive. Probe capacity 256/operation; count/discarded_samples explicit. Stacks measure wall occupancy, not CPU time. Harness starts after synthetic model fitting; websocket decode and actual HA are not exercised here. Collector counters include warmup/shutdown, steady probes reset after warmup.')
    args.output.write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--runtime-revision',default='unrecorded')
    p.add_argument('--sensors',type=int,default=5);p.add_argument('--devices',type=int,default=1)
    p.add_argument('--events',type=int,default=4);p.add_argument('--warmup',type=int,default=2)
    p.add_argument('--stack-sampling',action='store_true')
    p.add_argument('--schema-once',action='store_true',help='Research only: skip duplicate schema ensures on the disposable fixed-schema fixture')
    p.add_argument('--event-timeout',type=float,default=30.)
    for flag in ('collector','candidates','training','correct'):p.add_argument('--'+flag,action='store_true')
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if not (5<=args.sensors<=1000 and 1<=args.devices<=200 and 2<=args.events<=100 and
            1<=args.warmup<=20 and 30<=args.event_timeout<=120):
        p.error('bounded diagnostic cardinalities required')
    if args.output.exists() or args.output.with_suffix('.raw.json.gz').exists():p.error('output already exists')
    result=run(args)
    print(json.dumps({k:result[k] for k in ('sensors','devices','warmup_pipeline','event_to_intent','ha_calls')}))


if __name__=='__main__':main()
