(()=>{
  const STYLE_ID='manual-feedback-style';
  const BUTTON_CLASS='manual-correction-btn';
  let busy=new Set();

  function ensureStyle(){
    if(document.getElementById(STYLE_ID))return;
    const s=document.createElement('style');
    s.id=STYLE_ID;
    s.textContent=`
      .${BUTTON_CLASS}{display:inline-flex!important;visibility:visible!important;border:1px solid rgba(255,110,100,.55)!important;background:rgba(170,45,35,.16)!important;color:#ffb1a8!important;font-weight:700!important}
      .${BUTTON_CLASS}:hover{background:rgba(190,55,42,.26)!important}
      .${BUTTON_CLASS}[disabled]{opacity:.55;cursor:wait}
      .manual-correction-hint{width:100%;font-size:12px;line-height:1.35;color:var(--muted,#9aa0aa);margin-top:2px}
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
    if(agent?.target_property==='power')return '👎 Agent zrobił źle — popraw';
    return '👎 Agent zrobił źle — ustaw poprawnie';
  }

  function askDesired(agent){
    const rt=agent.runtime||{};
    const current=rt.current_value;
    if(agent.target_property==='power')return null;
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

  function learningSuffix(result){
    const learned=result.positive_applied?' · właściwy stan +1':'';
    const punished=result.negative_applied?' · błędna decyzja −1':'';
    const added=result.context_learning?.added||[];
    const context=added.length?` · nowy kontekst: ${added.slice(0,2).join(', ')}`:'';
    return `${punished}${learned}${context}`;
  }

  async function correct(agentId,button){
    if(busy.has(agentId))return;
    busy.add(agentId);
    const old=button.textContent;
    button.disabled=true;
    button.textContent='Poprawiam i uczę…';
    try{
      const agents=await api('api/agents');
      const agent=agents.find(a=>String(a.id)===String(agentId));
      if(!agent)throw new Error('Nie znaleziono agenta.');
      const desired=askDesired(agent);
      if(desired===undefined){button.textContent=old;button.disabled=false;return;}
      const body=agent.target_property==='power'?{}:{desired_value:desired};
      const result=await api(`api/agents/${encodeURIComponent(agentId)}/manual-correction`,{method:'POST',body:JSON.stringify(body)});
      button.textContent='✓ Stan poprawiony'+learningSuffix(result);
      setTimeout(()=>{button.textContent=correctionLabel(agent);button.disabled=false;},2200);
      if(typeof window.load==='function')setTimeout(()=>window.load(),150);
    }catch(e){
      alert('Nie udało się wykonać korekty: '+e.message);
      button.textContent=old;
      button.disabled=false;
    }finally{busy.delete(agentId);}
  }

  function installButtons(){
    ensureStyle();
    document.querySelectorAll('.agent.card').forEach(card=>{
      const details=card.querySelector('.agent-details[data-agent-id]');
      const actions=card.querySelector('.actions');
      if(!details||!actions)return;
      const id=details.dataset.agentId;
      let b=actions.querySelector('.'+BUTTON_CLASS);
      if(!b){
        b=[...actions.querySelectorAll('button')].find(x=>{
          const onclick=x.getAttribute('onclick')||'';
          return onclick.includes('verifyControl(')||(x.textContent||'').trim()==='Verify control';
        });
        if(b){
          b.removeAttribute('onclick');
          b.classList.add(BUTTON_CLASS);
        }else{
          b=document.createElement('button');
          b.type='button';
          b.className='ghost '+BUTTON_CLASS;
          actions.prepend(b);
        }
        b.addEventListener('click',()=>correct(id,b));
      }
      b.disabled=false;
      b.hidden=false;
      b.style.display='';
      b.textContent='👎 Naucz / popraw';
      b.title='Zgłoś błędną decyzję agenta. HomeMind poprawi urządzenie i zapisze tę korektę jako silny sygnał uczący razem z bieżącym kontekstem domu.';
      if(!actions.querySelector('.manual-correction-hint')){
        const hint=document.createElement('div');
        hint.className='manual-correction-hint';
        hint.textContent='Zawsze dostępne: użyj, gdy agent powinien zrobić coś innego. Dla urządzenia binarnego kliknięcie przełączy stan; dla pozostałych podasz właściwą wartość.';
        actions.appendChild(hint);
      }
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
