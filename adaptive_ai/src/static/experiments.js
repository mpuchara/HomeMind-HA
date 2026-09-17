/* Explore reuses the existing Experiments residual learner and Sensor Tournament. */
(() => {
  const focusNames={presence:'Presence boundary',environment:'Environment boundary',devices:'Other device activity'};
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const request=async(path,opts={})=>{
    const method=String(opts.method||'GET').toUpperCase();
    const timeout=(method==='GET'||method==='HEAD')?{adaptiveAiTimeoutMs:20000}:{};
    const r=await fetch(path,{headers:{'Content-Type':'application/json'},...timeout,...opts});
    let body={};try{body=await r.json();}catch(_){ }
    if(!r.ok)throw Error(body.error||`HTTP ${r.status}`);return body;
  };
  const pct=v=>v==null?'—':`${Number(v)>=0?'+':''}${(Number(v)*100).toFixed(1)}%`;

  const dialog=document.createElement('dialog');
  dialog.id='exploreDialog';
  dialog.setAttribute('aria-labelledby','exploreTitle');
  document.body.appendChild(dialog);

  function sessionHtml(session){
    if(!session)return '<p class="muted">No Explore session exists for this parent generation yet.</p>';
    const evidence=session.result?.evidence||{};
    return `<div class="context-all"><b>Current Explore child · Gen ${esc(session.child_generation_number??'—')}</b>
      <span>${esc(session.mode==='free'?'Free exploration':'Targeted sensor')} · ${esc(session.status)}</span>
      <span>${esc(session.result_message||'Collecting evidence')}</span>
      ${session.targeted_sensor?`<span>Sensor: ${esc(session.targeted_sensor)} · samples ${esc(evidence.samples??0)} · gain ${esc(pct(evidence.gain))}${evidence.sensor_quality==null?'':` · quality ${(Number(evidence.sensor_quality)*100).toFixed(1)}%`}</span>`:''}
      <span>Candidate dispatch: forbidden · ${session.mode==='free'?'physical probes owned by Live/Executor':'passive Candidate Shadow'}</span></div>`;
  }

  function entityOptions(entities,selected){
    const rows=(entities||[]).filter(e=>e&&e.entity_id).sort((a,b)=>String(a.entity_id).localeCompare(String(b.entity_id)));
    return rows.map(e=>`<option value="${esc(e.entity_id)}" ${String(e.entity_id)===String(selected||'')?'selected':''}>${esc(e.entity_id)}${e.name&&e.name!==e.entity_id?` · ${esc(e.name)}`:''}</option>`).join('');
  }

  window.openExplore=async generationRef=>{
    const ref=String(generationRef);
    try{
      const [explore,subject,entities]=await Promise.all([
        request(`api/agent-workflow/${encodeURIComponent(ref)}/explore`),
        request(`api/agent-workflow/${encodeURIComponent(ref)}/status`),
        request('api/entities'),
      ]);
      const cfg={focus:'presence',intensity:.2,interval:900,daily_budget:6,observation_seconds:30,max_step:subject.target_property==='power'?1:subject.target_property==='temperature'?.5:5,...(explore.free_config||{})};
      const previousSensor=explore.session?.targeted_sensor||'';
      dialog.innerHTML=`<form method="dialog" class="experiment-form">
        <div class="teach-head"><div><div class="eyebrow">GENERATION EXPLORE</div><h2 id="exploreTitle">Explore · ${esc(subject.name)} · Gen ${esc(subject.generation_number)}</h2></div><button type="button" class="icon" data-close>×</button></div>
        <p>Explore never changes Gen ${esc(subject.generation_number)} in place. A direct child Candidate owns the result.</p>
        <div class="actions" data-explore-modes><button type="button" class="primary" data-mode="free">Free exploration</button><button type="button" class="ghost" data-mode="targeted_sensor">Targeted sensor</button></div>

        <section data-pane="free">
          <div class="context-all"><b>Free exploration</b><span>Uses the existing Experiments residual learner and all of its current safety guards.</span></div>
          <label>Exploration focus<select name="focus">${Object.entries(focusNames).map(([v,n])=>`<option value="${v}" ${v===cfg.focus?'selected':''}>${esc(n)}</option>`).join('')}</select></label>
          <div class="two"><label>Intensity (%)<input name="intensity" type="number" min="5" max="35" step="1" value="${Math.round(Number(cfg.intensity)*100)}" required></label>
          <label>Maximum setting step<input name="max_step" type="number" min="0.01" max="100" step="any" value="${Number(cfg.max_step)}" required></label></div>
          <div class="two"><label>Interval between trials (min)<input name="interval" type="number" min="5" max="1440" step="any" value="${Number(cfg.interval)/60}" required></label>
          <label>Daily budget<input name="daily_budget" type="number" min="1" max="24" step="1" value="${Number(cfg.daily_budget)}" required></label></div>
          <label>Observation window (s)<input name="observation_seconds" type="number" min="10" max="3600" step="1" value="${Number(cfg.observation_seconds)}" required></label>
          <p class="muted">Confidence, historical support, novelty, device limits, cooldowns and legal-value guards remain unchanged. ${explore.active_probe_available?'Active probes can run only through Live → ActionIntent → Executor.':'Live is not currently eligible for an active probe. Candidate will never dispatch a physical action; Explore waits rather than bypassing Control safety.'}</p>
        </section>

        <section data-pane="targeted_sensor" hidden>
          <div class="context-all"><b>Targeted sensor</b><span>Prioritize one hypothesis: does this HA entity add incremental predictive value?</span></div>
          <label>HA entity<select name="sensor_entity" required><option value="">Choose an entity…</option>${entityOptions(entities,previousSensor)}</select></label>
          <p class="muted">Manual selection is only challenger priority. The sensor still needs normal availability/quality, future prequential samples, predictive gain and Sensor Tournament safety gates. A Candidate never dispatches a probe; this path is passive Shadow.</p>
          <p class="muted">If sufficient future evidence shows no incremental value, Explore reports exactly <b>no measurable gain</b>.</p>
        </section>

        ${sessionHtml(explore.session)}
        <p data-explore-error role="alert"></p>
        <div class="dialog-actions"><button type="button" class="ghost" data-cancel>Cancel</button><button type="submit" class="primary" data-start>Start Explore · create child Candidate</button></div>
      </form>`;
      const form=dialog.querySelector('form');
      let mode='free';
      const setMode=next=>{
        mode=next;
        dialog.querySelectorAll('[data-pane]').forEach(p=>p.hidden=p.dataset.pane!==mode);
        dialog.querySelectorAll('[data-mode]').forEach(b=>{b.className=b.dataset.mode===mode?'primary':'ghost';});
        form.elements.sensor_entity.required=mode==='targeted_sensor';
      };
      dialog.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
      dialog.querySelector('[data-close]').onclick=()=>dialog.close();
      dialog.querySelector('[data-cancel]').onclick=()=>dialog.close();
      form.addEventListener('invalid',event=>{const pane=event.target.closest('[data-pane]');if(pane?.hidden)setMode(pane.dataset.pane);},true);
      form.onsubmit=async event=>{
        event.preventDefault();
        const f=form.elements;
        const body=mode==='free'?{
          mode:'free',
          config:{focus:f.focus.value,intensity:Number(f.intensity.value)/100,max_step:Number(f.max_step.value),interval:Number(f.interval.value)*60,daily_budget:Number(f.daily_budget.value),observation_seconds:Number(f.observation_seconds.value)}
        }:{mode:'targeted_sensor',sensor_entity:f.sensor_entity.value};
        const button=dialog.querySelector('[data-start]');button.disabled=true;
        dialog.querySelector('[data-explore-error]').textContent='';
        try{
          await request(`api/agent-workflow/${encodeURIComponent(ref)}/explore`,{method:'POST',body:JSON.stringify(body)});
          dialog.close();
          try{await window.refreshCandidates?.();}catch(_){}
          try{window.renderAgents?.();}catch(_){}
        }catch(error){dialog.querySelector('[data-explore-error]').textContent=error.message;}
        finally{button.disabled=false;}
      };
      setMode(explore.session?.mode==='targeted_sensor'?'targeted_sensor':'free');
      if(!dialog.open)dialog.showModal();
    }catch(error){alert(`Explore failed: ${error.message}`);}
  };

  // Compatibility entry point for old cards cached in a browser during upgrade.
  window.openExperiments=id=>window.openExplore(id);
})();
