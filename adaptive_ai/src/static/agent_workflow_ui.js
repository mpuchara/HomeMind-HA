// Generation-first agent actions: Autonomous, Correct, Explore (next PR), Change decision, Settings.
(()=>{
  const COLORS={current:'#73dbec',parent:'#c2a6ff',candidate:'#ff9f43',correct:'#ffd166'};
  const html=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});let b={};try{b=await r.json();}catch(_){ }if(!r.ok){const e=Error(b.error||`HTTP ${r.status}`);e.status=r.status;throw e;}return b;};
  const fmt=(subject,v)=>v==null?'—':subject?.target_property==='power'?(Number(v)>=.5?'ON':'OFF'):Number(v).toFixed(3).replace(/\.000$/,'');
  const local=ts=>{const d=new Date(Number(ts)*1000);return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,19);};
  const notifyError=e=>alert(e?.message||String(e));
  const refresh=async()=>{try{await window.refreshCandidates?.();}catch(_){}try{window.renderAgents?.();}catch(_){}};
  const post=(ref,action,body={})=>api(`api/agent-workflow/${encodeURIComponent(ref)}/${action}`,{method:'POST',body:JSON.stringify(body)});
  const status=ref=>api(`api/agent-workflow/${encodeURIComponent(ref)}/status`);
  // app.js declares lastAgents with top-level `let`, which is a global lexical binding,
  // not a window property. Read that binding directly so the workflow layer decorates
  // the actual current Live cards after every normal render.
  const liveAgents=()=>{try{return Array.isArray(lastAgents)?lastAgents:[];}catch(_){return [];}};
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const requestStorageKey=ref=>`adaptive-ai:correct-request:${String(ref)}`;
  const rememberRequest=(ref,id)=>{try{sessionStorage.setItem(requestStorageKey(ref),String(id));}catch(_){}};
  const pendingRequest=ref=>{try{return sessionStorage.getItem(requestStorageKey(ref));}catch(_){return null;}};
  const clearRequest=ref=>{try{sessionStorage.removeItem(requestStorageKey(ref));}catch(_){}};
  const newRequestId=()=>{try{if(globalThis.crypto?.randomUUID)return crypto.randomUUID();}catch(_){}return `correct-${Date.now()}-${Math.random().toString(16).slice(2)}`;};
  const provisionalSubject=generationRef=>{
    const a=liveAgents().find(x=>String(x.id)===String(generationRef));
    if(!a)return {name:'Agent',generation_number:'…',target_property:'power',min_value:0,max_value:1,current:null};
    return {
      name:a.name||a.id,generation_number:a.generation_number??a.generation??'…',
      target_property:a.target_property,min_value:a.min_value,max_value:a.max_value,
      current:a.runtime?.current_value??null,
    };
  };

  window.workflowAutonomous=async(ref,button)=>{
    if(button)button.disabled=true;
    try{
      const s=await status(ref);
      if(!confirm(`Autonomous: utworzyć Gen ${Number(s.generation_number)+1} jako child Gen ${s.generation_number}?\n\nParent pozostanie bez zmian; child zachowa jego model i będzie kontynuował naukę tylko na nowej historii.`))return;
      const out=await post(ref,'autonomous');
      await refresh();
      return out;
    }catch(e){notifyError(e);}finally{if(button)button.disabled=false;}
  };

  window.workflowChangeDecision=async(ref,button)=>{
    if(button)button.disabled=true;
    try{
      const s=await status(ref);
      let desired;
      if(s.target_property!=='power'){
        const raw=prompt(`Nowe Desired dla ${s.name} (${s.min_value}–${s.max_value}):`,s.observed_desired??s.current??'');
        if(raw===null)return;
        desired=Number(String(raw).replace(',','.'));
        if(!Number.isFinite(desired))throw Error('Podaj poprawną liczbę');
      }
      const body=desired===undefined?{}:{desired_value:desired};
      const out=await post(ref,'change-decision',body);
      await refresh();
      return out;
    }catch(e){notifyError(e);}finally{if(button)button.disabled=false;}
  };

  window.workflowSettings=async ref=>{
    try{
      const s=await status(ref);
      if(s.generation_type==='live'&&typeof window.editAgent==='function')return window.editAgent(s.agent_id);
      alert(`Settings · Gen ${s.generation_number}\n\nCandidate dziedziczy konfigurację parenta.\nModel revision: ${s.model_revision||'—'}\nSchema revision: ${s.schema_revision||'—'}\n\nZmiana konfiguracji Candidate w miejscu jest celowo zablokowana; dalszy rozwój odbywa się przez child generation.`);
    }catch(e){notifyError(e);}
  };

  const dialog=document.createElement('dialog');
  dialog.id='correctDialog';
  document.body.append(dialog);
  let subject=null,ref=null,range=null,data=null,selected=null,requestSeq=0;

  const error=e=>{const n=dialog.querySelector('[data-error]');if(n)n.textContent=e?.message||String(e);else notifyError(e);};
  const setBusy=busy=>dialog.querySelectorAll('button,input').forEach(el=>{if(!el.hasAttribute('data-close'))el.disabled=!!busy;});

  function shell(){
    dialog.innerHTML=`<div class="teach-head"><h2 data-title>Correct: ${html(subject.name)} · Gen ${subject.generation_number}</h2><button class="ghost" data-close>Zamknij</button></div>
      <p>Kliknij wykres, aby wskazać moment, albo przeciągnij poziomo po wykresie, aby zaznaczyć zakres i go przybliżyć. Kółko myszy przybliża wokół kursora. Correct nie zmienia Gen ${subject.generation_number} w miejscu — po zatwierdzeniu utworzy child Candidate.</p>
      <div class="teach-range"><label>Od<input data-start type="datetime-local" step="1"></label><label>Do<input data-end type="datetime-local" step="1"></label><button class="ghost" data-load>Pokaż</button><button class="ghost" data-retry>Połącz ponownie</button><button class="ghost" data-prev>←</button><button class="ghost" data-next>→</button><button class="ghost" data-in>+</button><button class="ghost" data-out>−</button></div>
      <p class="teach-legend" data-legend></p>
      <div class="teach-chart" data-chart></div><p data-status role="status"></p><p data-error role="alert"></p>
      <form data-point><label>Wybrany moment<input data-time type="datetime-local" step="1" required></label><button type="button" class="ghost" data-inspect>Sprawdź punkt</button><p data-point-info>Wybierz moment na wykresie.</p><label>Poprawne Desired<input data-value type="number" step="any" required></label><button class="primary" type="submit" data-save disabled>Dodaj Correct</button><button class="ghost" type="button" data-undo>Cofnij ostatni Correct</button></form>
      <div class="dialog-actions"><button class="primary" type="button" data-apply>Apply Correct · create child Candidate</button></div>
      <p>Wykres używa wyłącznie observed generation decision history. Candidate jest porównywany tylko z bezpośrednim parentem; brak runtime pozostaje luką i nie jest odtwarzany obecną policy.</p>`;
    dialog.querySelector('[data-close]').onclick=()=>dialog.close();
    dialog.querySelector('[data-load]').onclick=()=>{const a=Date.parse(dialog.querySelector('[data-start]').value)/1000,b=Date.parse(dialog.querySelector('[data-end]').value)/1000;if(Number.isFinite(a)&&Number.isFinite(b)){range={start:a,end:b};load();}};
    dialog.querySelector('[data-retry]').onclick=()=>refreshSubjectAndLoad();
    for(const [k,f] of [['in',.5],['out',2]])dialog.querySelector(`[data-${k}]`).onclick=()=>zoom(f);
    for(const [k,d] of [['prev',-1],['next',1]])dialog.querySelector(`[data-${k}]`).onclick=()=>shift(d);
    dialog.querySelector('[data-inspect]').onclick=()=>inspect(Date.parse(dialog.querySelector('[data-time]').value)/1000);
    dialog.querySelector('[data-time]').oninput=()=>{selected=null;dialog.querySelector('[data-save]').disabled=true;};
    dialog.querySelector('[data-undo]').onclick=undo;
    dialog.querySelector('[data-apply]').onclick=apply;
    dialog.querySelector('[data-point]').onsubmit=save;
  }

  async function refreshSubjectAndLoad(){
    if(!dialog.open)return;
    dialog.querySelector('[data-error]').textContent='';
    dialog.querySelector('[data-status]').textContent='Łączę z Adaptive AI i ładuję historię…';
    try{
      const fresh=await status(ref);
      if(!dialog.open)return;
      subject=fresh;
      const title=dialog.querySelector('[data-title]');
      if(title)title.textContent=`Correct: ${subject.name} · Gen ${subject.generation_number}`;
      await load();
    }catch(e){
      if(!dialog.open)return;
      error(e);
      dialog.querySelector('[data-status]').textContent='Backend jest zajęty. Okno Correct pozostaje otwarte — użyj „Połącz ponownie”, gdy odczyt wróci.';
    }
  }

  async function monitorCorrectRequest(requestId,{postFailed=false}={}){
    let seen=false,missing=0,lastError=null;
    for(let attempt=0;attempt<40&&dialog.open;attempt++){
      try{
        const state=await api(`api/agent-workflow-requests/${encodeURIComponent(requestId)}`);
        seen=true;
        const statusNode=dialog.querySelector('[data-status]');
        if(state.state==='done'){
          clearRequest(ref);
          if(statusNode)statusNode.textContent='Correct zapisany. Child Candidate został utworzony lub zaktualizowany.';
          await refresh();
          setBusy(false);
          if(dialog.open)dialog.close();
          return state;
        }
        if(state.state==='failed'){
          clearRequest(ref);
          const terminal=Error(state.error||'Correct request failed');terminal.workflowTerminal=true;throw terminal;
        }
        if(statusNode)statusNode.textContent=state.state==='processing'
          ?'Correct jest zapisany trwale · tworzę child Candidate…'
          :'Correct jest zapisany trwale · oczekuje na obsługę…';
      }catch(e){
        if(e?.workflowTerminal)throw e;
        lastError=e;
        if(e?.status===404)missing+=1;
        const statusNode=dialog.querySelector('[data-status]');
        if(statusNode)statusNode.textContent=seen
          ?'Correct jest zapisany trwale. Backend jest chwilowo zajęty; sprawdzę ponownie…'
          :`Sprawdzam trwałe żądanie Correct ${requestId.slice(0,8)}…`;
      }
      await sleep(500);
    }
    if(!seen&&postFailed&&missing>=10){
      clearRequest(ref);
      throw lastError||Error('Nie udało się potwierdzić zapisu Correct');
    }
    const statusNode=dialog.querySelector('[data-status]');
    if(statusNode)statusNode.textContent='Correct ma trwały request ID i będzie przetwarzany w tle. Możesz zamknąć okno i wrócić później.';
    setBusy(false);
    const applyButton=dialog.querySelector('[data-apply]');
    if(applyButton)applyButton.disabled=true;
    return null;
  }

  window.openWorkflowCorrect=async generationRef=>{
    ref=String(generationRef);subject=provisionalSubject(ref);
    const end=Date.now()/1000;range={start:end-600,end};selected=null;data=null;shell();dialog.showModal();
    const pending=pendingRequest(ref);
    if(pending){
      setBusy(true);
      monitorCorrectRequest(pending).catch(e=>{clearRequest(ref);setBusy(false);error(e);});
    }
    await refreshSubjectAndLoad();
  };

  function zoom(factor,anchor=.5){if(!Number.isFinite(range.end-range.start))return;const oldWidth=range.end-range.start,width=Math.max(10,Math.min(31*86400,oldWidth*factor)),center=range.start+oldWidth*anchor;range={start:center-width*anchor,end:center+width*(1-anchor)};load();}
  function shift(direction){const width=range.end-range.start;range={start:range.start+direction*width,end:range.end+direction*width};load();}

  function renderLegend(){
    const legend=dialog.querySelector('[data-legend]');if(!legend||!data)return;
    const series=data.series||{};
    const rows=[];
    if(series.current)rows.push([series.current.label||'Current',COLORS.current,false]);
    if(data.chart_mode==='live'&&series.live_desired)rows.push([series.live_desired.label||'Live Desired',COLORS.candidate,true]);
    if(data.chart_mode==='candidate_vs_parent'&&series.parent_desired)rows.push([series.parent_desired.label||'Parent Desired',COLORS.parent,true]);
    if(data.chart_mode==='candidate_vs_parent'&&series.candidate_desired)rows.push([series.candidate_desired.label||'Candidate Desired',COLORS.candidate,true]);
    rows.push(['Correct points',COLORS.correct,false]);
    legend.innerHTML=rows.map(([label,color,dashed])=>`<span style="color:${color}">${dashed?'┄':'●'} ${html(label)}</span>`).join('');
  }

  async function load(){
    const seq=++requestSeq,end=Math.min(range.end,Date.now()/1000);range={start:range.start,end};
    if(!Number.isFinite(range.start)||!Number.isFinite(end)||end<=range.start||end-range.start>31*86400){error(Error('Wybierz zakres od 1 sekundy do 31 dni'));return;}
    dialog.querySelector('[data-start]').value=local(range.start);dialog.querySelector('[data-end]').value=local(end);dialog.querySelector('[data-error]').textContent='';dialog.querySelector('[data-status]').textContent='Ładuję zaobserwowane decyzje generacji…';
    try{
      const out=await api(`api/agent-workflow/${encodeURIComponent(ref)}/correct-history?start=${range.start}&end=${end}`);
      if(seq!==requestSeq||!dialog.open)return;
      data=out;renderLegend();draw();
      const current=(out.series?.current?.points||[]).length,labels=(out.labels||[]).length;
      const childGaps=(out.gaps||[]).length,parentGaps=(out.parent_gaps||[]).length,gaps=childGaps+parentGaps;
      dialog.querySelector('[data-status]').textContent=`${current} obserwacji · ${labels} Correct points${gaps?` · ${gaps} luk runtime`:''}. Direct parent comparison; policy replay wyłączony.`;
      dialog.querySelector('[data-undo]').disabled=!labels;
    }catch(e){if(seq===requestSeq)error(e);}
  }

  async function inspect(ts){
    if(!Number.isFinite(ts)){error(Error('Wybierz poprawny moment'));return;}
    selected=null;dialog.querySelector('[data-save]').disabled=true;dialog.querySelector('[data-error]').textContent='';
    try{
      const p=await api(`api/agent-workflow/${encodeURIComponent(ref)}/correct-point?ts=${ts}`);
      selected=p;dialog.querySelector('[data-time]').value=local(p.ts);
      const info=dialog.querySelector('[data-point-info]');
      if(p.gap||p.desired==null){info.textContent=`Current: ${fmt(subject,p.current)} · Selected generation Desired: GAP — tej generacji nie wolno tu korygować przez wymyśloną predykcję.`;return;}
      if(p.generation_type==='candidate'){
        info.textContent=`Current: ${fmt(subject,p.current)} · ${p.parent_desired_label||'Parent Desired'}: ${fmt(subject,p.parent_desired)} · ${p.candidate_desired_label||'Candidate Desired'}: ${fmt(subject,p.candidate_desired)}${p.confidence==null?'':` · Candidate confidence ${(Number(p.confidence)*100).toFixed(1)}%`}${p.context_complete?'':' · niepełny kontekst'}`;
      }else{
        info.textContent=`Current: ${fmt(subject,p.current)} · Live Desired: ${fmt(subject,p.live_desired)}${p.confidence==null?'':` · Confidence ${(Number(p.confidence)*100).toFixed(1)}%`}${p.context_complete?'':' · niepełny kontekst'}`;
      }
      const input=dialog.querySelector('[data-value]');input.min=subject.min_value;input.max=subject.max_value;input.value=subject.target_property==='power'?(Number(p.desired)>=.5?0:1):p.desired;
      dialog.querySelector('[data-save]').disabled=!(p.current!=null&&p.context_complete&&p.desired!=null);
      draw();
    }catch(e){error(e);}
  }

  async function save(ev){
    ev.preventDefault();if(!selected)return;
    const desired=Number(dialog.querySelector('[data-value]').value);if(!Number.isFinite(desired)){error(Error('Podaj poprawne Desired'));return;}
    setBusy(true);
    try{await post(ref,'correct-label',{sample_ts:selected.ts,desired_value:desired});selected=null;await load();}
    catch(e){error(e);}finally{setBusy(false);}
  }

  async function undo(){setBusy(true);try{await post(ref,'correct-undo');selected=null;await load();}catch(e){error(e);}finally{setBusy(false);}}
  async function apply(){
    const requestId=newRequestId();
    rememberRequest(ref,requestId);
    setBusy(true);dialog.querySelector('[data-error]').textContent='';
    dialog.querySelector('[data-status]').textContent='Zapisuję trwałe żądanie Correct…';
    let postFailed=false;
    try{
      await post(ref,'correct',{request_id:requestId});
    }catch(e){
      postFailed=true;
      dialog.querySelector('[data-status]').textContent='Nie mam potwierdzenia odpowiedzi HTTP. Sprawdzam request ID zamiast ponawiać korektę…';
    }
    try{
      return await monitorCorrectRequest(requestId,{postFailed});
    }catch(e){
      clearRequest(ref);setBusy(false);error(e);
      return null;
    }
  }

  function draw(){
    const box=dialog.querySelector('[data-chart]');if(!box||!data)return;
    const series=data.series||{},allSeries=[];
    for(const key of ['current','live_desired','parent_desired','candidate_desired'])if(series[key])allSeries.push(series[key]);
    const values=allSeries.flatMap(s=>(s.points||[]).map(p=>p.value)).concat((data.labels||[]).map(p=>p.desired)).filter(v=>v!=null&&Number.isFinite(Number(v)));
    const lo=Math.min(Number(subject.min_value),...(values.length?values:[Number(subject.min_value)]));
    const hi=Math.max(Number(subject.max_value),...(values.length?values:[Number(subject.max_value)]));
    const span=Math.max(1e-6,hi-lo),start=Number(data.start??range.start),end=Number(data.end??range.end),width=Math.max(1,end-start),stale=Number(data.stale_after_seconds||95);
    const x=t=>50+930*(Number(t)-start)/width,y=v=>300-255*(Number(v)-lo)/span;
    const path=(points,expire=true)=>{let d='',active=false,last=0;for(const p of (points||[]).slice().sort((a,b)=>Number(a.ts)-Number(b.ts))){const v=p.value,ts=Number(p.ts);if(v==null||!Number.isFinite(Number(v))||(expire&&last&&ts-last>stale))active=false;if(v!=null&&Number.isFinite(Number(v))){d+=active?` H${x(ts)} V${y(v)}`:` M${x(ts)},${y(v)}`;active=true;last=ts;}}return d;};
    const rendered=[];
    if(series.live_desired)rendered.push(`<path data-series="live_desired" d="${path(series.live_desired.points)}" fill="none" stroke="${COLORS.candidate}" stroke-width="2" stroke-dasharray="8 6" stroke-linecap="round" opacity="0.95"/>`);
    if(series.parent_desired)rendered.push(`<path data-series="parent_desired" d="${path(series.parent_desired.points)}" fill="none" stroke="${COLORS.parent}" stroke-width="2" stroke-dasharray="8 6" stroke-linecap="round" opacity="0.95"/>`);
    if(series.candidate_desired)rendered.push(`<path data-series="candidate_desired" d="${path(series.candidate_desired.points)}" fill="none" stroke="${COLORS.candidate}" stroke-width="2" stroke-dasharray="8 6" stroke-linecap="round" opacity="0.95"/>`);
    if(series.current){
      const currentPath=path(series.current.points,false);
      rendered.push(`<path data-series="current-outline" d="${currentPath}" fill="none" stroke="#04111f" stroke-width="6" stroke-linejoin="round" stroke-linecap="round" opacity="0.92"/>`);
      rendered.push(`<path data-series="current" d="${currentPath}" fill="none" stroke="${COLORS.current}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>`);
    }
    const labels=(data.labels||[]).filter(r=>Number(r.sample_ts)>=start&&Number(r.sample_ts)<=end).map(r=>`<circle data-series="correct" cx="${x(r.sample_ts)}" cy="${y(r.desired)}" r="5" fill="${COLORS.correct}"><title>Correct ${html(r.desired)}</title></circle>`).join('');
    const chosen=selected&&selected.ts>=start&&selected.ts<=end?`<line x1="${x(selected.ts)}" x2="${x(selected.ts)}" y1="35" y2="305" stroke="#fff" opacity=".35"/>`:'';
    box.innerHTML=`<svg viewBox="0 0 1030 350" role="img" aria-label="Correct direct-parent generation history chart" tabindex="0">${rendered.join('')}${labels}${chosen}<rect data-selection x="50" y="25" width="0" height="285" fill="#9aa0a6" fill-opacity="0.28" stroke="#d4d7da" stroke-opacity="0.75" visibility="hidden"/><rect data-hit x="50" y="25" width="930" height="285" fill="transparent" style="cursor:crosshair;touch-action:none"/></svg>`;
    const svg=box.querySelector('svg'),hit=box.querySelector('[data-hit]'),selection=box.querySelector('[data-selection]');let down=null;
    const fraction=ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)*1030/r.width;return Math.max(0,Math.min(1,(px-50)/930));};
    const hideSelection=()=>selection.setAttribute('visibility','hidden');
    const showSelection=endFraction=>{if(down==null)return;const a=50+930*Math.min(down,endFraction),b=50+930*Math.max(down,endFraction);selection.setAttribute('x',a);selection.setAttribute('width',Math.max(1,b-a));selection.setAttribute('visibility','visible');};
    svg.onwheel=ev=>{ev.preventDefault();zoom(ev.deltaY<0?.5:2,fraction(ev));};
    hit.onpointerdown=ev=>{down=fraction(ev);showSelection(down);hit.setPointerCapture(ev.pointerId);};
    hit.onpointermove=ev=>{if(down!=null)showSelection(fraction(ev));};
    hit.onpointercancel=()=>{down=null;hideSelection();};
    hit.onlostpointercapture=()=>{if(down!=null){down=null;hideSelection();}};
    hit.onpointerup=ev=>{if(down==null)return;const endFraction=fraction(ev),startFraction=down;down=null;hideSelection();try{hit.releasePointerCapture(ev.pointerId);}catch(_){ }if(Math.abs(endFraction-startFraction)>.015){range={start:start+Math.min(startFraction,endFraction)*width,end:start+Math.max(startFraction,endFraction)*width};load();}else inspect(start+endFraction*width);};
  }

  function liveLearningState(a){
    const rt=a.runtime||{};
    const training=a.training_state||rt.training_state||'paused';
    const paused=['paused','waiting','needs_retrain'].includes(training);
    const indexing=training==='training';
    const neverTrained=training==='waiting'||training==='needs_retrain'||(paused&&a.benchmark_score==null&&!a.training_cursor_ts);
    return {training,paused,indexing,neverTrained};
  }

  function liveSettings(a){
    if(typeof window.editAgent==='function')return window.editAgent(a.id);
    return workflowSettings(a.id);
  }

  function liveActions(card,a){
    const actions=card.querySelector('.actions');if(!actions)return;
    const state=liveLearningState(a);
    const signature=[state.training,state.neverTrained?'new':'model',a.training_cursor_ts??'',a.benchmark_score??''].join('|');
    if(actions.dataset.generationWorkflow===signature)return;
    actions.dataset.generationWorkflow=signature;

    // A fresh discovery generation has no model yet. Generation workflow actions require
    // a parent model, so exposing only those actions creates a UI deadlock: their status
    // endpoint rejects the agent while the original Train button has already been replaced.
    if(state.indexing){
      actions.innerHTML=`<button class="ghost" disabled data-wf="training">Training…</button><button class="ghost" data-wf="settings">Settings</button>`;
      actions.querySelector('[data-wf=settings]').onclick=()=>liveSettings(a);
      return;
    }
    if(state.neverTrained){
      actions.innerHTML=`<button class="primary" data-wf="train">Train</button><button class="ghost" data-wf="settings">Settings</button>`;
      actions.querySelector('[data-wf=train]').onclick=()=>window.trainAgent?.(a.id);
      actions.querySelector('[data-wf=settings]').onclick=()=>liveSettings(a);
      return;
    }

    const resume=state.paused?'<button class="ghost resume" data-wf="resume">Resume training</button>':'';
    const shadow=a.mode==='paused'?'<button class="primary" data-wf="shadow">Start Shadow</button>':a.mode==='shadow'?'<button class="ghost" data-wf="shadow">Pause Shadow</button>':'';
    actions.innerHTML=`${shadow}${resume}<button class="ghost" data-wf="auto">Autonomous</button><button class="primary" data-wf="correct">Correct</button><button class="ghost" data-wf="explore" disabled title="Explore będzie aktywowane przez warstwę Explore">Explore</button><button class="ghost" data-wf="change">Change decision</button><button class="ghost" data-wf="settings">Settings</button>`;
    if(actions.querySelector('[data-wf=shadow]'))actions.querySelector('[data-wf=shadow]').onclick=()=>window.setMode?.(a.id,a.mode==='shadow'?'paused':'shadow');
    if(state.paused)actions.querySelector('[data-wf=resume]').onclick=()=>window.resumeLearning?.(a.id);
    actions.querySelector('[data-wf=auto]').onclick=e=>workflowAutonomous(a.id,e.currentTarget);
    actions.querySelector('[data-wf=correct]').onclick=()=>openWorkflowCorrect(a.id);
    actions.querySelector('[data-wf=change]').onclick=e=>workflowChangeDecision(a.id,e.currentTarget);
    actions.querySelector('[data-wf=settings]').onclick=()=>liveSettings(a);
    window.bindExploreButtons?.();
  }

  const baseRender=window.renderAgents;
  if(typeof baseRender==='function'&&!window.__generationWorkflowRender){
    window.renderAgents=()=>{const out=baseRender(),agents=liveAgents();document.querySelectorAll('#agents > .agent:not(.candidate-agent)').forEach(card=>{const a=agents.find(x=>String(x.id)===String(card.dataset.agentId));if(a)liveActions(card,a);});return out;};
    window.__generationWorkflowRender=true;
  }
  // Decorate cards already rendered before this final UI layer loaded.
  {const agents=liveAgents();document.querySelectorAll('#agents > .agent:not(.candidate-agent)').forEach(card=>{const a=agents.find(x=>String(x.id)===String(card.dataset.agentId));if(a)liveActions(card,a);});}
})();