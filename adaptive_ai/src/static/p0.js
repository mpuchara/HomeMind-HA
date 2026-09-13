// P0/P1 runtime UX. Loaded after app.js so API/actions stay in one place.
(() => {
  const nodes = new Map();
  const oldHome = renderHome;
  const text = (root, role, value) => {
    const el = root.querySelector(`[data-p0="${role}"]`);
    const next = String(value ?? '');
    if (el && el.textContent !== next) el.textContent = next;
  };
  const ms = v => v == null || !Number.isFinite(Number(v)) ? '—' : (Number(v) < 1000 ? `${Math.round(Number(v))} ms` : `${(Number(v)/1000).toFixed(2)} s`);

  duration = v => {
    if (v == null || !Number.isFinite(Number(v)) || Number(v) < 0) return null;
    const s = Number(v);
    if (s < 90) return `≈ ${Math.max(1, Math.round(s))} s`;
    if (s < 600) { const m=Math.floor(s/60), r=Math.round(s%60); return r ? `≈ ${m} min ${r} s` : `≈ ${m} min`; }
    if (s < 3600) return `≈ ${Math.round(s/60)} min`;
    const h=Math.floor(s/3600), m=Math.round((s-h*3600)/60); return m ? `≈ ${h} h ${m} min` : `≈ ${h} h`;
  };

  const startup = status => {
    const s=status.startup||{}, panel=$('#startupPanel');
    if (!panel) return;
    panel.hidden=Boolean(s.ready);
    if (s.ready) return;
    const steps=Math.max(1,Number(s.steps||7)), step=Math.max(0,Number(s.step||0)), progress=Math.round(step*100/steps);
    panel.innerHTML=`<div class="history-head"><div><b>${s.error?'Startup problem':'Starting Adaptive AI'}</b><span>${esc(s.message||'Preparing runtime…')}</span></div><div class="history-percent"><strong>${step}/${steps}</strong><small>${esc(duration(s.elapsed_seconds)||'')}</small></div></div><div class="bar history-bar"><i style="width:${progress}%"></i></div>${s.error?`<div class="conflict-warning"><b>Runtime did not start</b><span>${esc(s.error)}</span></div>`:''}`;
  };

  renderOverview = status => {
    startup(status);
    const control=lastAgents.filter(a=>a.mode==='control').length, shadow=lastAgents.filter(a=>a.mode==='shadow').length;
    const p95=status.telemetry?.metrics?.event_to_intent?.p95_ms;
    $('#overview').innerHTML=`<div class="metric"><b>${lastAgents.length||status.agent_count||0}</b><span>agents</span></div><div class="metric"><b>${control}</b><span>Control · ${shadow} Shadow</span></div><div class="metric"><b>${status.state_count||0}</b><span>HA entities</span></div><div class="metric"><b>${p95==null?'—':ms(p95)}</b><span>event → intent p95</span></div>`;
    const c=$('#connection'), s=status.startup||{}, rt=status.realtime||{};
    if (!s.ready) { c.textContent=s.error?'Startup error':`Starting · ${s.step||0}/${s.steps||7}`; c.className='pill'; return; }
    c.textContent=status.ha_connected?`HA connected${rt.connected?' · realtime':' · REST fallback'}`:`Connecting to HA${status.ha_error?' · '+status.ha_error:''}`;
    c.className='pill'+(status.ha_connected?' good':'');
  };

  const taskFor = (h,status) => {
    const s=status.startup||{};
    if (!s.ready) return {title:'Starting Adaptive AI', detail:s.message||'Preparing runtime', p:(s.step||0)/Math.max(1,s.steps||7), eta:null, work:null};
    const training=lastAgents.find(a=>(a.runtime?.training_state||a.training_state)==='training');
    if (training) return {title:`Training ${training.name}`,detail:h.phase_detail||h.message||'Replaying recorded behaviour',p:Number(training.training_progress||0),eta:h.stage_eta_seconds??h.eta_seconds,work:h.work_total?`${num(h.work_done)} / ${num(h.work_total)} ${h.work_unit||'history rows'}`:null};
    const b=status.home_bootstrap||{};
    if (['IMPORTING','TRAINING'].includes(b.state)) return {title:'Building Home Intelligence',detail:`${num(b.rows||0)} rows · ${num(b.rows_per_second||0)} rows/s`,p:Number(b.progress||0),eta:b.eta_seconds,work:null};
    if (status.heavy_job || (h.phase && h.phase!=='ready')) return {title:'Preparing local intelligence',detail:h.phase_detail||h.message||String(status.heavy_job||h.phase),p:Number(h.progress||0),eta:h.stage_eta_seconds??h.eta_seconds,work:h.work_total?`${num(h.work_done)} / ${num(h.work_total)} ${h.work_unit||'items'}`:null};
    return {title:'System ready',detail:status.ha_connected?'Agents react to Home Assistant events in realtime.':'Runtime is ready; waiting for Home Assistant.',p:1,eta:null,work:null};
  };

  renderHistory = (h,status={}) => {
    const t=taskFor(h,status), p=Math.max(0,Math.min(100,Math.round((t.p||0)*100))), eta=duration(t.eta);
    const task=$('#taskPanel');
    if(task) task.innerHTML=`<div class="history-head"><div><b>${esc(t.title)}</b><span>${esc(t.detail)}</span></div><div class="history-percent"><strong>${p}%</strong><small>${esc(eta||(p>0&&p<100?'ETA after first measured batch':''))}</small></div></div>${p<100?`<div class="bar history-bar"><i style="width:${p}%"></i></div>`:''}<div class="history-timing"><b>${esc(t.work||'')}</b><span>${p<100?'Work is measured from actual throughput, not a fixed guess.':''}</span></div>`;
    const ar=h.archive||{};
    $('#historyPanel').innerHTML=`<div class="history-head"><div><b>History / training diagnostics</b><span>${esc(h.message||h.phase||'idle')}</span></div><div class="history-percent"><strong>${p}%</strong><small>${esc(eta||'')}</small></div></div><div class="history-timing"><b>${h.work_total?`${num(h.work_done)} / ${num(h.work_total)} ${esc(h.work_unit||'items')}`:'No measurable background work'}</b><span>${esc(h.phase_detail||'')}</span></div><div class="history-grid"><div><b>${num(ar.n||0)}</b><span>archived changes</span></div><div><b>${Number(ar.days||0).toFixed(1)} d</b><span>coverage</span></div><div><b>${num(h.context_candidates||ar.entities||0)}</b><span>context candidates</span></div><div><b>${num(h.training_rows_per_second||0)}</b><span>training rows/s</span></div></div>`;
  };

  renderHome = status => { if ($('#diagnosticsPanel')?.open) oldHome(status); };
  $('#diagnosticsPanel')?.addEventListener('toggle',()=>{ if($('#diagnosticsPanel').open) oldHome(lastStatus); });

  const driver = a => { const c=a.runtime?.context_meta||{}, xs=c.primary_behavioural_drivers||[]; return c.primary_occupancy_sensor||xs[0]||c.primary_local_sensor||null; };
  const latency = a => { const r=a.runtime||{}; if(r.event_to_ack_ms!=null)return `event → device ${ms(r.event_to_ack_ms)}`; if(r.event_to_service_ms!=null)return `event → command ${ms(r.event_to_service_ms)}`; if(r.last_service_latency_ms!=null)return `HA call ${ms(r.last_service_latency_ms)}`; return r.realtime_connected?'⚡ realtime':'REST fallback'; };
  const controlReady = a => Boolean((a.runtime?.training_state||a.training_state)==='qualified' && (a.control_qualification?.passed ?? true) && (a.control_review?.ready ?? true));
  const controlBlockReason = a => {
    if ((a.runtime?.training_state||a.training_state)!=='qualified') return 'Complete training and the historical benchmark first.';
    if (a.control_review && !a.control_review.ready) return a.control_review.approval_required && !a.control_review.approved ? 'Review this generic target in Settings before Control.' : 'Device capabilities changed; review the target again.';
    if (a.control_qualification && !a.control_qualification.passed) return a.control_qualification.reason||'More held-out evidence is required for Control.';
    return '';
  };
  const human = a => {
    const r=a.runtime||{}, training=r.training_state||a.training_state||'paused', reason=String(r.decision_reason||''), state=String(r.decision_state||'idle');
    if(training==='training') return ['training',`Learning ${Math.round(Number(a.training_progress||0)*100)}%`,'Historical replay and validation are running.'];
    if(training==='needs_retrain') return ['waiting','Needs training','Policy inputs changed. Press Train.'];
    if(training==='waiting') return ['waiting','Ready to train','Training has not started yet.'];
    if(training==='paused'&&a.mode==='paused') return ['paused','Paused','This agent is not making decisions.'];
    if(a.mode==='shadow') return ['shadow','Observing','Shadow calculates decisions but sends no Home Assistant services.'];
    if(a.control_review && !a.control_review.ready) return ['waiting','Control review required',controlBlockReason(a)];
    if(a.control_qualification && !a.control_qualification.passed) return ['waiting','More validation required',controlBlockReason(a)];
    if(state==='error'||/^(service|takeover|unavailable|limits):/.test(reason)) return ['error','Problem',reason.replace(/^[^:]+:\s*/, '')||'Control path failed.'];
    if(reason.startsWith('manual:')) return ['hold','Manual control','Your manual setting has priority.'];
    if(reason.startsWith('duplicate:')) return ['hold','State correct','The device is already in the desired state.'];
    if(reason.startsWith('acknowledgement:')) return ['waiting','Applying change','Waiting for the device to confirm the command.'];
    if(/^(cooldown|retry):/.test(reason)) return ['waiting','Waiting','A short protection interval is active.'];
    if(/^(context|state|settings|expired):/.test(reason)) return ['waiting','Recalculating','Context changed; a fresh decision will be made.'];
    if(reason.startsWith('confidence:')) return ['hold','Still learning','Confidence is below the Control threshold.'];
    if(reason.startsWith('support:')) return ['hold','Still learning','There is not enough similar history yet.'];
    if(reason.startsWith('novelty:')) return ['hold','Unusual situation','No action: this context is outside the learned distribution.'];
    if(state==='acted'||r.intent?.status==='ACCEPTED') return ['acted','Command sent',r.pending_feedback?'Waiting for feedback.':'The requested change was sent.'];
    if(state==='hold') return ['hold','State correct','No action is needed.'];
    return ['idle','Ready','Waiting for the next relevant Home Assistant event.'];
  };

  const detailsHtml = a => {
    const r=a.runtime||{}, selected=(r.selected_context_entities||[]).slice(0,12).join(' · ')||'—';
    const q=a.control_qualification||{}, review=a.control_review||{}, lease=a.control_lease;
    const qText=q.reason?`${esc(q.reason)} · observed ${pct(q.observed_score||0)} · 95% lower bound ${pct(q.lower_bound||0)}`:'—';
    const perAction=Object.entries(q.per_action||{}).map(([k,v])=>`${esc(k)}: ${pct(v.accuracy||0)} (${v.correct||0}/${v.samples||0}), lower ${pct(v.lower_bound||0)}`).join(' · ')||'—';
    const reviewText=review.approval_required?(review.ready?'approved':'review required / invalidated'):'standard device profile';
    const leaseText=lease?`active · ${Number((lease.disabled_automations||[]).length)} previous controller(s) held`:'none';
    return `<div class="detail"><b>Target:</b> ${esc(a.target_entity)} · ${esc(a.target_property)}<br><b>Eksperymenty:</b> ${esc(r.experiments?.config?.enabled ? r.experiments.reason : "wyłączone")}<br><b>Internal reason:</b> ${esc(r.decision_reason||'—')}<br><b>Behaviour benchmark:</b> ${a.benchmark_score==null?'—':pct(a.benchmark_score)} · ${num(a.benchmark_samples||0)} held-out<br><b>Control qualification:</b> ${qText}<br><b>Per-action validation:</b> ${perAction}<br><b>Control review:</b> ${esc(reviewText)}<br><b>Control lease:</b> ${esc(leaseText)}<br><b>Support / novelty:</b> ${pct(r.historical_support||0)} / ${pct(r.context_novelty??1)}<br><b>Latency:</b> HA call ${ms(r.last_service_latency_ms)} · ACK ${r.ack_latency_seconds==null?'—':ms(Number(r.ack_latency_seconds)*1000)}<br><b>Timing:</b> ACK ${num(r.timing?.acknowledgement||0)} s · settling ${num(r.timing?.settling||0)} s · action interval ${num(a.action_interval||0,1)} s<br><b>Primary sensor:</b> ${esc(driver(a)||'—')}<br><b>Selected context:</b> ${esc(selected)}<br><b>Model:</b> ${esc(r.model||'—')}<br><b>Last HA service:</b> ${esc(r.last_service||'—')}${r.last_service_error?' · '+esc(r.last_service_error):''}</div>`;
  };

  const create = a => {
    const el=document.createElement('article'); el.className='agent card'; el.dataset.agentId=a.id;
    el.innerHTML=`<div class="agent-head"><div class="agent-title"><h3><span data-p0="name"></span> <span data-p0="badge"></span></h3><div class="target" data-p0="target"></div></div><span class="mode" data-p0="mode"></span></div><div class="agent-primary"><div class="state-metric current"><span>Current</span><b data-p0="current"></b></div><div class="state-metric desired"><span>Desired</span><b data-p0="desired"></b></div><div><span>Confidence</span><b data-p0="confidence"></b></div></div><div class="decision" data-p0="decision"><b data-p0="state"></b><span data-p0="detail"></span></div><div class="agent-status"><span data-p0="driver"></span><span data-p0="latency"></span><span data-p0="training"></span></div><details class="agent-details"><summary><span>Details</span><span class="details-chevron">⌄</span></summary><div data-p0="details"></div></details><div class="actions"><button class="ghost" data-mode="shadow">Shadow</button><button class="ghost" data-mode="control">Control</button><button class="ghost" data-mode="paused">Paused</button><button class="ghost resume" data-a="train">Train</button><button class="ghost resume" data-a="resume">Resume</button><button class="ghost" data-a="settings">Settings</button><button class="ghost" data-a="verify">Verify control</button><button class="ghost" data-a="experiments">Eksperymenty</button><button class="ghost rebuild" data-a="rebuild">Rebuild</button><button class="ghost danger" data-a="delete">Delete</button></div>`;
    el.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>setMode(a.id,b.dataset.mode));
    el.querySelector('[data-a=experiments]').onclick=()=>openExperiments(a.id); el.querySelector('[data-a=train]').onclick=()=>trainAgent(a.id); el.querySelector('[data-a=resume]').onclick=()=>resumeLearning(a.id); el.querySelector('[data-a=settings]').onclick=()=>editAgent(a.id); el.querySelector('[data-a=verify]').onclick=()=>verifyControl(a.id); el.querySelector('[data-a=rebuild]').onclick=()=>resetLearning(a.id); el.querySelector('[data-a=delete]').onclick=()=>removeAgent(a.id);
    const d=el.querySelector('details'); d.open=openAgentDetails.has(String(a.id)); d.ontoggle=()=>{ if(d.open)openAgentDetails.add(String(a.id));else openAgentDetails.delete(String(a.id));persistOpenAgentDetails(); const cur=lastAgents.find(x=>x.id===a.id); if(d.open&&cur)el.querySelector('[data-p0=details]').innerHTML=detailsHtml(cur); };
    return el;
  };

  const update = (el,a) => {
    const r=a.runtime||{}, training=r.training_state||a.training_state||'paused', qualified=training==='qualified', conf=qualified&&r.last_prediction!=null?Number(r.last_confidence||0):Number(a.benchmark_score??r.last_confidence??0);
    text(el,'name',a.name); text(el,'badge',(a.auto_created?'AUTO · ':'')+training.toUpperCase()); text(el,'target',`${a.target_entity} · ${a.target_property}`); text(el,'mode',a.mode.toUpperCase()); text(el,'current',currentValue(a)); text(el,'desired',prediction(a)); text(el,'confidence',pct(conf)); text(el,'driver',driver(a)?`Sensor: ${driver(a)}`:'Sensor: learning context'); text(el,'latency',latency(a)); text(el,'training',training==='training'?`training ${Math.round(Number(a.training_progress||0)*100)}%`:training);
    const experimentButton=el.querySelector('[data-a=experiments]'); experimentButton.classList.toggle('active',Boolean(r.experiments?.config?.enabled)); experimentButton.textContent=r.experiments?.active?'Eksperyment: obserwacja':r.experiments?.config?.enabled?'Eksperymenty ON':'Eksperymenty';
    el.querySelector('[data-p0=mode]').className=`mode ${a.mode}`;
    const [tone,title,detail]=r.experiments?.active?.kind==='probe' ? ['acted','Eksperyment w toku','Pewność dotyczy zwykłej predykcji. Trwa obserwacja niewielkiej zmiany nastawy.'] : human(a), box=el.querySelector('[data-p0=decision]'); box.className=`decision ${tone}`; text(el,'state',title); text(el,'detail',detail);
    el.classList.toggle('paused-agent',['paused','waiting','needs_retrain'].includes(training)); el.classList.toggle('training-agent',training==='training');
    el.querySelectorAll('[data-mode]').forEach(b=>{
      b.classList.toggle('active',b.dataset.mode===a.mode);
      if(b.dataset.mode==='control') { const why=controlBlockReason(a); b.disabled=!controlReady(a); b.title=why; }
    });
    el.querySelector('[data-a=train]').hidden=!['waiting','needs_retrain'].includes(training); el.querySelector('[data-a=resume]').hidden=training!=='paused'; el.querySelector('[data-a=verify]').disabled=a.mode!=='control'||!controlReady(a); el.querySelector('[data-a=rebuild]').disabled=training==='training'||['waiting','needs_retrain'].includes(training);
    if(el.querySelector('details').open){const body=el.querySelector('[data-p0=details]'),html=detailsHtml(a);if(body.dataset.snap!==html){body.innerHTML=html;body.dataset.snap=html;}}
  };

  renderAgents = () => {
    const root=$('#agents'), existing=new Set(lastAgents.map(a=>String(a.id)));
    for(const [id,node] of nodes)if(!existing.has(String(id))){node.remove();nodes.delete(id);}
    for(const a of lastAgents){let node=nodes.get(a.id);if(!node){node=create(a);nodes.set(a.id,node);root.appendChild(node);}update(node,a);node.hidden=true;}
    const shown=sortedFilteredAgents(); shown.forEach(a=>{const n=nodes.get(a.id);n.hidden=false;root.appendChild(n);});
    $('#agentCount').textContent=`${lastAgents.length} configured · ${shown.length} shown`;
    let empty=root.querySelector('.p0-empty');if(!shown.length){if(!empty){empty=document.createElement('div');empty.className='empty card p0-empty';root.appendChild(empty);}empty.innerHTML=`<b>No matching agents</b><span>${esc(lastAgents.length?'Change the search filter.':'Discovery will add active devices; training starts manually.')}</span>`;}else if(empty)empty.remove();
  };

  const oldBounds=updateBounds;
  updateBounds = () => { oldBounds(); const f=$('#agentForm'), d=String($('#targetEntity').value||'').split('.')[0]; f.action_interval.value=['light','switch','input_boolean','media_player','select','input_select'].includes(d)?1:['fan','cover'].includes(d)?2:30; };
})();
