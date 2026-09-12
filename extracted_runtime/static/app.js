let entities=[];
let lastHistory={};
let lastAgents=[];
let lastStatus={};
const openAgentDetails=new Set(JSON.parse(localStorage.getItem('adaptiveAiOpenAgentDetails')||'[]').map(String));
const $=s=>document.querySelector(s);
const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});if(!r.ok)throw new Error(await r.text());return r.status===204?null:r.json()};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=v=>Math.round((Number(v)||0)*100)+'%';
const num=(v,d=0)=>Number(v||0).toLocaleString(undefined,{maximumFractionDigits:d});
function duration(v){const s=Number(v);if(!Number.isFinite(s)||s<0)return null;if(s<20)return '< 1 min';if(s<90)return '≈ 1 min';const m=Math.round(s/60);if(m<60)return `≈ ${m} min`;const h=Math.floor(m/60),rm=m%60;return rm?`≈ ${h} h ${rm} min`:`≈ ${h} h`;}
function renderOverview(status){
  const h=status.history||{};
  $('#overview').innerHTML=`
    <div class="metric"><b>${status.state_count||0}</b><span>HA entities in context</span></div>
    <div class="metric"><b>${status.agent_count||0}</b><span>agents · ${h.active||0} active / ${h.eligible||h.controllable||0} eligible</span></div>
    <div class="metric"><b>${pct(status.average_confidence)}</b><span>average policy confidence</span></div>
    <div class="metric"><b>${num(status.historical_experience_count||0)}</b><span>predictive RL experiences</span></div>`;
}
async function load(){
  let status;
  try{
    status=await api('api/status');lastStatus=status;
    const c=$('#connection');
    const rt=status.realtime||{};
    c.textContent=status.ha_connected?`HA connected · ${status.state_count} entities${rt.connected?' · realtime':''}`:`HA disconnected · ${status.ha_error||status.engine_error||'unknown error'}`;
    c.className='pill'+(status.ha_connected?' good':'');
    lastHistory=status.history||{};renderOverview(status);renderHistory(lastHistory,status);
  }catch(e){$('#connection').textContent='App API error: '+e.message;return;}
  try{
    const [agents,events]=await Promise.all([api('api/agents'),api('api/events?limit=60')]);
    lastAgents=agents;renderAgents();renderEvents(events);
  }catch(e){$('#events').innerHTML=`<div class="empty">UI data will retry automatically. ${esc(e.message)}</div>`;}
}
function renderHistory(h,status={}){
  const ar=h.archive||{}, progress=Math.round((Number(h.progress)||0)*100);
  const eta=duration(h.eta_seconds),stageEta=duration(h.stage_eta_seconds),elapsed=duration(h.elapsed_seconds);
  const chunks=(h.chunk_total||0)>0?`${h.chunk_done||0}/${h.chunk_total} chunks`:null;
  const fullEta=progress>=100?'Complete':(eta?`Full index ${eta}`:'Estimating…');
  const stage=[chunks,stageEta?`current step ${stageEta}`:null].filter(Boolean).join(' · ');
  const ak=status.automation_knowledge||{};
  $('#historyPanel').innerHTML=`
    <div class="history-head"><div><b>${esc(h.message||'Starting history engine…')}</b><span>${esc(h.phase||'starting')}</span></div><div class="history-percent"><strong>${progress}%</strong><small>${esc(fullEta)}</small></div></div>
    <div class="bar history-bar"><i style="width:${progress}%"></i></div>
    <div class="history-timing"><b>${progress>=100?'Index ready':(eta?`Estimated time remaining ${esc(eta)}`:'Calculating ETA…')}</b><span>${elapsed?`elapsed ${esc(elapsed)}`:''}${elapsed&&stage?' · ':''}${esc(stage||'')}</span></div>
    <div class="history-grid">
      <div><b>${num(ar.n||0)}</b><span>archived state changes</span></div>
      <div><b>${Number(ar.days||0).toFixed(1)} d</b><span>local history coverage</span></div>
      <div><b>${num(ar.entities||0)}</b><span>archived entities</span></div>
      <div><b>${h.active||0}/${h.eligible||h.controllable||0}</b><span>active / eligible targets</span></div>
    </div>
    <div class="history-meta"><span>${h.filtered_config||0} config/diagnostic filtered</span><span>${h.inactive||0} currently insufficient activity</span><span>${ak.automation_count||0} automations scanned</span><span>${status.realtime?.connected?'Realtime event stream active':'REST fallback active'}</span>${ak.error?`<span title="${esc(ak.error)}">Automation scan partial</span>`:''}</div>`;
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
function noAgentsMessage(h){if(h?.phase==='ready')return 'No active controllable target met the discovery criteria. Try Rescan devices; config/diagnostic entities are intentionally ignored.';return 'Discovery is running. Agents appear in Shadow as soon as sufficient real use is found.';}
function persistOpenAgentDetails(){localStorage.setItem('adaptiveAiOpenAgentDetails',JSON.stringify([...openAgentDetails]));}
function bindAgentDetails(){document.querySelectorAll('.agent-details[data-agent-id]').forEach(d=>d.addEventListener('toggle',()=>{const id=String(d.dataset.agentId);if(d.open)openAgentDetails.add(id);else openAgentDetails.delete(id);persistOpenAgentDetails();}));}
function renderAgents(){
  const agents=sortedFilteredAgents(),h=lastHistory||{};
  $('#agentCount').textContent=`${lastAgents.length} configured · ${agents.length} shown`;
  $('#agents').innerHTML=agents.length?agents.map(a=>{
    const rt=a.runtime||{},support=Number(rt.historical_support||0),novelty=Number(rt.context_novelty??1),havg=a.historical_average_reward==null?'—':Number(a.historical_average_reward).toFixed(2),ctx=rt.context_meta||{};
    const training=rt.training_state||a.training_state||'training', paused=training==='paused', indexing=training==='training', qualified=training==='qualified';
    const benchmark=a.benchmark_score==null?null:Number(a.benchmark_score), benchmarkSamples=Number(a.benchmark_samples||0), liveConf=Number(rt.last_confidence||0);
    const hasLiveInference=qualified&&rt.last_prediction!=null, conf=hasLiveInference?liveConf:(benchmark??liveConf), confidenceLabel=hasLiveInference?'Live confidence':'Candidate confidence';
    const perAction=a.benchmark_detail?.per_action_accuracy||{}, perActionText=Object.entries(perAction).map(([k,v])=>`${a.target_property==='power'?(Number(k)===0?'OFF':'ON'):'arm '+k} ${pct(Number(v))}`).join(' · ')||'—';
    const behaviourDrivers=(ctx.primary_behavioural_drivers||[]), primaryDriver=ctx.primary_occupancy_sensor||behaviourDrivers[0]||ctx.primary_local_sensor||null, driverScore=primaryDriver==null?null:Number((ctx.causal_behaviour_scores||ctx.causal_presence_scores||{})[primaryDriver]);
    const badge=(a.auto_created?'<span class="auto">AUTO</span>':'')+`<span class="train-state ${esc(training)}">${esc(training.toUpperCase())}</span>`;
    const rawDs=rt.decision_state||'idle', ds=paused?'paused':indexing?'training':rawDs, conflicts=Number(rt.enabled_automation_conflicts||0);
    const trainProgress=Math.round(Number(a.training_progress||0)*100);
    const decisionReason=paused?`Historical pass complete. Behaviour confidence ${benchmark==null?'—':pct(benchmark)} (${benchmarkSamples} samples) — paused to save CPU. Resume continues from the saved cursor.`:indexing?`Indexing historical data ${trainProgress}% — live inference is paused until the full pass reaches current data.`:(rt.decision_reason||'Waiting for inference');
    const service=rt.last_service?`${rt.last_service_ok===false?'✕':'✓'} ${esc(rt.last_service)}`:'No service call sent yet';
    return `<article class="agent card ${paused?'paused-agent':''} ${indexing?'training-agent':''}">
      <div class="agent-head"><div class="agent-title"><h3>${esc(a.name)} ${badge}</h3><div class="target">${esc(a.target_entity)} · ${esc(a.target_property)}</div></div><span class="mode ${esc(a.mode)}">${esc(a.mode).toUpperCase()}</span></div>
      <div class="agent-primary"><div class="state-metric current"><span>Current</span><b>${currentValue(a)}</b></div><div class="state-metric desired"><span>Desired</span><b>${prediction(a)}</b></div><div><span>${confidenceLabel}</span><b>${pct(conf)}</b></div><div class="benchmark-metric"><span>Behaviour benchmark</span><b>${benchmark==null?'—':pct(benchmark)}</b><small>${benchmarkSamples?`${benchmarkSamples} held-out`:''}</small></div><div><span>Support</span><b>${pct(support)}</b></div><div><span>History</span><b>${num(a.historical_count||0)}</b></div></div>
      <div class="confidence"><div class="bar"><i style="width:${Math.round((benchmark??conf)*100)}%"></i></div></div>
      <div class="decision ${esc(ds)}"><b>${esc(ds.toUpperCase())}</b><span>${esc(decisionReason)}</span></div>
      <div class="agent-status"><span>${rt.realtime_connected?'⚡ realtime':'REST fallback'}</span><span>${esc(training)}${indexing?` ${trainProgress}%`:''}</span><span>stabilizacja ${rt.timing?.settling||0}s</span><span>${ctx.selected_entities||0}/${ctx.considered_entities||ctx.whole_home_entities||0} context selected</span><span>${ctx.dimensions||128} explicit dims</span></div>
      ${conflicts?`<div class="conflict-warning"><b>⚠ ${conflicts} enabled automation${conflicts===1?'':'s'} also target this entity</b><span>Control automatycznie wyłączy te automatyzacje i zatrzyma ich akcje. Wyłączona zostaje cała automatyzacja, także jeśli obsługuje inne urządzenia.</span></div>`:''}
      ${(rt.automation_priors||[]).length?`<div class="automation-prior"><b>Automation prior</b>${automationPrior(a)}</div>`:''}
      <details class="agent-details" data-agent-id="${esc(a.id)}" ${openAgentDetails.has(String(a.id))?'open':''}><summary><span>Control diagnostics & learning</span><span class="details-chevron" aria-hidden="true">⌄</span></summary>
        <div class="detail"><b>Last HA service:</b> ${service}${rt.last_service_error?` · ${esc(rt.last_service_error)}`:''}<br><b>Źródło ostatniej zmiany:</b> ${esc(({own_command:"Własne polecenie",manual_user:"Użytkownik",external:"Zewnętrzna zmiana"})[rt.last_change_origin]||"—")}<br><b>Czas wywołania HA:</b> ${rt.last_service_latency_ms==null?"—":num(rt.last_service_latency_ms)+" ms"}<br><b>Potwierdzenie urządzenia:</b> ${rt.ack_latency_seconds==null?"—":num(rt.ack_latency_seconds,2)+" s"}<br><b>Model:</b> ${esc(rt.model||'—')}<br><b>History heads:</b> ${(rt.prediction_horizons||[]).map(x=>esc(x+'s')).join(' · ')||'—'}<br><b>Behaviour benchmark:</b> ${benchmark==null?'—':pct(benchmark)} · ${benchmarkSamples} held-out transitions · threshold >78%<br><b>Historical cursor:</b> ${a.training_cursor_ts?new Date(a.training_cursor_ts*1000).toLocaleString():'—'} · ${trainProgress}% of current pass<br><b>Benchmark source:</b> ${esc(a.benchmark_source||'—')}<br><b>Benchmark origins:</b> ${esc(Object.entries(a.benchmark_detail?.origin_counts||{}).map(([k,v])=>`${k}=${v}`).join(' · ')||'—')}<br><b>Per-action benchmark:</b> ${esc(perActionText)}<br><b>Held-out backtest:</b> ${rt.validation_samples?`${pct(Number(rt.validation_accuracy||0))} accuracy · ${num(rt.validation_samples)} weighted samples · confidence ceiling ${pct(Number(rt.validation_lower_bound||0))}`:'not enough validation data yet'}<br><b>Structural confidence:</b> ${pct(Number(rt.structural_confidence||0))}<br><b>Historical support / novelty:</b> ${pct(support)} / ${pct(novelty)}<br><b>Potwierdzenie / stabilizacja:</b> ${rt.timing?.acknowledgement||0}s / ${rt.timing?.settling||0}s<br><b>Live reward:</b> ${rt.last_reward==null?'—':Number(rt.last_reward).toFixed(2)} ${rt.last_reward_reason?'· '+esc(rt.last_reward_reason):''}${rt.pending_feedback?' · awaiting feedback':''}<br><b>Controllable-device inputs excluded:</b> ${num(ctx.excluded_controllable_context_entities||0)}<br><b>Electrical-unit inputs excluded:</b> ${num(ctx.excluded_electrical_context_entities||0)}<br><b>Context candidates screened:</b> ${num(ctx.considered_entities||0)} · selected ${num(ctx.selected_entities||0)}<br><b>Primary behavioural driver:</b> ${esc(primaryDriver||"—")}${Number.isFinite(driverScore)?` · historical score ${pct(driverScore)}`:""}<br><b>Behavioural drivers:</b> ${esc(behaviourDrivers.join(" · ")||"—")}<br><b>Primary local sensor:</b> ${esc(ctx.primary_local_sensor||"—")}<br><b>Last context trigger:</b> ${esc((ctx.trigger_entities||[]).join(" · ")||"—")}<br><b>Upstream early cues:</b> ${esc((ctx.upstream_sensors||[]).slice(0,4).join(" · ")||"—")}<br><b>Automation hints:</b> ${ctx.automation_hint_entities||0} upstream entities</div>
        <div class="sensor-box"><h4>What additional sensing could improve this agent?</h4>${sensorRecommendations(a)}<div class="context-list"><b>Current learned influences:</b> ${contextInfluence(a)}</div><div class="context-list"><b>Selected context:</b> ${(rt.selected_context_entities||[]).slice(0,12).map(esc).join(' · ')||'—'}${(rt.selected_context_entities||[]).length>12?' …':''}</div></div>
      </details>
      <div class="actions">${['shadow','control','paused'].map(m=>`<button class="ghost ${a.mode===m?'active':''}" ${(m==='control'&&!qualified)||indexing?'disabled title="Requires completed benchmark >78%"':''} onclick="setMode('${a.id}','${m}')">${m}</button>`).join('')}${paused?`<button class="ghost resume" onclick="resumeLearning('${a.id}')">Resume</button>`:''}<button class="ghost" onclick="editAgent('${a.id}')">Ustawienia</button><button class="ghost" onclick="verifyControl('${a.id}')">Verify control</button><button class="ghost ${a.micro_exploration?'active warn':''}" ${!qualified?'disabled':''} onclick="toggleExplore('${a.id}',${a.micro_exploration?'false':'true'})">Explore ${a.micro_exploration?'ON':'OFF'}</button><button class="ghost rebuild" ${indexing?'disabled':''} onclick="resetLearning('${a.id}')">Rebuild</button><button class="ghost danger" onclick="removeAgent('${a.id}')">Delete</button></div>
    </article>`;
  }).join(''):`<div class="empty card"><b>No matching agents</b><span>${esc(lastAgents.length?'Change the search/sort filter.':noAgentsMessage(h))}</span></div>`;
  bindAgentDetails();
}
function renderEvents(events){$('#events').innerHTML=events.length?events.map(e=>`<div class="event"><time>${esc(e.created_at)}</time><div><strong>${esc(e.message)}</strong><span class="kind">${esc(e.kind)}</span></div></div>`).join(''):'<div class="empty">No activity yet.</div>';}
async function setMode(id,mode){if(mode==='control'&&!confirm('Control wyłączy automatyzacje sterujące tą encją i zatrzyma ich trwające akcje. Wyłącza całe automatyzacje, także te obsługujące kilka urządzeń. Shadow nie włączy ich ponownie. Włączyć Control?'))return;try{await api(`api/agents/${id}`,{method:'PATCH',body:JSON.stringify({mode})});await load();}catch(e){alert('Nie udało się zmienić trybu: '+e.message);await load();}}
async function verifyControl(id){if(!confirm('This sends the device its CURRENT value again through the same Home Assistant service path used by Control. It should not intentionally change the setting. Continue?'))return;try{const r=await api(`api/agents/${id}/verify-control`,{method:'POST',body:'{}'});alert(`Control path OK: ${r.service}`);await load();}catch(e){alert('Control verification failed: '+e.message);await load();}}
async function toggleExplore(id,on){if(on&&!confirm('Micro-exploration is optional and constrained to nearby actions. Enable?'))return;await api(`api/agents/${id}`,{method:'PATCH',body:JSON.stringify({micro_exploration:on})});load();}
async function resumeLearning(id){if(!confirm('Resume learning from the saved historical cursor? Existing model and benchmark are preserved.'))return;try{await api(`api/agents/${id}/resume`,{method:'POST',body:'{}'});await load();}catch(e){alert('Resume failed: '+e.message);await load();}}
async function resetLearning(id){if(!confirm('FULL REBUILD: clear this agent model, benchmark and saved cursor, then index all locally archived history from the beginning? Use this after adding/changing sensors.'))return;await api(`api/agents/${id}/learning`,{method:'DELETE'});load();}
async function removeAgent(id){if(!confirm('Delete this agent and its learned policy? Auto-discovery may recreate it if the device remains active.'))return;await api(`api/agents/${id}`,{method:'DELETE'});load();}
window.setMode=setMode;window.verifyControl=verifyControl;window.toggleExplore=toggleExplore;window.resumeLearning=resumeLearning;window.resetLearning=resetLearning;window.removeAgent=removeAgent;
async function rescan(){const b=$('#rescanBtn');b.disabled=true;b.textContent='Scanning…';try{const r=await api('api/discovery/rescan',{method:'POST',body:'{}'});b.textContent=`Found +${r.created||0}`;setTimeout(()=>b.textContent='Rescan devices',1500);await load();}catch(e){alert(e.message);b.textContent='Rescan devices';}finally{b.disabled=false;}}
async function openDialog(){entities=await api('api/entities');const targets=entities.filter(e=>e.target_options?.length);$('#targetEntity').innerHTML=targets.map(e=>`<option value="${esc(e.entity_id)}">${esc(e.name)} — ${esc(e.entity_id)}</option>`).join('');updateTargetProps();$('#agentDialog').showModal();}
function updateTargetProps(){const target=entities.find(e=>e.entity_id===$('#targetEntity').value),opts=target?.target_options||[];$('#targetProperty').innerHTML=opts.map(o=>`<option value="${esc(o.property)}" data-min="${o.min}" data-max="${o.max}" data-deadband="${o.deadband}" data-explore="${o.exploration_step}">${esc(o.label)}</option>`).join('');updateBounds();}
function updateBounds(){const o=$('#targetProperty').selectedOptions[0];if(!o)return;const f=$('#agentForm');f.min_value.value=o.dataset.min;f.max_value.value=o.dataset.max;f.deadband.value=o.dataset.deadband;f.exploration_step.value=o.dataset.explore;}
$('#newAgentBtn').onclick=openDialog;$('#refreshBtn').onclick=load;$('#rescanBtn').onclick=rescan;$('#closeDialog').onclick=()=>$('#agentDialog').close();$('#cancelDialog').onclick=()=>$('#agentDialog').close();$('#targetEntity').onchange=updateTargetProps;$('#targetProperty').onchange=updateBounds;
$('#agentSearch').oninput=renderAgents;const savedSort=localStorage.getItem('adaptiveAiAgentSort');if(savedSort)$('#agentSort').value=savedSort;$('#agentSort').onchange=()=>{localStorage.setItem('adaptiveAiAgentSort',$('#agentSort').value);renderAgents();};
$('#agentForm').onsubmit=async e=>{e.preventDefault();const f=e.target;const body={name:f.name.value,target_entity:f.target_entity.value,target_property:f.target_property.value,min_value:Number(f.min_value.value),max_value:Number(f.max_value.value),exploration_step:Number(f.exploration_step.value),confidence_threshold:Number(f.confidence_threshold.value),deadband:Number(f.deadband.value),action_interval:Number(f.action_interval.value),mode:'paused',micro_exploration:false};try{await api('api/agents',{method:'POST',body:JSON.stringify(body)});f.reset();$('#agentDialog').close();load();}catch(err){alert(err.message)}};
load();setInterval(load,4000);
