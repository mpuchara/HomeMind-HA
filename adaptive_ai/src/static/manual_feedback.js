(()=>{
  const STYLE_ID='manual-feedback-style';
  const BUTTON_CLASS='manual-correction-btn';
  const TEACH_CLASS='teach-desired-btn';
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
      .${TEACH_CLASS}{display:inline-flex!important;visibility:visible!important;border:1px solid #7cd2f6!important;color:#a7e3ff!important;font-weight:700!important}
      .${TEACH_CLASS}[disabled]{opacity:.55;cursor:wait}
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

  function correctionLabel(teaching=false){return teaching?'Naucz':'👎 Naucz / popraw';}

  function askDesired(agent,teaching=false){
    const rt=agent.runtime||{};
    const current=teaching?rt.last_prediction:rt.current_value;
    if(agent.target_property==='power'&&(!teaching||current!=null))return null;
    const lo=Number(agent.min_value),hi=Number(agent.max_value);
    let message=`Podaj właściwą wartość dla ${agent.name}.\n${teaching?'Desired (nauka bez polecenia do urządzenia)':'Current (zmiana urządzenia)'}: ${current??'—'}`;
    if(agent.target_property==='power')message+='\nPodaj 0 (OFF) lub 1 (ON).';
    if(Number.isFinite(lo)&&Number.isFinite(hi))message+=`\nZakres: ${lo} … ${hi}`;
    if(agent.target_property==='option_index')message+='\nDla wyboru podaj indeks opcji.';
    const raw=window.prompt(message,current??'');
    if(raw===null)return undefined;
    if(!String(raw).trim())throw new Error('Podaj wartość.');
    const value=Number(String(raw).replace(',','.'));
    if(!Number.isFinite(value))throw new Error('Podana wartość nie jest liczbą.');
    if(agent.target_property==='power'&&![0,1].includes(value))throw new Error('Podaj 0 lub 1.');
    return value;
  }

  function learningSuffix(result){
    const learned=result.positive_applied?' · właściwy stan +1':'';
    const punished=result.negative_applied?' · błędna decyzja −1':'';
    const added=result.context_learning?.added||[];
    const context=added.length?` · nowy kontekst: ${added.slice(0,2).join(', ')}`:'';
    return `${punished}${learned}${context}`;
  }

  async function correct(agentId,button,teaching=false){
    const key=String(agentId);
    const feedbackKey=key+':'+teaching;
    if(busy.has(key))return;
    busy.add(key);
    const old=button.textContent;
    button.disabled=true;
    button.textContent=teaching?'Uczę Desired…':'Poprawiam i uczę…';
    try{
      const agents=await api('api/agents');
      const agent=agents.find(a=>String(a.id)===key);
      if(!agent)throw new Error('Nie znaleziono agenta.');
      const desired=askDesired(agent,teaching);
      if(desired===undefined){button.textContent=old;button.disabled=false;return;}
      const body=teaching?(desired===null?{}:{desired_value:desired}):(agent.target_property==='power'?{}:{desired_value:desired});
      const path=teaching?`api/agents/${encodeURIComponent(key)}/teach-desired`:`api/agents/${encodeURIComponent(key)}/manual-correction`;
      const result=await api(path,{method:'POST',body:JSON.stringify(body)});
      feedbackUntil.set(feedbackKey,Date.now()+2200);
      button.textContent=(teaching?'✓ Zapisano naukę Desired':'✓ Nauczono')+learningSuffix(result);
      setTimeout(()=>{
        feedbackUntil.delete(feedbackKey);
        button.textContent=correctionLabel(teaching);
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

      for(const teaching of [true,false]){
      const cls=teaching?TEACH_CLASS:BUTTON_CLASS;
      let b=[...actions.querySelectorAll('.'+cls)].find(x=>x!==verify);
      if(!b){
        b=document.createElement('button');
        b.type='button';
        b.className='ghost '+cls;
        if(verify)actions.insertBefore(b,verify);else actions.prepend(b);
      }
      if(teaching){
        if(b.dataset.teachBound!=='1'){
          b.dataset.teachBound='1';
          b.addEventListener('click',()=>correct(id,b,true));
        }
      }else bindManualButton(b,id);
      if(b.hidden)b.hidden=false;
      if(b.getAttribute('aria-hidden')!=null)b.removeAttribute('aria-hidden');
      if(b.tabIndex!==0)b.tabIndex=0;
      b.title=teaching?'Naucz poprawnego Desired w bieżącym kontekście, także w Shadow. Nie wysyła polecenia do urządzenia. Dla ON/OFF odwraca Desired.':'Popraw Current: zmień urządzenie i naucz agenta poprawnego stanu. Dla ON/OFF odwraca Current.';

      const showingFeedback=(feedbackUntil.get(id+':'+teaching)||0)>Date.now();
      if(!busy.has(id)&&!showingFeedback){
        if(b.disabled)b.disabled=false;
        const label=correctionLabel(teaching);
        if(b.textContent!==label)b.textContent=label;
      }
      }

      if(!actions.querySelector('.manual-correction-hint')){
        const hint=document.createElement('div');
        hint.className='manual-correction-hint';
        hint.textContent='Naucz → popraw Desired bez polecenia do urządzenia (także Shadow). Naucz / popraw → zmień Current i ucz. ON/OFF: odwróć wskazany stan; inne wartości: podaj nastawę. W Control agent nadal steruje automatycznie.';
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
