let entities=[];
let lastHistory={};
let lastAgents=[];
let lastStatus={};
let loadInFlight=false;
window.__adaptiveAiRuntimeReady=null; // unknown until status answers; server gates half-built runtime
const openAgentDetails=new Set(JSON.parse(localStorage.getItem('adaptiveAiOpenAgentDetails')||'[]').map(String));
const $=s=>document.querySelector(s);
const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});if(!r.ok)throw new Error(await r.text());return r.status===204?null:r.json()};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=v=>Math.round((Number(v)||0)*100)+'%';
const num=(v,d=0)=>Number(v||0).toLocaleString(undefined,{maximumFractionDigits:d});
function duration(v){if(v==null)return null;const s=Number(v);if(!Number.isFinite(s)||s<0)return null;if(s<20)return '< 1 min';if(s<90)return '≈ 1 min';const m=Math.round(s/60);if(m<60)return `≈ ${m} min`;const h=Math.floor(m/60),rm=m%60;return rm?`≈ ${h} h ${rm} min`:`≈ ${h} h`;}
function discoveryPending(h){return Boolean(h?.discovery_job_active&&!h?.discovery_classified);}
function discoveryProgressText(h){
  const done=Number(h?.chunk_done||0),total=Number(h?.chunk_total||0);
  return total?`${done}/${total} Recorder chunks`:'Recorder scan starting';
}
function updateRescanButton(h){
  const b=$('#rescanBtn');if(!b)return;
  const running=Boolean(h?.discovery_job_active);
  b.disabled=running;
  b.textContent=running?`Scanning… ${Number(h?.chunk_done||0)}/${Number(h?.chunk_total||0)||'…'}`:'Rescan devices';
}
function renderOverview(status){
  const h=status.history||{},pending=discoveryPending(h);
  const targetText=pending?`${discoveryProgressText(h)} · activity pending`:`${h.active||0} active / ${h.eligible||h.controllable||0} eligible`;
  $('#overview').innerHTML=`
    <div class="metric"><b>${status.state_count||0}</b><span>HA entities in context</span></div>
    <div class="metric"><b>${status.agent_count||0}</b><span>agents · ${targetText}</span></div>
    <div class="metric"><b>${pct(status.average_confidence)}</b><span>average policy confidence</span></div>
    <div class="metric"><b>${num(status.historical_experience_count||0)}</b><span>predictive RL experiences</span></div>`;
}
async function load(){
  if(loadInFlight)return;
  loadInFlight=true;
  try{
  const wasReady=window.__adaptiveAiRuntimeReady===true;
  // Once runtime readiness has been established, start the lightweight agent/event reads
  // immediately. A slow diagnostic status response must not delay Current/Desired cards.
  const earlyAgents=wasReady?api('api/agents'):null;
  const earlyEvents=wasReady?api('api/events?limit=60'):null;
  let status;
  try{
    status=await api('api/status',{adaptiveAiTimeoutMs:3500});lastStatus=status;
    window.__adaptiveAiRuntimeReady=Boolean(status.startup?.ready);
    const c=$('#connection');
    const rt=status.realtime||{};
    c.textContent=status.ha_connected?`HA connected · ${status.state_count} entities${rt.connected?' · realtime':' · REST fallback'}`:`HA disconnected · ${status.ha_error||status.engine_error||'unknown error'}`;
    c.className='pill'+(status.ha_connected?' good':'');
    lastHistory=status.history||{};renderOverview(status);renderHistory(lastHistory,status);renderHome(status);
    // HTTP is intentionally available before runtime composition. Do not fan out to
    // /agents, /events or live card reads until Engine.start() and the final adapters are
    // actually ready; otherwise a slow migration looks like missing Shadow predictions.
    if(!window.__adaptiveAiRuntimeReady)return;
  }catch(e){
    // Preserve a previously established ready state. A transient status timeout must not
    // cancel independent agent/event reads or the lightweight /api/live loop.
    $('#connection').textContent='App API error: '+e.message;
    if(!wasReady)return;
  }
  const [agentsResult,eventsResult]=await Promise.allSettled([
    earlyAgents||api('api/agents'),
    earlyEvents||api('api/events?limit=60'),
  ]);
  if(agentsResult.status==='fulfilled'){
    lastAgents=agentsResult.value;
    window.__adaptiveAiAgents=lastAgents;
    window.dispatchEvent(new CustomEvent('adaptive-ai:agents',{detail:lastAgents}));
    renderAgents();
  }
  if(eventsResult.status==='fulfilled'){
    renderEvents(eventsResult.value);
  }else{
    $('#events').innerHTML=`<div class="empty">Recent activity will retry automatically. ${esc(eventsResult.reason?.message||eventsResult.reason||'read failed')}</div>`;
  }
  if(agentsResult.status==='rejected'){
    const c=$('#connection');
    if(c)c.textContent=`Agent UI read delayed · ${agentsResult.reason?.message||agentsResult.reason||'retrying'}`;
  }
  }finally{loadInFlight=false;}
}
function renderHistory(h,status={}){
  updateRescanButton(h);
  const ar=h.archive||{}, progress=Math.round((Number(h.progress)||0)*100),pending=discoveryPending(h);
  const eta=duration(h.eta_seconds),stageEta=duration(h.stage_eta_seconds),elapsed=duration(h.elapsed_seconds);
  const workDone=Number(h.work_done||0),workTotal=Number(h.work_total||0),workPct=workTotal?Math.min(100,Math.round(workDone*100/workTotal)):null;
  const phase=String(h.phase||'starting');
  const phases=[
    ['fast_targets','Targets','Find controllable devices'],
    ['fast_context','Context','Import recent house context'],
    ['context_refresh','Sensors','Refresh all eligible sensors'],
    ['rebuilding','Learn','Screen + replay historical behaviour'],
    ['benchmarking','Validate','Held-out behaviour benchmark'],
    ['ready','Ready','Qualified agents enter Shadow'],
  ];
  const phaseAliases={manual_ready:'fast_targets',automation_scan:'fast_targets',discovering:'fast_targets',enriching_targets:'fast_targets',ready_enriching:'fast_context',enriching_context:'fast_context',importing:'fast_context',fast_training:'rebuilding',training:'rebuilding'};
  const canonical=phaseAliases[phase]||phase;
  let activeIdx=phases.findIndex(x=>x[0]===canonical);if(activeIdx<0)activeIdx=0;
  const pipeline=phases.map((x,i)=>`<div class="prep-step ${i<activeIdx?'done':i===activeIdx?'active':''}"><i>${i<activeIdx?'✓':i+1}</i><div><b>${esc(x[1])}</b><span>${esc(x[2])}</span></div></div>`).join('');
  const etaPrimary=stageEta?`Current phase ${stageEta}`:(eta?`Adaptive overall estimate ${eta}`:'Calibrating ETA…');
  const workLine=workTotal?`${num(workDone)} / ${num(workTotal)} ${esc(h.work_unit||'items')} · ${workPct}%`:null;
  const ak=status.automation_knowledge||{};
  const tr=h.temporal_replay?.totals||{};
  const trReduction=tr.query_reduction_ratio==null?null:Math.round(Number(tr.query_reduction_ratio)*100);
  const trMeta=Number(tr.advances||0)
    ?`<span>Temporal replay: ${num(tr.sql_queries||0)} SQL · ${num(tr.forward_advances||0)} forward · ${num(tr.rewinds||0)} rewind${Number(tr.rewinds||0)===1?'':'s'}${trReduction!=null?` · ~${trReduction}% fewer as-of queries`:''}</span>`
    :'';
  $('#historyPanel').innerHTML=`
    <div class="history-head"><div><b>${esc(h.message||'Starting history engine…')}</b><span>${esc(phase.toUpperCase())}${h.phase_detail?` · ${esc(h.phase_detail)}`:''}</span></div><div class="history-percent"><strong>${progress}%</strong><small>${esc(etaPrimary)}</small></div></div>
    <div class="bar history-bar"><i style="width:${progress}%"></i></div>
    <div class="prep-pipeline">${pipeline}</div>
    <div class="history-timing"><b>${workLine?esc(workLine):(progress>=100?'Index ready':'Preparing measurable work…')}</b><span>${elapsed?`elapsed ${esc(elapsed)}`:''}${h.eta_source?` · ETA: ${esc(h.eta_source)}`:''}</span></div>
    ${workPct!=null?`<div class="bar work-bar"><i style="width:${workPct}%"></i></div>`:''}
    <div class="history-grid">
      <div><b>${num(ar.n||0)}</b><span>archived state changes</span></div>
      <div><b>${Number(ar.days||0).toFixed(1)} d</b><span>local history coverage</span></div>
      <div><b>${num(h.context_candidates||ar.entities||0)}</b><span>eligible context candidates</span></div>
      <div><b>${num(h.esphome_context_candidates||0)}</b><span>ESPHome sensors eligible</span></div>
      <div><b>${pending?'pending':(h.active||0)}/${h.eligible||h.controllable||0}</b><span>${pending?'activity classification / eligible targets':'active / eligible targets'}</span></div>
    </div>
    <div class="history-meta"><span>${h.filtered_config||0} config/diagnostic targets filtered</span><span>${pending?'activity classification pending':(h.inactive||0)+' insufficient target activity'}</span><span>${ak.automation_count||0} automations scanned</span><span>${status.realtime?.connected?'Realtime event stream active':'REST fallback active'}</span><span>${h.esphome_sensor_sibling_overrides||0} ESPHome sensor siblings preserved</span>${trMeta}${ak.error?`<span title="${esc(ak.error)}">Automation scan partial</span>`:''}</div>`;
}
function sensorRecommendations(a){const recs=a.runtime?.sensor_recommendations||[];if(!recs.length)return `<div class="sensor-ok">✓ Core sensor classes expected for this target are present.</div>`;return recs.map(r=>`<div class="sensor-rec"><i class="dot"></i><div><b>${esc(r.label)}</b><span>${esc(r.reason)}</span></div></div>`).join('');}
function contextInfluence(a){const xs=a.runtime?.top_context||[];if(!xs.length)return 'Not enough rewarded history to rank context yet.';return xs.map(x=>`${esc(x.feature)} (${Number(x.contribution)>=0?'+':''}${Number(x.contribution).toFixed(2)})`).join(' · ');}
function automationPrior(a){const xs=a.runtime?.automation_priors||[];if(!xs.length)return `<span class="muted">No automation targeting this entity was found.</span>`;return xs.map(x=>`<span class="prior-chip" title="${x.context_count||0} context entities">${esc(x.name||x.entity_id)}</span>`).join('');}
function displayValue(a,v){if(v==null)return '—';if(a.target_property==='power')return Number(v)>=.5?'ON':'OFF';if(a.target_property==='option_index'&&a.runtime?.last_prediction_label&&Number(v)===Number(a.runtime?.last_prediction))return esc(a.runtime.last_prediction_label);return Number(v).toFixed(1);}
function prediction(a){const rt=a.runtime||{};if(rt.last_prediction_label)return esc(rt.last_prediction_label);return displayValue(a,rt.last_prediction);}
function currentValue(a){return displayValue(a,a.runtime?.current_value);}
function sortedFilteredAgents(){
  const sort=$('#agentSort')?.value||localStorage.getItem('adaptiveAiAgentSort')||'confidence_desc';
  const q=($('#agentSearch')?.value||'').trim().toLowerCase();
  const xs=lastAgents.filter(a=>!q||`${a.name} ${a.target_entity} ${a.target_property}`.toLowerCase().includes(q));
  const conf=a=>Number(a.runtime?.last_confidence||0), hist=a=>Number(a.historical_count||0), name=a=>(a.name||a.target_entity||'').toLowerCase();
  xs.sort((a,b)=>sort==='confidence_asc'?conf(a)-conf(b):sort==='name_asc'?name(a).localeCompare(name(b)):sort==='name_desc'?name(b).localeCompare(name(a)):sort==='history_desc'?hist(b)-hist(a):conf(b)-conf(a));
  return xs;
}
function noAgentsMessage(h){
  if(discoveryPending(h))return `Recorder discovery is still running (${discoveryProgressText(h)}). Activity classification has not run yet, so 0 agents is not a final result.`;
  if(h?.phase==='ready')return 'No active controllable target met the discovery criteria. Try Rescan devices; config/diagnostic entities are intentionally ignored.';
  return 'Discovery is preparing target activity classification. Auto-discovered agents enter the initial-training FIFO after classification.';
}
function persistOpenAgentDetails(){localStorage.setItem('adaptiveAiOpenAgentDetails',JSON.stringify([...openAgentDetails]));}
function bindAgentDetails(){document.querySelectorAll('.agent-details[data-agent-id]').forEach(d=>d.addEventListener('toggle',()=>{const id=String(d.dataset.agentId);if(d.open)openAgentDetails.add(id);else openAgentDetails.delete(id);persistOpenAgentDetails();}));}
function renderAgents(){
  const agents=sortedFilteredAgents(),h=lastHistory||{};
  $('#agentCount').textContent=`${lastAgents.length} configured · ${agents.length} shown`;
  $('#agents').innerHTML=agents.length?agents.map(a=>{
    const rt=a.runtime||{},support=Number(rt.historical_support||0),novelty=Number(rt.context_novelty??1),havg=a.historical_average_reward==null?'—':Number(a.historical_average_reward).toFixed(2),ctx=rt.context_meta||{};
    const training=rt.training_state||a.training_state||'paused', paused=['paused','waiting','needs_retrain'].includes(training), indexing=training==='training', qualified=training==='qualified';
    const benchmark=a.benchmark_score==null?null:Number(a.benchmark_score), benchmarkSamples=Number(a.benchmark_samples||0), liveConf=Number(rt.last_confidence||0);
    const hasLiveInference=qualified&&rt.last_prediction!=null, conf=hasLiveInference?liveConf:(benchmark??liveConf), confidenceLabel=hasLiveInference?'Live confidence':'Candidate confidence';
    const perAction=a.benchmark_detail?.per_action_accuracy||{}, perActionText=Object.entries(perAction).map(([k,v])=>`${a.target_property==='power'?(Number(k)===0?'OFF':'ON'):'arm '+k} ${pct(Number(v))}`).join(' · ')||'—';
    const behaviourDrivers=(ctx.primary_behavioural_drivers||[]), primaryDriver=ctx.primary_occupancy_sensor||behaviourDrivers[0]||ctx.primary_local_sensor||null, driverScore=primaryDriver==null?null:Number((ctx.causal_behaviour_scores||ctx.causal_presence_scores||{})[primaryDriver]);
    const badge=(a.auto_created?'<span class="auto">AUTO</span>':'')+`<span class="train-state ${esc(training)}">${esc(training.toUpperCase())}</span>`;
    const rawDs=rt.decision_state||'idle', ds=paused?'paused':indexing?'training':rawDs, conflicts=Number(rt.enabled_automation_conflicts||0);
    const trainProgress=Math.round(Number(a.training_progress||0)*100);
    const neverTrained=training==='waiting'||training==='needs_retrain'||(paused&&benchmark==null&&!a.training_cursor_ts);
    const tq=a.training_queue||null, initialQueued=neverTrained&&a.auto_created&&tq&&['queued','active'].includes(String(tq.state||''));
    const decisionReason=training==='needs_retrain'?'NEEDS_RETRAIN: policy or feature schema changed; a rebuild is required.':initialQueued?(tq.state==='active'?'Initial historical training is starting now.':`Initial historical training is queued at position ${Number(tq.position||0)}. One heavy job runs at a time.`):neverTrained?(a.auto_created?'Initial training is waiting for FIFO admission.':'Training has not started. Press Train to build the first policy.'):paused?`Training paused. Behaviour confidence ${benchmark==null?'—':pct(benchmark)} (${benchmarkSamples} samples). Resume continues from the saved cursor.`:indexing?`Training this agent ${trainProgress}% — other agents remain idle to protect Home Assistant resources.`:(rt.decision_reason||'Waiting for inference');
    const service=rt.last_service?`${rt.last_service_ok===false?'✕':'✓'} ${esc(rt.last_service)}`:'No service call sent yet';
    return `<article class="agent card ${paused?'paused-agent':''} ${indexing?'training-agent':''}">
      <div class="agent-head"><div class="agent-title"><h3>${esc(a.name)} ${badge}</h3><div class="target">${esc(a.target_entity)} · ${esc(a.target_property)}</div></div><span class="mode ${esc(a.mode)}">${esc(a.mode).toUpperCase()}</span></div>
      <div class="agent-primary"><div class="state-metric current"><span>Current</span><b>${currentValue(a)}</b></div><div class="state-metric desired"><span>Desired</span><b>${prediction(a)}</b></div><div><span>${confidenceLabel}</span><b>${pct(conf)}</b></div><div class="benchmark-metric"><span>Behaviour benchmark</span><b>${benchmark==null?'—':pct(benchmark)}</b><small>${benchmarkSamples?`${benchmarkSamples} held-out`:''}</small></div><div><span>Support</span><b>${pct(support)}</b></div><div><span>History</span><b>${num(a.historical_count||0)}</b></div></div>
      <div class="home-intent"><b>${esc(rt.intent?.status||training.toUpperCase())}</b><span>Novelty ${pct(novelty)} · target-area occupancy in 3 s ${pct(rt.home_forecast?.occupancy_in_3s)}</span></div><div class="confidence"><div class="bar"><i style="width:${Math.round((benchmark??conf)*100)}%"></i></div></div>
      <div class="decision ${esc(ds)}"><b>${esc(ds.toUpperCase())}</b><span>${esc(decisionReason)}</span></div>
      <div class="agent-status"><span>${rt.realtime_connected?'⚡ realtime':'REST fallback'}</span><span>${esc(training)}${indexing?` ${trainProgress}%`:''}</span><span>stabilizacja ${rt.timing?.settling||0}s</span><span>${ctx.selected_entities||0}/${ctx.considered_entities||ctx.whole_home_entities||0} context selected</span><span>${ctx.dimensions||128} explicit dims</span></div>
      ${conflicts?`<div class="conflict-warning"><b>⚠ ${conflicts} enabled automation${conflicts===1?'':'s'} also target this entity</b><span>Control automatycznie wyłączy te automatyzacje i zatrzyma ich akcje. Wyłączona zostaje cała automatyzacja, także jeśli obsługuje inne urządzenia.</span></div>`:''}
      ${(rt.automation_priors||[]).length?`<div class="automation-prior"><b>Automation prior</b>${automationPrior(a)}</div>`:''}
      <details class="agent-details" data-agent-id="${esc(a.id)}" ${openAgentDetails.has(String(a.id))?'open':''}><summary><span>Control diagnostics & learning</span><span class="details-chevron" aria-hidden="true">⌄</span></summary>
        ${homeAgentDetails(a)}<div class="detail"><b>Last HA service:</b> ${service}${rt.last_service_error?` · ${esc(rt.last_service_error)}`:''}<br><b>Źródło ostatniej zmiany:</b> ${esc(({own_command:"Własne polecenie",manual_user:"Użytkownik",external:"Zewnętrzna zmiana"})[rt.last_change_origin]||"—")}<br><b>Czas wywołania HA:</b> ${rt.last_service_latency_ms==null?"—":num(rt.last_service_latency_ms)+" ms"}<br><b>Potwierdzenie urządzenia:</b> ${rt.ack_latency_seconds==null?"—":num(rt.ack_latency_seconds,2)+" s"}<br><b>Model:</b> ${esc(rt.model||'—')}<br><b>History heads:</b> ${(rt.prediction_horizons||[]).map(x=>esc(x+'s')).join(' · ')||'—'}<br><b>Behaviour benchmark:</b> ${benchmark==null?'—':pct(benchmark)} · ${benchmarkSamples} held-out transitions · threshold >78%<br><b>Historical cursor:</b> ${a.training_cursor_ts?new Date(a.training_cursor_ts*1000).toLocaleString():'—'} · ${trainProgress}% of current pass<br><b>Benchmark source:</b> ${esc(a.benchmark_source||'—')}<br><b>Benchmark origins:</b> ${esc(Object.entries(a.benchmark_detail?.origin_counts||{}).map(([k,v])=>`${k}=${v}`).join(' · ')||'—')}<br><b>Per-action benchmark:</b> ${esc(perActionText)}<br><b>Held-out backtest:</b> ${rt.validation_samples?`${pct(Number(rt.validation_accuracy||0))} accuracy · ${num(rt.validation_samples)} weighted samples · confidence ceiling ${pct(Number(rt.validation_lower_bound||0))}`:'not enough validation data yet'}<br><b>Structural confidence:</b> ${pct(Number(rt.structural_confidence||0))}<br><b>Historical support / novelty:</b> ${pct(support)} / ${pct(novelty)}<br><b>Potwierdzenie / stabilizacja:</b> ${rt.timing?.acknowledgement||0}s / ${rt.timing?.settling||0}s<br><b>Live reward:</b> ${rt.last_reward==null?'—':Number(rt.last_reward).toFixed(2)} ${rt.last_reward_reason?'· '+esc(rt.last_reward_reason):''}${rt.pending_feedback?' · awaiting feedback':''}<br><b>Controllable-device inputs excluded:</b> ${num(ctx.excluded_controllable_context_entities||0)}<br><b>Electrical-unit inputs excluded:</b> ${num(ctx.excluded_electrical_context_entities||0)}<br><b>Context candidates screened:</b> ${num(ctx.considered_entities||0)} · selected ${num(ctx.selected_entities||0)}<br><b>ESPHome context:</b> ${num(ctx.esphome_context_candidates||0)} eligible · ${num(ctx.esphome_selected_context||0)} selected · ${num(ctx.esphome_sensor_sibling_overrides||0)} sensor siblings preserved<br><b>Primary behavioural driver:</b> ${esc(primaryDriver||"—")}${Number.isFinite(driverScore)?` · historical score ${pct(driverScore)}`:""}<br><b>Behavioural drivers:</b> ${esc(behaviourDrivers.join(" · ")||"—")}<br><b>Primary local sensor:</b> ${esc(ctx.primary_local_sensor||"—")}<br><b>Last context trigger:</b> ${esc((ctx.trigger_entities||[]).join(" · ")||"—")}<br><b>Upstream early cues:</b> ${esc((ctx.upstream_sensors||[]).slice(0,4).join(" · ")||"—")}<br><b>Automation hints:</b> ${ctx.automation_hint_entities||0} upstream entities</div>
        <div class="sensor-box"><h4>What additional sensing could improve this agent?</h4>${sensorRecommendations(a)}<div class="context-list"><b>Current learned influences:</b> ${contextInfluence(a)}</div><div class="context-list"><b>Selected context:</b> ${(rt.selected_context_entities||[]).slice(0,12).map(esc).join(' · ')||'—'}${(rt.selected_context_entities||[]).length>12?' …':''}</div></div>
      </details>
      <div class="actions">${['shadow','control','paused'].map(m=>`<button class="ghost ${a.mode===m?'active':''}" ${(m==='control'&&!qualified)||indexing?'disabled title="Requires completed benchmark >78%"':''} onclick="setMode('${a.id}','${m}')">${m}</button>`).join('')}${neverTrained?(initialQueued?`<button class="ghost resume" disabled>Queued</button>`:`<button class="ghost resume" onclick="trainAgent('${a.id}')">Train</button>`):(paused?`<button class="ghost resume" onclick="resumeLearning('${a.id}')">Resume</button>`:'')}<button class="ghost" onclick="editAgent('${a.id}')">Ustawienia</button><button class="ghost" ${a.mode!=='control'||!qualified?'disabled':''} onclick="verifyControl('${a.id}')">Verify control</button><button class="ghost" onclick="openExperiments('${a.id}')">Eksperymenty</button><button class="ghost rebuild" ${indexing||neverTrained?'disabled':''} onclick="resetLearning('${a.id}')">Rebuild</button><button class="ghost danger" onclick="removeAgent('${a.id}')">Delete</button></div>
    </article>`;
  }).join(''):`<div class="empty card"><b>No matching agents</b><span>${esc(lastAgents.length?'Change the search/sort filter.':noAgentsMessage(h))}</span></div>`;
  bindAgentDetails();
}
function renderEvents(events){$('#events').innerHTML=events.length?events.map(e=>`<div class="event"><time>${esc(e.created_at)}</time><div><strong>${esc(e.message)}</strong><span class="kind">${esc(e.kind)}</span></div></div>`).join(''):'<div class="empty">No activity yet.</div>';}
async function setMode(id,mode){if(mode==='control'&&!confirm('Control wyłączy automatyzacje sterujące tą encją i zatrzyma ich trwające akcje. Wyłącza całe automatyzacje, także te obsługujące kilka urządzeń. Shadow nie włączy ich ponownie. Włączyć Control?'))return;try{await api(`api/agents/${id}`,{method:'PATCH',body:JSON.stringify({mode})});await load();}catch(e){alert('Nie udało się zmienić trybu: '+e.message);await load();}}
async function verifyControl(id){if(!confirm('This sends the device its CURRENT value again through the same Home Assistant service path used by Control. It should not intentionally change the setting. Continue?'))return;try{const r=await api(`api/agents/${id}/verify-control`,{method:'POST',body:'{}'});alert(`Control path OK: ${r.service}`);await load();}catch(e){alert('Control verification failed: '+e.message);await load();}}
async function trainAgent(id){if(!confirm('Start training this agent now? Low-memory mode trains only one agent at a time; all other agents stay idle.'))return;try{await api(`api/agents/${id}/train`,{method:'POST',body:'{}'});await load();}catch(e){alert('Train failed: '+e.message);await load();}}
async function resumeLearning(id){if(!confirm('Resume learning from the saved historical cursor? Existing model and benchmark are preserved.'))return;try{await api(`api/agents/${id}/resume`,{method:'POST',body:'{}'});await load();}catch(e){alert('Resume failed: '+e.message);await load();}}
async function resetLearning(id){if(!confirm('FULL REBUILD: clear this agent model, benchmark and saved cursor, then index all locally archived history from the beginning? Use this after adding/changing sensors.'))return;await api(`api/agents/${id}/learning`,{method:'DELETE'});load();}
async function removeAgent(id){if(!confirm('Delete this agent and its learned policy? Auto-discovery may recreate it if the device remains active.'))return;await api(`api/agents/${id}`,{method:'DELETE'});load();}
window.setMode=setMode;window.verifyControl=verifyControl;window.trainAgent=trainAgent;window.resumeLearning=resumeLearning;window.resetLearning=resetLearning;window.removeAgent=removeAgent;
async function rescan(){
  const b=$('#rescanBtn');
  if(lastHistory?.discovery_job_active){updateRescanButton(lastHistory);return;}
  b.disabled=true;b.textContent='Starting scan…';
  try{
    const r=await api('api/discovery/rescan',{method:'POST',body:'{}'});
    if(r?.history)lastHistory=r.history;
    updateRescanButton(lastHistory);
    await load();
  }catch(e){
    alert(e.message);
    updateRescanButton(lastHistory);
  }
}
async function openDialog(){entities=await api('api/entities');const targets=entities.filter(e=>e.target_options?.length);$('#targetEntity').innerHTML=targets.map(e=>`<option value="${esc(e.entity_id)}">${esc(e.name)} — ${esc(e.entity_id)}</option>`).join('');updateTargetProps();$('#agentDialog').showModal();}
function updateTargetProps(){const target=entities.find(e=>e.entity_id===$('#targetEntity').value),opts=target?.target_options||[];$('#targetProperty').innerHTML=opts.map(o=>`<option value="${esc(o.property)}" data-min="${o.min}" data-max="${o.max}" data-deadband="${o.deadband}" data-explore="${o.exploration_step}">${esc(o.label)}</option>`).join('');updateBounds();}
function updateBounds(){const o=$('#targetProperty').selectedOptions[0];if(!o)return;const f=$('#agentForm');f.min_value.value=o.dataset.min;f.max_value.value=o.dataset.max;f.deadband.value=o.dataset.deadband;f.exploration_step.value=o.dataset.explore;}
$('#newAgentBtn').onclick=openDialog;$('#refreshBtn').onclick=load;$('#rescanBtn').onclick=rescan;$('#closeDialog').onclick=()=>$('#agentDialog').close();$('#cancelDialog').onclick=()=>$('#agentDialog').close();$('#targetEntity').onchange=updateTargetProps;$('#targetProperty').onchange=updateBounds;
// Resolve the active renderer at event time, including P0 and learning hooks.
$('#agentSearch').oninput=()=>renderAgents();const savedSort=localStorage.getItem('adaptiveAiAgentSort');if(savedSort)$('#agentSort').value=savedSort;$('#agentSort').onchange=()=>{localStorage.setItem('adaptiveAiAgentSort',$('#agentSort').value);renderAgents();};
$('#agentForm').onsubmit=async e=>{e.preventDefault();const f=e.target;const body={name:f.name.value,target_entity:f.target_entity.value,target_property:f.target_property.value,min_value:Number(f.min_value.value),max_value:Number(f.max_value.value),exploration_step:Number(f.exploration_step.value),confidence_threshold:Number(f.confidence_threshold.value),deadband:Number(f.deadband.value),action_interval:Number(f.action_interval.value),mode:'paused',micro_exploration:false};try{await api('api/agents',{method:'POST',body:JSON.stringify(body)});f.reset();$('#agentDialog').close();load();}catch(err){alert(err.message)}};
load();setInterval(load,4000);
