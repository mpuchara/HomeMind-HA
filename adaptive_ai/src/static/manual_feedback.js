// Teaching UI: four card actions live in p0.js; no DOM observer or injected controls.
(()=>{
  const dialog=document.createElement('dialog');dialog.id='teachDialog';document.body.append(dialog);
  const busy=new Set(),liveValues=new Map();
  let selectedAgent=null,range=null,chartData=null,selectedPoint=null,chartRequest=0,pointRequest=0,liveBusy=false,chartLoading=false,chartPending=false;
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
      for(const item of data.agents)liveValues.set(String(item.id),{...item,last_prediction_label:null});
      window.applyLiveValues();
      if(!lastAgents.length&&data.configs?.length){
        lastAgents=data.configs.map(a=>({...a,control_qualification:{passed:false,reason:'Ładuję kwalifikację agenta…'}}));
        renderAgents();
      }
      document.querySelectorAll('#agents > .agent').forEach(card=>{
        const a=agentFor(card.dataset.agentId);if(!a)return;
        window.updateAgentLive?.(card,a);
        for(const [key,v] of [['current',a.runtime.current_value],['desired',a.runtime.last_prediction]]){
          const el=card.querySelector(`[data-p0="${key}"]`);if(el)el.textContent=value(a,v);
        }
      });
    }catch(_){/* Heavy diagnostics keep their own connection indicator; next lightweight poll retries. */}
    finally{clearTimeout(timer);liveBusy=false;}
  }
  async function liveLoop(){await refreshLive();setTimeout(liveLoop,500);}
  liveLoop();

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
      if(dialog.open&&String(selectedAgent.id)===key){const ts=selectedPoint?.ts;await loadChart();if(ts!=null)await inspect(ts);}
    }catch(e){error(e);}finally{busy.delete(key);}
  };

  window.openTeach=id=>{
    selectedAgent=agentFor(id);if(!selectedAgent)return;
    const end=Date.now()/1000;range={start:end-86400,end};selectedPoint=null;chartData=null;
    dialog.innerHTML=`<div class="teach-head"><h2>Teach: ${esc(selectedAgent.name)}</h2><button class="ghost" data-close>Zamknij</button></div>
      <p>Kliknij wykres, żeby wskazać dokładny czas. Kółko przybliża wokół kursora; przeciągnięcie zaznacza zakres.</p>
      <div class="teach-range"><label>Od<input data-start type="datetime-local" step="1"></label><label>Do<input data-end type="datetime-local" step="1"></label><button class="ghost" data-load>Pokaż</button>
      <button class="ghost" data-prev aria-label="Wcześniejszy zakres">←</button><button class="ghost" data-next aria-label="Późniejszy zakres">→</button><button class="ghost" data-in aria-label="Przybliż wykres">+</button><button class="ghost" data-out aria-label="Oddal wykres">−</button></div>
      <p class="teach-legend"><span>● Current (historia HA)</span><span>┄ Desired (obecny model w dawnym kontekście)</span></p>
      <label><input type="checkbox" data-recorded> Pokaż również zarejestrowane Desired (od wersji 0.11.0)</label>
      <div class="teach-chart" data-chart></div><p data-status role="status"></p><p data-error role="alert"></p>
      <form data-point><label>Wybrany moment<input data-time type="datetime-local" step="1" required></label><button type="button" class="ghost" data-inspect>Sprawdź punkt</button>
      <p data-point-info>Wybierz punkt na wykresie.</p><label>Poprawne Desired<input data-value type="number" step="any" required></label>
      <button type="submit" class="primary" data-save disabled>Zapisz Desired</button><button type="button" class="ghost" data-undo>Cofnij ostatnią naukę</button></form>
      <p>Cofanie usuwa ostatnią korektę z Wrong decision lub Teach tego agenta. W Control poprawione Desired przechodzi przez zwykłe sterowanie. W Shadow urządzenie pozostaje bez zmian.</p>`;
    dialog.querySelector('[data-close]').onclick=()=>dialog.close();
    dialog.querySelector('[data-load]').onclick=()=>{range={start:Date.parse(dialog.querySelector('[data-start]').value)/1000,end:Date.parse(dialog.querySelector('[data-end]').value)/1000};loadChart();};
    for(const [name,factor] of [['in',.5],['out',2]])dialog.querySelector(`[data-${name}]`).onclick=()=>zoom(factor,.5);
    for(const [name,direction] of [['prev',-1],['next',1]])dialog.querySelector(`[data-${name}]`).onclick=()=>{const d=(range.end-range.start)*direction;range={start:range.start+d,end:range.end+d};loadChart();};
    dialog.querySelector('[data-recorded]').onchange=draw;
    dialog.querySelector('[data-inspect]').onclick=()=>inspect(Date.parse(dialog.querySelector('[data-time]').value)/1000);
    dialog.querySelector('[data-time]').oninput=()=>{selectedPoint=null;dialog.querySelector('[data-save]').disabled=true;};
    dialog.querySelector('[data-undo]').onclick=()=>undoTeaching(selectedAgent.id);
    dialog.querySelector('[data-point]').onsubmit=async event=>{
      event.preventDefault();if(!selectedPoint)return;
      const button=dialog.querySelector('[data-save]'),desired=Number(dialog.querySelector('[data-value]').value),aid=selectedAgent.id,ts=selectedPoint.ts;
      try{
        await submit(aid,{sample_ts:ts,desired_value:desired},button);
        if(dialog.open&&selectedAgent.id===aid){await loadChart();await inspect(ts);}
      }catch(e){error(e);}
    };
    dialog.showModal();loadChart();
  };
  dialog.addEventListener('close',()=>{chartRequest++;pointRequest++;chartPending=false;});

  async function loadChart(){
    const seq=++chartRequest;
    const end=Math.min(range.end,Date.now()/1000);range={start:range.start,end};
    if(!Number.isFinite(range.start)||!Number.isFinite(end)||end<=range.start||end-range.start>31*86400){error(Error('Wybierz poprawny zakres do 31 dni'));return;}
    if(chartLoading){chartPending=true;return;}
    chartLoading=true;
    dialog.querySelector('[data-start]').value=local(range.start);dialog.querySelector('[data-end]').value=local(range.end);
    dialog.querySelector('[data-error]').textContent='';dialog.querySelector('[data-status]').textContent='Odtwarzam historię…';
    try{
      const data=await api(`api/agents/${selectedAgent.id}/teaching-history?start=${range.start}&end=${range.end}`);
      if(seq!==chartRequest||!dialog.open)return;
      chartData=data;draw();
      dialog.querySelector('[data-undo]').disabled=!data.labels.length;
      dialog.querySelector('[data-status]').textContent=(data.points.some(p=>p.current!=null)?`${data.points.length} punktów. `:'Brak historii urządzenia w tym zakresie. ')+(data.reduced?'Widok zagregowany — przybliż dla większej dokładności. ':'')+(data.recorded_truncated?'Zapisane Desired: skróć zakres, aby zobaczyć wszystkie próbki. ':'')+'Desired jest rekonstrukcją, a nie zapisem dawnej predykcji.';
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
      const point=await api(`api/agents/${aid}/teaching-point?ts=${ts}`);
      if(seq!==pointRequest||!dialog.open||selectedAgent.id!==aid)return;
      selectedPoint=point;dialog.querySelector('[data-time]').value=local(point.ts);
      dialog.querySelector('[data-point-info]').textContent=`Current: ${value(selectedAgent,point.current)} · Desired: ${value(selectedAgent,point.desired)}${point.context_complete?'':' · brak pełnego kontekstu czujników'}`;
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
    const marks=chartData.labels.filter(r=>r.sample_ts>=chartData.start&&r.sample_ts<=chartData.end).map(r=>`<circle cx="${x(r.sample_ts)}" cy="${y(r.desired)}" r="5" fill="#ffd166"><title>Korekta #${r.id}</title></circle>`).join('');
    const recorded=dialog.querySelector('[data-recorded]').checked?`<path d="${path(chartData.recorded,'desired',65)}" stroke="#abb5bf" opacity=".65"/>`:'';
    dialog.querySelector('[data-chart]').innerHTML=`<svg viewBox="0 0 1000 355" role="img" aria-label="Historia Current i Desired" tabindex="0"><g fill="none" stroke-width="2"><path d="M50,25 V300 H980" stroke="#536576"/><path d="${path(points,'current')}" stroke="#73dbec"/><path d="${path(points,'desired')}" stroke="#c2a6ff" stroke-dasharray="7 4"/>${recorded}${selectedPoint?`<path d="M${x(selectedPoint.ts)},20 V300" stroke="#ffd166"/>`:''}</g><g fill="#acb8c5" font-size="12">${labels}<text x="5" y="42">${value(selectedAgent,hi)}</text><text x="5" y="300">${value(selectedAgent,lo)}</text></g>${marks}</svg>`;
    const svg=dialog.querySelector('svg');let down=null;
    const fraction=event=>Math.max(0,Math.min(1,((event.clientX-svg.getBoundingClientRect().left)/svg.getBoundingClientRect().width*1000-50)/930));
    svg.onwheel=event=>{event.preventDefault();zoom(event.deltaY<0?.5:2,fraction(event));};
    svg.onpointerdown=event=>{down=fraction(event);svg.setPointerCapture(event.pointerId);};
    svg.onpointerup=event=>{if(down==null)return;const end=fraction(event),start=down;down=null;const width=chartData.end-chartData.start;
      if(Math.abs(end-start)>.015){range={start:chartData.start+Math.min(start,end)*width,end:chartData.start+Math.max(start,end)*width};loadChart();}
      else inspect(chartData.start+end*width);
    };
  }
})();
