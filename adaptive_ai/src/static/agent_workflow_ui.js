// Generation-first agent actions: Autonomous, Correct, Explore (next PR), Change decision, Settings.
(()=>{
  const COLORS={current:'#73dbec',parent:'#c2a6ff',candidate:'#ff9f43',correct:'#ffd166'};
  const html=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});let b={};try{b=await r.json();}catch(_){ }if(!r.ok)throw Error(b.error||`HTTP ${r.status}`);return b;};
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
    dialog.innerHTML=`<div class="teach-head"><h2>Correct: ${html(subject.name)} · Gen ${subject.generation_number}</h2><button class="ghost" data-close>Zamknij</button></div>
      <p>Kliknij wykres, aby wskazać moment, albo przeciągnij poziomo po wykresie, aby zaznaczyć zakres i go przybliżyć. Kółko myszy przybliża wokół kursora. Correct nie zmienia Gen ${subject.generation_number} w miejscu — po zatwierdzeniu utworzy child Candidate.</p>
      <div class="teach-range"><label>Od<input data-start type="datetime-local" step="1"></label><label>Do<input data-end type="datetime-local" step="1"></label><button class="ghost" data-load>Pokaż</button><button class="ghost" data-prev>←</button><button class="ghost" data-next>→</button><button class="ghost" data-in>+</button><button class="ghost" data-out>−</button></div>
      <p class="teach-legend" data-legend></p>
      <div class="teach-chart" data-chart></div><p data-status role="status"></p><p data-error role="alert"></p>
      <form data-point><label>Wybrany moment<input data-time type="datetime-local" step="1" required></label><button type="button" class="ghost" data-inspect>Sprawdź punkt</button><p data-point-info>Wybierz moment na wykresie.</p><label>Poprawne Desired<input data-value type="number" step="any" required></label><button class="primary" type="submit" data-save disabled>Dodaj Correct</button><button class="ghost" type="button" data-undo>Cofnij ostatni Correct</button></form>
      <div class="dialog-actions"><button class="primary" type="button" data-apply>Apply Correct · create child Candidate</button></div>
      <p>Wykres używa wyłącznie observed generation decision history. Candidate jest porównywany tylko z bezpośrednim parentem; brak runtime pozostaje luką i nie jest odtwarzany obecną policy.</p>`;
    dialog.querySelector('[data-close]').onclick=()=>dialog.close();
    dialog.querySelector('[data-load]').onclick=()=>{const a=Date.parse(dialog.querySelector('[data-start]').value)/1000,b=Date.parse(dialog.querySelector('[data-end]').value)/1000;if(Number.isFinite(a)&&Number.isFinite(b)){range={start:a,end:b};load();}};
    for(const [k,f] of [['in',.5],['out',2]])dialog.querySelector(`[data-${k}]`).onclick=()=>zoom(f);
    for(const [k,d] of [['prev',-1],['next',1]])dialog.querySelector(`[data-${k}]`).onclick=()=>shift(d);
    dialog.querySelector('[data-inspect]').onclick=()=>inspect(Date.parse(dialog.querySelector('[data-time]').value)/1000);
    dialog.querySelector('[data-time]').oninput=()=>{selected=null;dialog.querySelector('[data-save]').disabled=true;};
    dialog.querySelector('[data-undo]').onclick=undo;
    dialog.querySelector('[data-apply]').onclick=apply;
    dialog.querySelector('[data-point]').onsubmit=save;
  }

  window.openWorkflowCorrect=async generationRef=>{
    try{
      ref=String(generationRef);subject=await status(ref);
      const end=Date.now()/1000;range={start:end-600,end};selected=null;data=null;shell();dialog.showModal();await load();
    }catch(e){notifyError(e);}
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
    setBusy(true);dialog.querySelector('[data-error]').textContent='';
    try{
      const out=await post(ref,'correct');
      dialog.close();await refresh();return out;
    }catch(e){error(e);}finally{setBusy(false);}
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
    const path=points=>{let d='',active=false,last=0;for(const p of (points||[]).slice().sort((a,b)=>Number(a.ts)-Number(b.ts))){const v=p.value,ts=Number(p.ts);if(v==null||!Number.isFinite(Number(v))||(last&&ts-last>stale))active=false;if(v!=null&&Number.isFinite(Number(v))){d+=active?` H${x(ts)} V${y(v)}`:` M${x(ts)},${y(v)}`;active=true;last=ts;}}return d;};
    const rendered=[];
    if(series.live_desired)rendered.push(`<path data-series="live_desired" d="${path(series.live_desired.points)}" fill="none" stroke="${COLORS.candidate}" stroke-width="2" stroke-dasharray="8 6" stroke-linecap="round" opacity="0.95"/>`);
    if(series.parent_desired)rendered.push(`<path data-series="parent_desired" d="${path(series.parent_desired.points)}" fill="none" stroke="${COLORS.parent}" stroke-width="2" stroke-dasharray="8 6" stroke-linecap="round" opacity="0.95"/>`);
    if(series.candidate_desired)rendered.push(`<path data-series="candidate_desired" d="${path(series.candidate_desired.points)}" fill="none" stroke="${COLORS.candidate}" stroke-width="2" stroke-dasharray="8 6" stroke-linecap="round" opacity="0.95"/>`);
    if(series.current){
      const currentPath=path(series.current.points);
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

  function liveActions(card,a){
    const actions=card.querySelector('.actions');if(!actions)return;
    if(actions.dataset.generationWorkflow==='1')return;
    actions.dataset.generationWorkflow='1';
    actions.innerHTML=`<button class="ghost" data-wf="auto">Autonomous</button><button class="primary" data-wf="correct">Correct</button><button class="ghost" data-wf="explore" disabled title="Explore będzie wdrożone w następnym PR">Explore</button><button class="ghost" data-wf="change">Change decision</button><button class="ghost" data-wf="settings">Settings</button>`;
    actions.querySelector('[data-wf=auto]').onclick=e=>workflowAutonomous(a.id,e.currentTarget);
    actions.querySelector('[data-wf=correct]').onclick=()=>openWorkflowCorrect(a.id);
    actions.querySelector('[data-wf=change]').onclick=e=>workflowChangeDecision(a.id,e.currentTarget);
    actions.querySelector('[data-wf=settings]').onclick=()=>workflowSettings(a.id);
  }

  const baseRender=window.renderAgents;
  if(typeof baseRender==='function'&&!window.__generationWorkflowRender){
    window.renderAgents=()=>{const out=baseRender(),agents=liveAgents();document.querySelectorAll('#agents > .agent:not(.candidate-agent)').forEach(card=>{const a=agents.find(x=>String(x.id)===String(card.dataset.agentId));if(a)liveActions(card,a);});return out;};
    window.__generationWorkflowRender=true;
  }
  // Decorate cards already rendered before this final UI layer loaded.
  {const agents=liveAgents();document.querySelectorAll('#agents > .agent:not(.candidate-agent)').forEach(card=>{const a=agents.find(x=>String(x.id)===String(card.dataset.agentId));if(a)liveActions(card,a);});}
})();
