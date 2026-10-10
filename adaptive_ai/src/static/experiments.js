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
      <span>${esc(session.mode==='free'?'Free exploration':session.mode==='additional_signal'?'Dodatkowa jasność':'Targeted sensor')} · ${esc(session.status)}</span>
      <span>${esc(session.result_message||'Collecting evidence')}</span>
      ${session.targeted_sensor?`<span>Sensor: ${esc(session.targeted_sensor)}${session.mode==='additional_signal'?'':` · samples ${esc(evidence.samples??0)} · gain ${esc(pct(evidence.gain))}`}${evidence.sensor_quality==null?'':` · quality ${(Number(evidence.sensor_quality)*100).toFixed(1)}%`}</span>`:''}
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
      const defaults=explore.illumination_defaults||{};
      const signal=explore.additional_signal||{purpose:'context',hysteresis:0,max_age_seconds:900};
      dialog.innerHTML=`<form method="dialog" class="experiment-form">
        <div class="teach-head"><div><div class="eyebrow">GENERATION EXPLORE</div><h2 id="exploreTitle">Explore · ${esc(subject.name)} · Gen ${esc(subject.generation_number)}</h2></div><button type="button" class="icon" data-close>×</button></div>
        <p>Explore never changes Gen ${esc(subject.generation_number)} in place. A direct child Candidate owns the result.</p>
        <div class="actions" data-explore-modes><button type="button" class="primary" data-mode="free">Free exploration</button><button type="button" class="ghost" data-mode="targeted_sensor">Targeted sensor</button>${subject.target_property==='power'?'<button type="button" class="ghost" data-mode="additional_signal">Dodatkowa jasność</button>':''}</div>

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
        <section data-pane="additional_signal" hidden>
          <label>Czujnik jasności<select name="signal_entity"><option value="">Wybierz czujnik…</option>${entityOptions(entities.filter(e=>String(e.entity_id).startsWith('sensor.')),signal.entity_id)}</select></label>
          <label>Cel<select name="signal_purpose"><option value="context" ${signal.purpose==='context'?'selected':''}>Używaj jako dodatkowy kontekst treningu</option><option value="avoid_bright_on" ${signal.purpose==='avoid_bright_on'?'selected':''}>Pomijaj włączenie, gdy jest wystarczająco jasno</option></select></label>
          <div data-signal-goal><label>Wartość z istniejącej konfiguracji<select name="signal_reference"></select></label><p class="muted" data-signal-source></p>
          <label>Próg jasności — możesz zmienić<input name="signal_threshold" type="number" step="any" min="0" value="${esc(signal.threshold??'')}"></label>
          <label>Margines wokół progu<input name="signal_hysteresis" type="number" step="any" min="0" value="${esc(signal.hysteresis??0)}"></label></div>
          <label>Maksymalny wiek pomiaru (s)<input name="signal_age" type="number" min="1" max="86400" value="${esc(signal.max_age_seconds??900)}"></label>
          <p>Próg podaj w jednostkach czujnika: lx albo surowa skala LD2410 0–255. Wybierz odczyt reprezentujący oświetlenie tego miejsca; światło zapalone w kuchni może zmieniać jego wartość.</p>
          <p>Powstanie nowy Candidate. Preferencja pomija nowe włączenia; nie gasi światła podczas pobytu. Brak świeżego pomiaru pozostawia decyzję zwykłemu modelowi. Przyjęcie zmiany wymaga porównania w Shadow, także w ciemności.</p>
        </section>
        <div class="dialog-actions"><button type="button" class="ghost" data-cancel>Cancel</button><button type="submit" class="primary" data-start>Start Explore · create child Candidate</button></div>
      </form>`;
      const form=dialog.querySelector('form');
      let mode='free';
      const setMode=next=>{
        mode=next;
        dialog.querySelectorAll('[data-pane]').forEach(p=>{p.hidden=p.dataset.pane!==mode;p.querySelectorAll('input,select').forEach(e=>e.disabled=p.hidden);});
        dialog.querySelectorAll('[data-mode]').forEach(b=>{b.className=b.dataset.mode===mode?'primary':'ghost';});
        form.elements.sensor_entity.required=mode==='targeted_sensor';
        form.elements.signal_entity.required=mode==='additional_signal';
        const goal=mode==='additional_signal'&&form.elements.signal_purpose.value==='avoid_bright_on';
        form.elements.signal_threshold.required=goal;
        form.elements.signal_hysteresis.required=goal;
        form.elements.signal_age.required=mode==='additional_signal';
        dialog.querySelector('[data-signal-goal]').hidden=!goal;
      };
      const drafts=new Map();
      let previousSignal=form.elements.signal_entity.value;
      const saveDraft=()=>drafts.set(previousSignal,{threshold:form.elements.signal_threshold.value,
        hysteresis:form.elements.signal_hysteresis.value,age:form.elements.signal_age.value,reference:form.elements.signal_reference.value});
      const sourceName=source=>`${source.name||source.id} · ${source.kind==='automation'?'automatyzacja':'agent'}${source.bound_entity?` · ${source.bound_entity}`:''}${source.config_status==='cached'?' · ostatnia odczytana konfiguracja':''}`;
      const updateSource=()=>{
        const reference=defaults[form.elements.signal_entity.value];
        const index=form.elements.signal_reference.value;
        const choice=index===''?null:reference?.alternatives?.[Number(index)];
        dialog.querySelector('[data-signal-source]').textContent=choice?
          `Źródło: ${sourceName(choice.source)}. Wartość jest kopiowana; późniejsze zmiany źródła jej nie zmienią.${reference.conflicting?' Znaleziono różne progi — dostępne są powyżej.':''}`:
          reference?'Własna wartość. Możesz wybrać jeden z zapisanych progów.':'Nie znaleziono progu dla tego czujnika. Wpisz własną wartość.';
      };
      const applyReference=()=>{
        const choice=defaults[form.elements.signal_entity.value]?.alternatives?.[Number(form.elements.signal_reference.value)];
        if(form.elements.signal_reference.value!==''&&choice){
          form.elements.signal_threshold.value=choice.config.threshold;
          form.elements.signal_hysteresis.value=choice.config.hysteresis;
          form.elements.signal_age.value=choice.config.max_age_seconds;
        }
        updateSource();
      };
      const selectSignal=initial=>{
        const eid=form.elements.signal_entity.value;
        const reference=defaults[eid];
        form.elements.signal_reference.innerHTML='<option value="">Własna wartość</option>'+(reference?.alternatives||[]).map((r,i)=>
          `<option value="${i}">${esc(r.config.threshold)} ${esc(r.config.unit==='raw'?'(skala czujnika)':r.config.unit)} · ${esc(sourceName(r.source))}</option>`).join('');
        const draft=drafts.get(eid);
        if(draft){
          form.elements.signal_threshold.value=draft.threshold;form.elements.signal_hysteresis.value=draft.hysteresis;
          form.elements.signal_age.value=draft.age;form.elements.signal_reference.value=draft.reference;
        }else if(initial&&signal.entity_id===eid&&signal.threshold!=null&&reference?.source.kind==='selected_agent'){
          form.elements.signal_reference.value='0';
        }else{
          form.elements.signal_reference.value=reference?'0':'';
          form.elements.signal_threshold.value='';form.elements.signal_hysteresis.value=0;form.elements.signal_age.value=900;
          applyReference();
        }
        previousSignal=eid;updateSource();
      };
      form.elements.signal_entity.onchange=()=>{saveDraft();selectSignal(false);};
      form.elements.signal_reference.onchange=applyReference;
      [form.elements.signal_threshold,form.elements.signal_hysteresis,form.elements.signal_age].forEach(e=>e.oninput=()=>{form.elements.signal_reference.value='';updateSource();});
      selectSignal(true);
      form.elements.signal_purpose.onchange=()=>setMode(mode);
      setMode(mode);
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
        }:mode==='targeted_sensor'?{mode:'targeted_sensor',sensor_entity:f.sensor_entity.value}:{
          mode:'additional_signal',additional_signal:{version:1,entity_id:f.signal_entity.value,purpose:f.signal_purpose.value,
          ...(defaults[f.signal_entity.value]?.config.unit?{unit:defaults[f.signal_entity.value].config.unit}:{}),
          max_age_seconds:Number(f.signal_age.value),hysteresis:f.signal_purpose.value==='context'?0:Number(f.signal_hysteresis.value),
          ...(f.signal_purpose.value==='avoid_bright_on'?{threshold:Number(f.signal_threshold.value)}:{})}};
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
      setMode(['targeted_sensor','additional_signal'].includes(explore.session?.mode)?explore.session.mode:'free');
      if(!dialog.open)dialog.showModal();
    }catch(error){alert(`Explore failed: ${error.message}`);}
  };

  // Compatibility entry point for old cards cached in a browser during upgrade.
  window.openExperiments=id=>window.openExplore(id);
})();
