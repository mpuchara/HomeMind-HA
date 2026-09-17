(()=>{
  const pct=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const n1=v=>v==null?'—':Number(v).toFixed(1);
  let cachedCandidates=[];

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
  function ensureMetric(root,key,label,value,title=''){
    if(!root)return;
    let node=root.querySelector(`[data-confidence-metric="${key}"]`);
    if(!node){
      node=document.createElement('div');
      node.dataset.confidenceMetric=key;
      node.innerHTML='<span></span><b></b><small></small>';
      root.appendChild(node);
    }
    node.querySelector('span').textContent=label;
    node.querySelector('b').textContent=value;
    const small=node.querySelector('small');
    small.textContent=title;
    small.style.display=title?'':'none';
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
    for(const c of cachedCandidates){
      const ref=candidateRef(c);
      if(!ref)continue;
      const card=[...document.querySelectorAll('.candidate-agent')].find(x=>cardRef(x)===ref);
      if(card)decorateCandidate(card,c);
    }
  }

  async function refresh(){
    try{
      const r=await fetch('api/candidates',{cache:'no-store'});
      if(r.ok){
        const data=await r.json();
        cachedCandidates=data.candidates||[];
      }
    }catch(_e){}
    decorateAll();
  }

  const root=document.body;
  if(root)new MutationObserver(()=>decorateAll()).observe(root,{childList:true,subtree:true});
  setInterval(refresh,1500);
  refresh();
})();
