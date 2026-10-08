// Bound background/read requests so one stalled Supervisor/Ingress request cannot freeze
// the global app.js loadInFlight flag forever. Mutating requests are deliberately NOT
// aborted here: the server may already have accepted Train/Autonomous/etc., and aborting
// only the browser side would produce a false failure while the action keeps running.
(()=>{
  if(window.__adaptiveAiFetchTimeoutGuard)return;
  const nativeFetch=window.fetch.bind(window);
  window.fetch=(input,init={})=>{
    const {adaptiveAiTimeoutMs,...fetchInit}=init||{};
    const method=String(fetchInit.method||'GET').toUpperCase();
    if(method!=='GET'&&method!=='HEAD')return nativeFetch(input,fetchInit);
    const timeoutMs=adaptiveAiTimeoutMs===0?0:(Number.isFinite(Number(adaptiveAiTimeoutMs))?Math.max(1000,Number(adaptiveAiTimeoutMs)):12000);
    if(!timeoutMs)return nativeFetch(input,fetchInit);
    const controller=new AbortController();
    const upstream=fetchInit.signal;
    if(upstream){
      if(upstream.aborted)controller.abort(upstream.reason);
      else if(upstream.addEventListener)upstream.addEventListener('abort',()=>controller.abort(upstream.reason),{once:true});
    }
    const timer=setTimeout(()=>controller.abort(new DOMException(`Adaptive AI read timeout after ${timeoutMs} ms`,'TimeoutError')),timeoutMs);
    return nativeFetch(input,{...fetchInit,signal:controller.signal}).finally(()=>clearTimeout(timer));
  };
  window.__adaptiveAiFetchTimeoutGuard=true;
})();

function homeSources(h,names){
  const sources=h.source_details||[];
  const reasons={active:'Used for occupancy',missing_area:'No HA area',disabled_in_ha:'Disabled in HA',
    configuration_or_diagnostic:'Configuration / diagnostics',measurement_is_not_occupancy:'Distance / settings, not occupancy',
    non_presence_device_class:'Other measurement',binary_presence_on_same_device:'Binary presence on same device takes priority',
    actuator_or_electrical:'Actuator / electrical measurement'};
  const visible=[...sources].sort((a,b)=>(a.reason==='missing_area'?0:a.selected?1:2)-(b.reason==='missing_area'?0:b.selected?1:2)).slice(0,100);
  if(!visible.length)return '';
  return '<details class="home-source-details"><summary>Presence sources: '+num(h.mapped_sources)+' mapped · '+num(h.unmapped_sources)+' without area · sensor diagnostics</summary>'+
    '<p class="muted">Counts refer to entities, not physical devices. Radar distance and settings do not mean occupancy. Raw activity remains available to agent policies.</p>'+
    '<div class="home-source-table"><table><thead><tr><th>Entity</th><th>Area</th><th>Use / reason</th></tr></thead><tbody>'+
    visible.map(s=>'<tr><td>'+esc(s.name)+'<br><small>'+esc(s.entity_id)+'</small></td><td>'+esc(names[s.area_id]||s.area_id||'—')+(s.area_mapping_origin==='automation_radar_anchor'?'<br><small>Automation radar fallback</small>':'')+'</td><td>'+(s.available===false?'Unavailable · ':'')+esc(reasons[s.reason]||s.reason)+'</td></tr>').join('')+
    '</tbody></table></div>'+(h.source_details_total>visible.length?'<p class="muted">Showing '+visible.length+' of '+num(h.source_details_total)+' candidate channels.</p>':'')+'</details>';
}
function renderHome(status){
  const sourcePanel=$('#homePanel').querySelector('.home-source-details');
  const sourcesOpen=sourcePanel?.open||false, sourceScroll=sourcePanel?.querySelector('.home-source-table')?.scrollTop||0;
  const h=status.home_intelligence||{}, b=status.home_bootstrap||{}, t=status.telemetry||{};
  const inf=t.metrics?.inference||{}, latency=t.metrics?.event_to_decision||t.metrics?.event_to_intent||{};
  const candidateInf=t.metrics?.candidate_inference||{}, candidateLatency=t.metrics?.candidate_event_to_decision||{};
  const drift=status.drift_observer||{}, scheduler=status.inference_scheduler||{}, resync=status.state_resync||{};
  const pickMetric=(primary,fallback)=>{
    if(primary.recent_p95_ms!=null)return {value:primary.recent_p95_ms,source:'live',recent:true};
    if(fallback.recent_p95_ms!=null)return {value:fallback.recent_p95_ms,source:'candidate',recent:true};
    if(primary.p95_ms!=null)return {value:primary.p95_ms,source:'live',recent:false};
    if(fallback.p95_ms!=null)return {value:fallback.p95_ms,source:'candidate',recent:false};
    return {value:null,source:null,recent:false};
  };
  const pickRecentEventMetric=(primary,fallback)=>{
    if(Number(primary.recent_count||0)>0&&primary.recent_p95_ms!=null)return {value:primary.recent_p95_ms,source:'live',count:Number(primary.recent_count||0)};
    if(Number(fallback.recent_count||0)>0&&fallback.recent_p95_ms!=null)return {value:fallback.recent_p95_ms,source:'candidate',count:Number(fallback.recent_count||0)};
    return {value:null,source:null,count:0};
  };
  const inferenceMetric=pickMetric(inf,candidateInf), eventMetric=pickRecentEventMetric(latency,candidateLatency);
  const runtimeCost=name=>{
    const metric=pickMetric(t.metrics?.[name]||{},{});
    return metric.value==null?'waiting':num(metric.value,2)+' ms · '+(metric.recent?'last 60 s':'retained');
  };
  const inferenceP95=inferenceMetric.value, eventP95=eventMetric.value;
  const inferenceP95Label=inferenceMetric.source==='candidate'
    ?'Candidate inference p95 · '+(inferenceMetric.recent?'last 60 s':'retained')
    :inferenceMetric.source==='live'
      ?'inference p95 · '+(inferenceMetric.recent?'last 60 s':'retained')
      :'inference p95 · waiting for first sample';
  const eventP95Label=eventMetric.source==='candidate'
    ?'event → Candidate decision p95 · '+eventMetric.count+' samples · last 60 s'
    :eventMetric.source==='live'
      ?'event → decision p95 · '+eventMetric.count+' samples · last 60 s'
      :'event → decision p95 · waiting for relevant HA event · 0 samples';
  const running=['IMPORTING','TRAINING'].includes(b.state), names=h.area_names||{};
  const paths=(h.top_transitions||[]).slice(0,5);
  $('#homePanel').innerHTML='<div class="history-head"><div><b>Home Intelligence</b><span>Shared occupancy and trajectory model · live learning is automatic</span></div><strong>'+esc(b.state||'IDLE')+'</strong></div>'+
    '<div class="home-metrics">'+[
      [num(h.areas),'observed areas'],[num(h.edges),'transitions'],[num(h.updates),'online / bootstrap updates'],
      [num(h.unmapped_sources),'sources without area'],[t.rss_mb==null?'—':num(t.rss_mb,1)+' MB','RSS'],
      [t.cpu_percent_recent==null?'—':num(t.cpu_percent_recent,1)+'%','CPU · recent'],
      [inferenceP95==null?'—':num(inferenceP95,2)+' ms',inferenceP95Label],
      [eventP95==null?'—':num(eventP95,2)+' ms',eventP95Label]
    ].map(([v,label])=>'<div><b>'+v+'</b><span>'+label+'</span></div>').join('')+'</div>'+
    '<div class="history-meta"><span>Full agent p95: '+runtimeCost('wrapped_inference')+'</span><span>Context observation p95: '+runtimeCost('context_shadow_observation')+'</span><span>Sensor pool p95: '+runtimeCost('context_observed_pool')+'</span></div>'+
    '<div class="home-transitions">'+(paths.length?paths.map(p=>'<span>'+p.path.map(id=>esc(names[id]||id)).join(' → ')+' <b>'+pct(p.probability)+'</b></span>').join(''):'No observed area-to-area transitions yet. Existing mapped presence/activity sensors learn the live map automatically as state changes arrive.')+'</div>'+
    '<div class="history-meta"><span>Graph half-life '+num(h.half_life_days)+' days</span><span>Policy half-life '+num(status.options?.policy_half_life_days||30)+' days</span><span>Heavy job: '+esc(status.heavy_job||'idle')+'</span><span>Inference count '+num(inf.count)+' live · '+num(candidateInf.count)+' Candidate</span><span>Event/timer passes '+num(scheduler.event_passes||0)+' / '+num(scheduler.timer_passes||0)+'</span><span>Drift observer '+num(drift.runs||0)+' runs · '+num(drift.pending||0)+' pending · max '+num(drift.max_run_ms||0,1)+' ms</span><span>State resync '+num(resync.last_changed_entities||0)+' changed · '+num(resync.last_duration_ms||0,1)+' ms · max '+num(resync.max_duration_ms||0,1)+' ms</span></div>'+
    (running?'<div class="bar history-bar"><i style="width:'+Math.round((b.progress||0)*100)+'%"></i></div><p>'+num(b.rows)+' rows · '+num(b.rows_per_second)+' rows/s · '+esc(duration(b.eta_seconds)||'ETA pending')+'</p>':'')+
    (b.error?'<p role="alert">'+esc(b.error)+'</p><p class="muted">Bootstrap failed; the current live model continues running.</p>':'')+
    homeSources(h,names)+
    '<div class="actions"><button class="ghost" onclick="bootstrapHome()" '+(status.heavy_job?'disabled':'')+'>Backfill Home Model from history</button><button class="ghost" onclick="cancelHomeBootstrap()" '+(!running?'disabled':'')+'>Cancel backfill</button></div>'+
    '<p class="muted"><b>Automatic:</b> live occupancy, dwell and room-to-room trajectory learning from mapped sensors. <b>Optional:</b> historical backfill imports Recorder history once to give the model useful past trajectories immediately. Restart does not replay history automatically.</p>';
  const updatedSources=$('#homePanel').querySelector('.home-source-details');
  if(updatedSources){updatedSources.open=sourcesOpen;updatedSources.querySelector('.home-source-table').scrollTop=sourceScroll;}
}
function homeAgentDetails(a){
  const rt=a.runtime||{}, f=rt.home_forecast||{};
  return '<div class="detail"><b>Target area:</b> '+esc(f.area_id||'unmapped')+
    '<br><b>Occupancy now / within 1 / 3 / 5 seconds:</b> '+[f.occupancy_now,f.occupancy_in_1s,f.occupancy_in_3s,f.occupancy_in_5s].map(pct).join(' / ')+
    '<br><b>Trajectory confidence:</b> '+pct(f.trajectory_confidence)+' · sensing '+(f.known?'available':'unknown')+
    '<br><b>Trajectory event sources:</b> '+esc((rt.trajectory_event_sources||[]).join(', ')||'No mapped sources on learned routes yet')+
    '<br><b>Intent:</b> '+esc(rt.intent?.status||'—')+' · '+esc(rt.intent?.reason||'No intent yet')+
    (rt.automation_scan_warning?'<br><b>Automation scan warning:</b> '+esc(rt.automation_scan_warning)+'. Control uses known target mappings; unreadable automations without a saved mapping cannot be matched.':'')+
    '<br><b>Reward components:</b> '+esc(Object.entries(rt.reward_components||{}).filter(([,v])=>v!==0).map(([k,v])=>k+' '+Number(v).toFixed(2)).join(' · ')||'—')+
    '<br><b>Learned behavior:</b> '+esc(rt.behavior_summary||'Train a compatible policy to see its learned contributors.')+
    (a.training_state==='qualified'?'<br><a href="api/agents/'+encodeURIComponent(a.id)+'/export" download="homemind-'+esc(a.id)+'-inference.json">Export inference state</a>':'')+'</div>';
}
async function bootstrapHome(){
  try{await api('api/home/bootstrap',{method:'POST',body:JSON.stringify({import_recorder:true})});await load();}
  catch(e){alert(e.message);}
}
async function cancelHomeBootstrap(){
  try{await api('api/home/cancel',{method:'POST',body:'{}'});await load();}
  catch(e){alert(e.message);}
}
