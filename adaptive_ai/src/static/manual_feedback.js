(()=>{
  const STYLE_ID='manual-feedback-style';
  const BUTTON_CLASS='manual-correction-btn';
  let busy=new Set();

  function ensureStyle(){
    if(document.getElementById(STYLE_ID))return;
    const s=document.createElement('style');
    s.id=STYLE_ID;
    s.textContent=`
      .${BUTTON_CLASS}{border:1px solid rgba(255,110,100,.55)!important;background:rgba(170,45,35,.16)!important;color:#ffb1a8!important;font-weight:700!important}
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

  function labelFor(agent){
    if(agent?.target_property==='power')return '👎 Zły stan — przełącz';
    return '👎 Zły stan — popraw';
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

  async function correct(agentId,button){
    if(busy.has(agentId))return;
    busy.add(agentId);
    const old=button.textContent;
    button.disabled=true;
    button.textContent='Koryguję…';
    try{
      const agents=await api('api/agents');
      const agent=agents.find(a=>String(a.id)===String(agentId));
      if(!agent)throw new Error('Nie znaleziono agenta.');
      const desired=askDesired(agent);
      if(desired===undefined){button.textContent=old;return;}
      const body=agent.target_property==='power'?{}:{desired_value:desired};
      const result=await api(`api/agents/${encodeURIComponent(agentId)}/manual-correction`,{
        method:'POST',body:JSON.stringify(body)
      });
      const learned=result.positive_applied?' · nauka +1':'';
      const punished=result.negative_applied?' · kara −1':'';
      button.textContent=`✓ Poprawiono${punished}${learned}`;
      setTimeout(()=>{button.textContent=labelFor(agent);button.disabled=false;},1800);
      if(typeof window.load==='function')setTimeout(()=>window.load(),150);
    }catch(e){
      alert('Nie udało się wykonać korekty: '+e.message);
      button.textContent=old;
      button.disabled=false;
    }finally{
      busy.delete(agentId);
    }
  }

  function installButtons(){
    ensureStyle();
    document.querySelectorAll('.agent.card').forEach(card=>{
      const details=card.querySelector('.agent-details[data-agent-id]');
      const actions=card.querySelector('.actions');
      if(!details||!actions||actions.querySelector('.'+BUTTON_CLASS))return;
      const id=details.dataset.agentId;
      const b=document.createElement('button');
      b.type='button';
      b.className='ghost '+BUTTON_CLASS;
      b.textContent='👎 Zły stan — popraw';
      b.title='Ręczna korekta użytkownika: zmienia urządzenie i uczy agenta tak jak fizyczna zmiana nastawy.';
      b.addEventListener('click',()=>correct(id,b));
      actions.prepend(b);
      const hint=document.createElement('div');
      hint.className='manual-correction-hint';
      hint.textContent='Korekta użytkownika ma priorytet: odrzuca błędną decyzję i wzmacnia właściwy stan.';
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
