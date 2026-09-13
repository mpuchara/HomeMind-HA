(()=>{
  const STYLE_ID='manual-feedback-style';
  const BUTTON_CLASS='manual-correction-btn';
  const KEEP_CLASS='manual-keep-current-btn';
  let busy=new Set();

  function ensureStyle(){
    if(document.getElementById(STYLE_ID))return;
    const s=document.createElement('style');
    s.id=STYLE_ID;
    s.textContent=`
      .${BUTTON_CLASS}{border:1px solid rgba(255,110,100,.55)!important;background:rgba(170,45,35,.16)!important;color:#ffb1a8!important;font-weight:700!important}
      .${BUTTON_CLASS}:hover{background:rgba(190,55,42,.26)!important}
      .${KEEP_CLASS}{border:1px solid rgba(90,190,140,.55)!important;background:rgba(30,130,85,.14)!important;color:#a9efc9!important;font-weight:700!important}
      .${KEEP_CLASS}:hover{background:rgba(35,150,95,.24)!important}
      .${BUTTON_CLASS}[disabled],.${KEEP_CLASS}[disabled]{opacity:.55;cursor:wait}
      .manual-correction-hint{width:100%;font-size:12px;line-height:1.35;color:var(--muted,#9aa0aa);margin-top:2px}
      .manual-feedback-row{display:flex;gap:8px;flex-wrap:wrap;width:100%;align-items:center}
    `;
    document.head.appendChild(s);
  }

  async function api(path,opts={}){
    const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});
    const text=await r.text();
    let body=null;
    try{body=text?JSON.parse(text):null}catch(_){body=null}
    if(!r.ok)throw new Error(body?.error||text||`HTTP ${r.status}`);
    return body;
  }

  function correctionLabel(agent){
    if(agent?.target_property==='power')return '👎 Stan zły — przełącz';
    return '👎 Stan zły — ustaw poprawny';
  }

  function askDesired(agent){
    const rt=agent.runtime||{};
    const current=rt.current_value;
    if(agent.target_property==='power')return null; // backend toggles atomically from current state
    const lo=Number(agent.min_value),hi=Number(agent.max_value);
    let message=`Podaj właściwą wartość dla ${agent.name}.\nAktualnie: ${current??'—'}`;
    if(Number.isFinite(lo)&&Number.isFinite(hi))message+=`\nZakres: ${lo} … ${hi}`;
    if(agent.target_property==='option_index')message+='\nDla wyboru podaj indeks opcji.';
    const raw=window.prompt(message,current??'');
    if(raw===null)return undefined;
    const value=Number(String(raw).replace(',','.'));
    if(!Number.isFinite(value))throw new Error('Podana wartość nie jest liczbą.');
    return value;
  }

  function setAgentButtons(agentId,disabled){
    document.querySelectorAll(`[data-manual-agent="${CSS.escape(String(agentId))}"]`).forEach(b=>b.disabled=disabled);
  }

  function learningSuffix(result){
    const learned=result.positive_applied?' · +1':'';
    const punished=result.negative_applied?' · −1':'';
    const added=result.context_learning?.added||[];
    const context=added.length?` · nowy kontekst: ${added.slice(0,2).join(', ')}`:'';
    return `${punished}${learned}${context}`;
  }

  async function teach(agentId,button,keepCurrent){
    if(busy.has(agentId))return;
    busy.add(agentId);
    const old=button.textContent;
    setAgentButtons(agentId,true);
    button.textContent=keepCurrent?'Uczę obecny stan…':'Koryguję…';
    try{
      const agents=await api('api/agents');
      const agent=agents.find(a=>String(a.id)===String(agentId));
      if(!agent)throw new Error('Nie znaleziono agenta.');
      let body={};
      if(keepCurrent){
        body={keep_current:true};
      }else{
        const desired=askDesired(agent);
        if(desired===undefined){
          button.textContent=old;
          return;
        }
        body=agent.target_property==='power'?{}:{desired_value:desired};
      }
      const result=await api(`api/agents/${encodeURIComponent(agentId)}/manual-correction`,{
        method:'POST',body:JSON.stringify(body)
      });
      button.textContent=(keepCurrent?'✓ Obecny stan nauczony':'✓ Poprawiono')+learningSuffix(result);
      setTimeout(()=>{
        button.textContent=keepCurrent?'✓ Obecny stan jest poprawny':correctionLabel(agent);
        setAgentButtons(agentId,false);
      },2200);
      if(typeof window.load==='function')setTimeout(()=>window.load(),150);
    }catch(e){
      alert('Nie udało się wykonać korekty: '+e.message);
      button.textContent=old;
    }finally{
      busy.delete(agentId);
      // If the flow was cancelled or failed there is no delayed reset callback.
      if(!button.textContent.startsWith('✓'))setAgentButtons(agentId,false);
    }
  }

  function installButtons(){
    ensureStyle();
    document.querySelectorAll('.agent.card').forEach(card=>{
      const details=card.querySelector('.agent-details[data-agent-id]');
      const actions=card.querySelector('.actions');
      if(!details||!actions||actions.querySelector('.'+KEEP_CLASS))return;
      const id=details.dataset.agentId;
      const row=document.createElement('div');
      row.className='manual-feedback-row';

      const keep=document.createElement('button');
      keep.type='button';
      keep.className='ghost '+KEEP_CLASS;
      keep.dataset.manualAgent=id;
      keep.textContent='✓ Obecny stan jest poprawny';
      keep.title='Desired jest błędne. Nie zmieniaj urządzenia: ukarz błędne Desired, nagródź stan widoczny teraz i zapisz pełny kontekst domu.';
      keep.addEventListener('click',()=>teach(id,keep,true));

      const correct=document.createElement('button');
      correct.type='button';
      correct.className='ghost '+BUTTON_CLASS;
      correct.dataset.manualAgent=id;
      correct.textContent='👎 Stan zły — popraw';
      correct.title='Ręczna korekta użytkownika: zmienia urządzenie i uczy agenta tak jak fizyczna zmiana nastawy.';
      correct.addEventListener('click',()=>teach(id,correct,false));

      row.appendChild(keep);
      row.appendChild(correct);
      actions.prepend(row);
      const hint=document.createElement('div');
      hint.className='manual-correction-hint';
      hint.textContent='Obie opcje zapisują pełny kontekst. „Obecny stan jest poprawny” niczego nie przełącza; odrzuca błędne Desired i uczy także niewybrane jeszcze sensory.';
      actions.appendChild(hint);
    });
  }

  function start(){
    installButtons();
    const root=document.getElementById('agents');
    if(root)new MutationObserver(()=>installButtons()).observe(root,{childList:true,subtree:true});
    setInterval(installButtons,2000);
  }

  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);
  else start();
})();
