(()=>{
  const pct=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const n1=v=>v==null?'—':Number(v).toFixed(1);
  const signed=v=>v==null?'—':`${Number(v)>=0?'+':''}${Number(v).toFixed(3)}`;
  let cachedCandidates=[];
  let cachedAgents=[];

  function renameLegacyLabels(){
    for(const span of document.querySelectorAll('.agent-primary span,.candidate-decision-strip span,.candidate-compare-details span,.candidate-compare-minimal span')){
      const text=(span.textContent||'').trim();
      if(text==='Live confidence'||text==='Candidate confidence'||text==='Confidence'||text==='Model confidence'){
        span.textContent='Decision strength';
        span.title='Heuristic decision-strength / gating score. Not a probability of comfort or correctness.';
      }else if(text==='Preference confidence'){
        span.textContent='Preference alignment';
        span.title='Weighted alignment lower-bound style score. Not a probability of comfort.';
      }else if(text==='Behaviour benchmark'){
        span.textContent='Held-out policy quality';
      }
    }
    const overview=[...document.querySelectorAll('#overview span')].find(x=>(x.textContent||'').includes('average policy confidence'));
    if(overview){
      overview.textContent='average decision strength';
      overview.title='Compatibility score; not a calibrated probability.';
    }
  }

  function cardRef(card){return String(card?.dataset?.candidateRef||card?.dataset?.generationId||'');}
  function candidateRef(c){return String(c?.generation_id||c?.candidate_id||'');}
  function liveDetails(agentId){
    return [...document.querySelectorAll('.agent-details[data-agent-id]')]
      .find(x=>String(x.dataset.agentId)===String(agentId));
  }
  const setText=(node,value)=>{
    if(!node)return;
    const text=String(value??'');
    if(node.textContent!==text)node.textContent=text;
  };
  const setDisplay=(node,value)=>{if(node&&node.style.display!==value)node.style.display=value;};

  function ensureMetric(root,key,label,value,title=''){
    if(!root)return;
    let node=root.querySelector(`[data-confidence-metric="${key}"]`);
    if(!node){
      node=document.createElement('div');
      node.dataset.confidenceMetric=key;
      node.innerHTML='<span></span><b></b><small></small>';
      root.appendChild(node);
    }
    setText(node.querySelector('span'),label);
    setText(node.querySelector('b'),value);
    const small=node.querySelector('small');
    setText(small,title);
    setDisplay(small,title?'':'none');
  }

  function decorateLive(a){
    const rt=a?.runtime||{};
    const details=liveDetails(a?.id);
    if(!details)return;
    let panel=details.querySelector('[data-confidence-contract-live]');
    if(!panel){
      panel=document.createElement('div');
      panel.className='detail confidence-contract-live';
      panel.dataset.confidenceContractLive='1';
      details.appendChild(panel);
    }
    ensureMetric(panel,'live-decision-strength','Decision strength',pct(rt.decision_strength??rt.last_confidence),'heuristic gate score · not a probability');
    ensureMetric(panel,'live-utility','Expected action utility',signed(rt.expected_action_utility),'expected reward/utility · not a probability');
    ensureMetric(panel,'live-coverage','Data coverage',pct(rt.data_coverage),'context support / effective coverage · not quality');
    ensureMetric(panel,'live-presence','Presence probability · 3 s',pct(rt.presence_probability),'probability claim; calibration requires independent labelled episodes');
    ensureMetric(panel,'live-uncertainty','Forecast uncertainty',pct(rt.forecast_uncertainty),'uncertainty score · not probability of failure');
    const diagnostic=rt.policy_validation_diagnostic||{};
    ensureMetric(panel,'live-policy-diagnostic','Policy validation diagnostic',diagnostic.accuracy==null?'—':pct(diagnostic.accuracy),
      diagnostic.samples==null?'legacy held-out diagnostic':`${diagnostic.samples} weighted samples · not Stage-13 final evaluation`);
  }

  function decorateCandidate(card,c){
    const m=c?.comparison||{};
    const cc=c?.confidence_contract||m?.confidence_contract||{};
    const final=c?.empirical_policy_quality||m?.empirical_policy_quality||cc?.final_evaluation||{};
    const details=card?.querySelector('.candidate-compare-details');
    const minimal=card?.querySelector('.candidate-compare-minimal');
    const alignment=c?.preference_alignment_score??m?.preference_alignment_score??m?.preference_confidence;
    ensureMetric(details,'preference-alignment','Preference alignment',pct(alignment),'selection metric · not a probability');
    if(final&&final.status){
      const q=final.accuracy==null?'—':`${pct(final.accuracy)} [${pct(final.quality_lower_bound)}, ${pct(final.quality_upper_bound)}]`;
      const ev=`n_eff ${n1(final.effective_n)} · ${final.status}`;
      ensureMetric(details,'final-quality','Final empirical quality',q,ev);
      const off=final.per_action?.OFF, on=final.per_action?.ON;
      ensureMetric(details,'off-safety','OFF future safety',off?.accuracy==null?'—':`${pct(off.accuracy)} · n_eff ${n1(off.effective_n)}`,
        off?.sufficient_evidence?'independent evidence ready':'insufficient independent OFF evidence');
      ensureMetric(details,'on-safety','ON future safety',on?.accuracy==null?'—':`${pct(on.accuracy)} · n_eff ${n1(on.effective_n)}`,
        on?.sufficient_evidence?'independent evidence ready':'insufficient independent ON evidence');
      const gate=final.sufficient_evidence?'future test locked':'Shadow / fallback';
      ensureMetric(minimal,'final-eval','Independent final evaluation',gate,`fixed target ${final.final_target??cc.final_min_independent_episodes??'—'}`);
      if(final.decision_strength_overstated){
        ensureMetric(details,'overstated-strength','Decision-strength warning','overstated','empirical future quality is materially below the decision-strength score');
      }
    }
  }

  function decorateAll(){
    renameLegacyLabels();
    for(const a of cachedAgents)decorateLive(a);
    for(const c of cachedCandidates){
      const ref=candidateRef(c);
      if(!ref)continue;
      const card=[...document.querySelectorAll('.candidate-agent')].find(x=>cardRef(x)===ref);
      if(card)decorateCandidate(card,c);
    }
  }

  async function refresh(){
    try{
      const [agents,candidates]=await Promise.all([
        fetch('api/agents',{cache:'no-store'}),
        fetch('api/candidates',{cache:'no-store'}),
      ]);
      if(agents.ok)cachedAgents=await agents.json();
      if(candidates.ok){
        const data=await candidates.json();
        cachedCandidates=data.candidates||[];
      }
    }catch(_e){}
    decorateAll();
  }

  // Observe only replacement of top-level agent cards. Observing the entire body with
  // subtree=true creates a self-triggering loop because confidence decoration itself
  // adds/updates descendants. Full refresh still runs every 2 s, so nested Candidate
  // updates don't need a body-wide observer.
  const root=document.getElementById('agents');
  if(root){
    let queued=false;
    new MutationObserver(()=>{
      if(queued)return;
      queued=true;
      queueMicrotask(()=>{queued=false;decorateAll();});
    }).observe(root,{childList:true});
  }
  setInterval(refresh,2000);
  refresh();
})();
