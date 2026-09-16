// Teaching UI: four card actions live in p0.js; no DOM observer or injected controls.
(()=>{
  const dialog=document.createElement('dialog');dialog.id='teachDialog';document.body.append(dialog);
  const busy=new Set(),liveValues=new Map();
  let selectedAgent=null,range=null,chartData=null,selectedPoint=null,chartRequest=0,pointRequest=0,liveBusy=false,chartLoading=false,chartPending=false,teachPoll=0;
  const api=async(path,opts={})=>{
    const response=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});
    const body=await response.json();if(!response.ok)throw Error(body.error||`HTTP ${response.status}`);return body;
  };
  const agentFor=id=>lastAgents.find(a=>String(a.id)===String(id));
  const local=ts=>{const d=new Date(ts*1000);return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,19);};
  const value=(a,n)=>n==null?'—':a.target_property==='power'?(n>=.5?'ON':'OFF'):String(Number(n.toFixed(3)));
  const error=e=>{const el=dialog.open?dialog.querySelector('[data-error]'):null;if(el)el.textContent=e.message;else alert(e.message);};

  window.applyLiveValues=()=>{for(const a of lastAgents){const live=liveValues.get(String(a.id));if(live)a.runtime={...a.runtime,...live};}};
  async function refreshLive(){
    if(liveBusy||document.hidden)return;
    liveBusy=true;const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2500);
    try{
      const data=await api('api/live'+(!lastAgents.length?'?bootstrap=1':''),{signal:controller.signal});
      liveValues.clear();
      // Keep the complete runtime snapshot. Current, Desired, categorical Desired label
      // and Confidence are all live card values; none should fall back to the heavy status poll.
      for(const item of data.agents)liveValues.set(String(item.id),{...item});
      window.applyLiveValues();
      if(!lastAgents.length&&data.configs?.length){
        lastAgents=data.configs.map(a=>({...a,control_qualification:{passed:false,reason:'Ładuję kwalifikację agenta…'}}));
        renderAgents();
      }
      document.querySelectorAll('#agents > .agent').forEach(card=>{
        const a=agentFor(card.dataset.agentId);if(!a)return;
        // p0.js refreshes Current / Desired / Confidence from the merged live snapshot.
        window.updateAgentLive?.(card,a);
        for(const [key,v] of [['current',a.runtime.current_value],['desired',a.runtime.last_prediction]]){
          const el=card.querySelector(`[data-p0="${key}"]`);if(el)el.textContent=value(a,v);
        }
      });
    }catch(_){/* Heavy diagnostics keep their own connection indicator; next lightweight poll retries. */}
    finally{clearTimeout(timer);liveBusy=false;}
  }
  async function liveLoop(){await refreshLive();setTimeout(liveLoop,250);}
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshLive();});
  liveLoop();

  // Existing Wrong decision contract. Keep this path independent from historical Teach RL.
  async function submit(id,body,button){
    const key=String(id);if(busy.has(key))return;
    busy.add(key);if(button)button.disabled=true;
    try{return await api(`api/agents/${encodeURIComponent(key)}/teaching`,{method:'POST',body:JSON.stringify(body)});}
    finally{busy.delete(key);if(button)button.disabled=false;await refreshLive();}
  }
  window.wrongDecision=async(id,button)=>{
    try{
      const a=agentFor(id);if(!a)return;
      let desired;
      if(a.target_property!=='power'||a.runtime?.last_prediction==null){
        const raw=prompt(`Poprawne Desired dla ${a.name} (${a.min_value}–${a.max_value}):`,a.runtime?.last_prediction??'');
        if(raw===null)return;if(!raw.trim()||!Number.isFinite(desired=Number(raw.replace(',','.'))))throw Error('Podaj poprawną liczbę');
      }
      await submit(id,desired===undefined?{}:{desired_value:desired},button);
    }catch(e){error(e);}
  };
  window.undoTeaching=async id=>{
    const key=String(id);if(busy.has(key))return;
    busy.add(key);
    try{
      await api(`api/agents/${encodeURIComponent(key)}/undo-teaching`,{method:'POST',body:'{}'});
      await refreshLive();
    }catch(e){error(e);}finally{busy.delete(key);}
  };

  async function teachPost(suffix,body={}){
    const aid=String(selectedAgent.id);if(busy.has('teach:'+aid))return;
    busy.add('teach:'+aid);
    try{return await api(`api/agents/${encodeURIComponent(aid)}/${suffix}`,{method:'POST',body:JSON.stringify(body)});}
    finally{busy.delete('teach:'+aid);}
  }
  const setTeachButtons=disabled=>{for(const el of dialog.querySelectorAll('[data-save],[data-undo],[data-train]'))el.disabled=disabled||(el.hasAttribute('data-save')&&!selectedPoint);};
  const reportText=status=>{
    const r=status?.report||{};
    const parts=[];
    if(r.labels_applied!=null)parts.push(`${r.labels_applied} punktów Teach`);
    if(r.added?.length)parts.push(`dodano: ${r.added.join(', ')}`);
    if(r.removed?.length)parts.push(`usunięto: ${r.removed.join(', ')}`);
    if(r.teach_fit_before!=null&&r.teach_fit_after!=null)parts.push(`zgodność Teach ${Math.round(r.teach_fit_before*100)}% → ${Math.round(r.teach_fit_after*100)}%`);
    return parts.join(' · ');
  };
  async function pollTeach(seq){
    if(!dialog.open||seq!==teachPoll)return;
    try{
      const status=await api(`api/agents/${encodeURIComponent(selectedAgent.id)}/teach-rl-status`);
      const q=status.training_queue;
      const h=status.history||{};
      if(q){
        setTeachButtons(true);
        const stage=q.state==='queued'?`W kolejce (${q.position})`:(h.message||'Trening RL…');
        dialog.querySelector('[data-status]').textContent=`Teach RL: ${stage}`;
        setTimeout(()=>pollTeach(seq),1000);return;
      }
      if(status.state==='done'){
        setTeachButtons(false);
        dialog.querySelector('[data-status]').textContent=`Teach RL zakończony. ${reportText(status)}`;
        await loadChart();return;
      }
      if(status.state==='failed'){
        setTeachButtons(false);throw Error(status.report?.error||'Teach RL nie powiódł się');
      }
      setTeachButtons(false);
    }catch(e){setTeachButtons(false);error(e);}
  }
  async function trainTeach(){
    dialog.querySelector('[data-error]').textContent='';setTeachButtons(true);
    try{
      const result=await teachPost('teach-rl-train');
      dialog.querySelector('[data-status]').textContent=result.training_queue?.state==='queued'?'Teach RL dodany do kolejki…':'Uruchamiam Teach RL…';
      const seq=++teachPoll;pollTeach(seq);
    }catch(e){setTeachButtons(false);error(e);}
  }
  async function undoTeach(){
    try{await teachPost('undo-teach-rl');selectedPoint=null;await loadChart();}
    catch(e){error(e);}
  }

  window.openTeach=id=>{
    selectedAgent=agentFor(id);if(!selectedAgent)return;
    const end=Date.now()/1000;range={start:end-600,end};selectedPoint=null;chartData=null;
    dialog.innerHTML=`<div class="teach-head"><h2>Teach RL: ${esc(selectedAgent.name)}</h2><button class="ghost" data-close>Zamknij</button></div>
      <p>Zaznacz historyczne momenty i podaj prawidłowe Desired. Punkty staną się danymi supervised przy pełnym retrainingu RL.</p>
      <div class="teach-range"><label>Od<input data-start type="datetime-local" step="1"></label><label>Do<input data-end type="datetime-local" step="1"></label><button class="ghost" data-load>Pokaż</button>
      <button class="ghost" data-prev aria-label="Wcześniejszy zakres">←</button><button class="ghost" data-next aria-label="Późniejszy zakres">→</button><button class="ghost" data-in aria-label="Przybliż wykres">+</button><button class="ghost" data-out aria-label="Oddal wykres">−</button></div>
      <p class="teach-legend"><span>● Current (historia HA)</span><span>┄ Desired (bazowa policy RL)</span><span>● Punkty Teach</span></p>
      <div class="teach-chart" data-chart></div><p data-status role="status"></p><p data-error role="alert"></p>
      <form data-point><label>Wybrany moment<input data-time type="datetime-local" step="1" required></label><button type="button" class="ghost" data-inspect>Sprawdź punkt</button>
      <p data-point-info>Wybierz punkt na wykresie.</p><label>Poprawne Desired<input data-value type="number" step="any" required></label>
      <button type="submit" class="primary" data-save disabled>Dodaj punkt Teach</button><button type="button" class="ghost" data-undo>Cofnij ostatni punkt</button></form>
      <div class="dialog-actions"><button type="button" class="primary" data-train>Trenuj RL z punktami Teach</button></div>
      <p>Teach nie zmienia decyzji natychmiast. Po uruchomieniu treningu system ponownie ocenia kontekst, przebudowuje policy RL i odtwarza ten wykres na nowym modelu.</p>`;
    dialog.querySelector('[data-close]').onclick=()=>dialog.close();
    dialog.querySelector('[data-load]').onclick=()=>{range={start:Date.parse(dialog.querySelector('[data-start]').value)/1000,end:Date.parse(dialog.querySelector('[data-end]').value)/1000};loadChart();};
    for(const [name,factor] of [['in',.5],['out',2]])dialog.querySelector(`[data-${name}]`).onclick=()=>zoom(factor,.5);
    for(const [name,direction] of [['prev',-1],['next',1]])dialog.querySelector(`[data-${name}]`).onclick=()=>{const d=(range.end-range.start)*direction;range={start:range.start+d,end:range.end+d};loadChart();};
    dialog.querySelector('[data-inspect]').onclick=()=>inspect(Date.parse(dialog.querySelector('[data-time]').value)/1000);
    dialog.querySelector('[data-time]').oninput=()=>{selectedPoint=null;dialog.querySelector('[data-save]').disabled=true;};
    dialog.querySelector('[data-undo]').onclick=undoTeach;
    dialog.querySelector('[data-train]').onclick=trainTeach;
    dialog.querySelector('[data-point]').onsubmit=async event=>{
      event.preventDefault();if(!selectedPoint)return;
      const button=dialog.querySelector('[data-save]'),desired=Number(dialog.querySelector('[data-value]').value),aid=selectedAgent.id,ts=selectedPoint.ts;
      if(!Number.isFinite(desired)){error(Error('Podaj poprawną wartość Desired'));return;}
      button.disabled=true;
      try{
        await teachPost('teach-rl',{sample_ts:ts,desired_value:desired});
        if(dialog.open&&selectedAgent.id===aid){await loadChart();await inspect(ts);}
      }catch(e){error(e);}finally{button.disabled=false;}
    };
    dialog.showModal();loadChart();
    const seq=++teachPoll;pollTeach(seq);
  };
  dialog.addEventListener('close',()=>{chartRequest++;pointRequest++;chartPending=false;teachPoll++;});

  async function loadChart(){
    const seq=++chartRequest;
    const end=Math.min(range.end,Date.now()/1000);range={start:range.start,end};
    if(!Number.isFinite(range.start)||!Number.isFinite(end)||end<=range.start||end-range.start>31*86400){error(Error('Wybierz poprawny zakres do 31 dni'));return;}
    if(chartLoading){chartPending=true;return;}
    chartLoading=true;
    dialog.querySelector('[data-start]').value=local(range.start);dialog.querySelector('[data-end]').value=local(range.end);
    dialog.querySelector('[data-error]').textContent='';dialog.querySelector('[data-status]').textContent='Odtwarzam bazową policy RL…';
    try{
      const data=await api(`api/agents/${selectedAgent.id}/teach-rl-history?start=${range.start}&end=${range.end}`);
      if(seq!==chartRequest||!dialog.open)return;
      chartData=data;draw();
      dialog.querySelector('[data-undo]').disabled=!data.labels.length;
      dialog.querySelector('[data-status]').textContent=(data.points.some(p=>p.current!=null)?`${data.points.length} punktów. `:'Brak historii urządzenia w tym zakresie. ')+(data.reduced?'Widok zagregowany — przybliż dla większej dokładności. ':'')+'Desired jest replayem bazowej policy RL; punkty Teach nie są runtime override.';
    }catch(e){if(seq===chartRequest){dialog.querySelector('[data-status]').textContent='';error(e);}}
    finally{chartLoading=false;if(chartPending&&dialog.open){chartPending=false;loadChart();}}
  }
  function zoom(factor,anchor){
    if(!Number.isFinite(range.end-range.start))return;
    const width=Math.max(10,Math.min(31*86400,(range.end-range.start)*factor));
    const center=range.start+(range.end-range.start)*anchor;
    range={start:center-width*anchor,end:center+width*(1-anchor)};loadChart();
  }
  async function inspect(ts){
    if(!Number.isFinite(ts)){error(Error('Wybierz poprawną datę'));return;}
    const aid=selectedAgent.id,seq=++pointRequest;
    selectedPoint=null;dialog.querySelector('[data-save]').disabled=true;
    try{
      const point=await api(`api/agents/${aid}/teach-rl-point?ts=${ts}`);
      if(seq!==pointRequest||!dialog.open||selectedAgent.id!==aid)return;
      selectedPoint=point;dialog.querySelector('[data-time]').value=local(point.ts);
      dialog.querySelector('[data-point-info]').textContent=`Current: ${value(selectedAgent,point.current)} · Desired RL: ${value(selectedAgent,point.desired)}${point.context_complete?'':' · brak pełnego kontekstu czujników'}`;
      const input=dialog.querySelector('[data-value]');input.min=selectedAgent.min_value;input.max=selectedAgent.max_value;
      input.value=selectedAgent.target_property==='power'&&point.desired!=null?(point.desired>=.5?0:1):(point.desired??'');
      dialog.querySelector('[data-save]').disabled=point.current==null||!point.context_complete;draw();
    }catch(e){if(seq===pointRequest)error(e);}
  }
  function draw(){
    if(!chartData)return;
    const points=chartData.points,values=points.flatMap(p=>[p.current,p.desired]).filter(v=>v!=null);
    const lo=Math.min(selectedAgent.min_value,...values),hi=Math.max(selectedAgent.max_value,...values),span=Math.max(1,hi-lo);
    const x=ts=>50+930*(ts-chartData.start)/(chartData.end-chartData.start),y=v=>300-260*(v-lo)/span;
    const path=(ps,key,maxGap=Infinity)=>{let d='',previous=false,lastTs=0;for(const p of ps){const v=p[key];if(v==null){previous=false;continue;}if(p.ts-lastTs>maxGap)previous=false;d+=previous?` H${x(p.ts)} V${y(v)}`:` M${x(p.ts)},${y(v)}`;previous=true;lastTs=p.ts;}return d;};
    const labels=Array.from({length:5},(_,i)=>{const ts=chartData.start+(chartData.end-chartData.start)*i/4;return `<text x="${x(ts)}" y="335" text-anchor="${i===0?'start':i===4?'end':'middle'}">${esc(new Date(ts*1000).toLocaleString())}</text>`;}).join('');
    const marks=chartData.labels.filter(r=>r.sample_ts>=chartData.start&&r.sample_ts<=chartData.end).map(r=>`<circle cx="${x(r.sample_ts)}" cy="${y(r.desired)}" r="5" fill="#ffd166"><title>Teach RL #${r.id}</title></circle>`).join('');
    dialog.querySelector('[data-chart]').innerHTML=`<svg viewBox="0 0 1000 355" role="img" aria-label="Historia Current i Desired RL" tabindex="0"><rect data-selection x="50" y="20" width="0" height="280" fill="#9aa0a6" fill-opacity="0.32" stroke="#d4d7da" stroke-opacity="0.75" visibility="hidden"/><g fill="none" stroke-width="2"><path d="M50,25 V300 H980" stroke="#536576"/><path d="${path(points,'current')}" stroke="#73dbec"/><path d="${path(points,'desired')}" stroke="#c2a6ff" stroke-dasharray="7 4"/>${selectedPoint?`<path d="M${x(selectedPoint.ts)},20 V300" stroke="#d4d7da" stroke-opacity=".9"/>`:''}</g><g fill="#acb8c5" font-size="12">${labels}<text x="5" y="42">${value(selectedAgent,hi)}</text><text x="5" y="300">${value(selectedAgent,lo)}</text></g>${marks}</svg>`;
    const svg=dialog.querySelector('svg'),selection=svg.querySelector('[data-selection]');let down=null;
    const fraction=event=>Math.max(0,Math.min(1,((event.clientX-svg.getBoundingClientRect().left)/svg.getBoundingClientRect().width*1000-50)/930));
    const showSelection=end=>{if(down==null)return;const a=50+930*Math.min(down,end),b=50+930*Math.max(down,end);selection.setAttribute('x',a);selection.setAttribute('width',Math.max(1,b-a));selection.setAttribute('visibility','visible');};
    svg.onwheel=event=>{event.preventDefault();zoom(event.deltaY<0?.5:2,fraction(event));};
    svg.onpointerdown=event=>{down=fraction(event);showSelection(down);svg.setPointerCapture(event.pointerId);};
    svg.onpointermove=event=>{if(down!=null)showSelection(fraction(event));};
    svg.onpointercancel=()=>{down=null;selection.setAttribute('visibility','hidden');};
    svg.onpointerup=event=>{if(down==null)return;const end=fraction(event),start=down;down=null;selection.setAttribute('visibility','hidden');const width=chartData.end-chartData.start;
      if(Math.abs(end-start)>.015){range={start:chartData.start+Math.min(start,end)*width,end:chartData.start+Math.max(start,end)*width};loadChart();}
      else inspect(chartData.start+end*width);
    };
  }
})();