(()=>{
  const STYLE_ID='manual-feedback-style';
  const BUTTON_CLASS='manual-correction-btn';
  const LEGACY_VERIFY_CLASS='manual-legacy-verify';
  const busy=new Set();
  const feedbackUntil=new Map();

  function ensureStyle(){
    if(document.getElementById(STYLE_ID))return;
    const s=document.createElement('style');
    s.id=STYLE_ID;
    s.textContent=`
      .${BUTTON_CLASS}{display:inline-flex!important;visibility:visible!important;border:1px solid rgba(255,110,100,.55)!important;background:rgba(170,45,35,.16)!important;color:#ffb1a8!important;font-weight:700!important}
      .${BUTTON_CLASS}:hover{background:rgba(190,55,42,.26)!important}
      .${BUTTON_CLASS}[disabled]{opacity:.55;cursor:wait}
      .${LEGACY_VERIFY_CLASS}{display:none!important;visibility:hidden!important}
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

  function correctionLabel(){return '👎 Naucz / popraw';}

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
    const key=String(agentId);
    if(busy.has(key))return;
    busy.add(key);
    const old=button.textContent;
    button.disabled=true;
    button.textContent='Poprawiam i uczę…';
    try{
      const agents=await api('api/agents');
      const agent=agents.find(a=>String(a.id)===key);
      if(!agent)throw new Error('Nie znaleziono agenta.');
      const desired=askDesired(agent);
      if(desired===undefined){button.textContent=old;button.disabled=false;return;}
      const body=agent.target_property==='power'?{}:{desired_value:desired};
      const result=await api(`api/agents/${encodeURIComponent(key)}/manual-correction`,{method:'POST',body:JSON.stringify(body)});
      feedbackUntil.set(key,Date.now()+2200);
      button.textContent='✓ Nauczono'+learningSuffix(result);
      setTimeout(()=>{
        feedbackUntil.delete(key);
        button.textContent=correctionLabel();
        button.disabled=false;
      },2200);
      if(typeof window.load==='function')setTimeout(()=>window.load(),150);
    }catch(e){
      alert('Nie udało się wykonać korekty: '+e.message);
      button.textContent=old;
      button.disabled=false;
    }finally{busy.delete(key);}
  }

  function agentIdForCard(card){
    if(card?.dataset?.agentId)return String(card.dataset.agentId);
    const details=card?.querySelector('.agent-details[data-agent-id]');
    return details?.dataset?.agentId?String(details.dataset.agentId):null;
  }

  function legacyVerifyButton(actions){
    return [...actions.querySelectorAll('button')].find(x=>{
      const onclick=x.getAttribute('onclick')||'';
      return x.dataset.a==='verify'||onclick.includes('verifyControl(')||(x.textContent||'').trim()==='Verify control';
    })||null;
  }

  function bindManualButton(button,id){
    if(button.dataset.manualCorrectionBound==='1')return;
    button.dataset.manualCorrectionBound='1';
    button.addEventListener('click',()=>correct(id,button));
  }

  function installButtons(){
    ensureStyle();
    document.querySelectorAll('.agent.card').forEach(card=>{
      const actions=card.querySelector('.actions');
      const id=agentIdForCard(card);
      if(!id||!actions)return;

      const verify=legacyVerifyButton(actions);
      if(verify){
        if(!verify.classList.contains(LEGACY_VERIFY_CLASS))verify.classList.add(LEGACY_VERIFY_CLASS);
        if(!verify.hidden)verify.hidden=true;
        if(verify.getAttribute('aria-hidden')!=='true')verify.setAttribute('aria-hidden','true');
        if(verify.tabIndex!==-1)verify.tabIndex=-1;
      }

      let b=[...actions.querySelectorAll('.'+BUTTON_CLASS)].find(x=>x!==verify);
      if(!b){
        b=document.createElement('button');
        b.type='button';
        b.className='ghost '+BUTTON_CLASS;
        if(verify)actions.insertBefore(b,verify);else actions.prepend(b);
      }
      bindManualButton(b,id);
      if(b.hidden)b.hidden=false;
      if(b.getAttribute('aria-hidden')!=null)b.removeAttribute('aria-hidden');
      if(b.tabIndex!==0)b.tabIndex=0;
      b.title='Zgłoś błędną decyzję agenta. HomeMind poprawi urządzenie i zapisze tę korektę jako silny sygnał uczący razem z bieżącym kontekstem domu.';

      const showingFeedback=(feedbackUntil.get(id)||0)>Date.now();
      if(!busy.has(id)&&!showingFeedback){
        if(b.disabled)b.disabled=false;
        const label=correctionLabel();
        if(b.textContent!==label)b.textContent=label;
      }

      if(!actions.querySelector('.manual-correction-hint')){
        const hint=document.createElement('div');
        hint.className='manual-correction-hint';
        hint.textContent='Zawsze dostępne: użyj, gdy agent powinien zrobić coś innego. Dla urządzenia binarnego kliknięcie przełączy stan; dla pozostałych podasz właściwą wartość.';
        actions.appendChild(hint);
      }
    });
  }

  window.manualCorrection=(agentId,button)=>correct(agentId,button);

  function start(){
    installButtons();
    // Do not observe the whole agent subtree. Updating button text/children from inside
    // a subtree MutationObserver can schedule the observer again indefinitely and lock
    // the Home Assistant ingress tab. Instead hook the existing render pass once.
    if(typeof renderAgents==='function'){
      const previousRenderAgents=renderAgents;
      renderAgents=(...args)=>{
        const result=previousRenderAgents(...args);
        installButtons();
        return result;
      };
    }
  }

  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});
  else start();
})();
