(()=>{
  const pct=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const signed=v=>v==null?'—':`${Number(v)>=0?'+':''}${(Number(v)*100).toFixed(1)} pp`;
  const sec=v=>v==null?'—':`${Number(v).toFixed(1)} s`;
  let busy=false;

  const findCard=ref=>[...document.querySelectorAll('.candidate-agent')]
    .find(el=>(el.dataset.candidateRef||el.dataset.generationId||'')===String(ref));

  const ensureMetric=(root,key,label,value)=>{
    let node=root.querySelector(`[data-pref-metric="${key}"]`);
    if(!node){
      node=document.createElement('div');
      node.dataset.prefMetric=key;
      node.innerHTML='<span></span><b></b>';
      root.appendChild(node);
    }
    node.querySelector('span').textContent=label;
    node.querySelector('b').textContent=value;
  };

  function decorate(c){
    const ref=String(c.generation_id||c.candidate_id||'');
    const card=findCard(ref);
    if(!card)return;
    const m=c.comparison||{};

    const details=card.querySelector('.candidate-compare-details');
    if(details){
      for(const row of details.children){
        const label=row.querySelector('span');
        if(label&&label.textContent.trim()==='Confidence')label.textContent='Model confidence';
      }
      ensureMetric(details,'preference-confidence','Preference confidence',pct(c.preference_confidence??m.preference_confidence));
      ensureMetric(details,'opportunities','Confirmed opportunities',String(m.meaningful_opportunities??m.samples??0));
      ensureMetric(details,'corrections','Corrections since generation',String(m.manual_corrections_since_generation??0));
      ensureMetric(details,'correction-rate','Corrections / 100 opportunities',m.corrections_per_100_opportunities==null?'—':Number(m.corrections_per_100_opportunities).toFixed(1));
      ensureMetric(details,'teach-anchor','Teach anchors',m.teach_anchor_total?`${pct(m.teach_anchor_fit)} · ${m.teach_anchor_total}`:'—');
      if(m.comparison_metric==='fast_timing_preference'){
        ensureMetric(details,'timing-gain','Timing utility vs Parent',signed(m.timing_objective_gain));
        ensureMetric(details,'off-lead','OFF lead · Parent / Candidate',`${sec(m.fast_off_parent_lead_seconds)} / ${sec(m.fast_off_candidate_lead_seconds)}`);
        ensureMetric(details,'on-lead','ON lead · Parent / Candidate',`${sec(m.fast_on_parent_lead_seconds)} / ${sec(m.fast_on_candidate_lead_seconds)}`);
      }
    }

    const minimal=card.querySelector('.candidate-compare-minimal');
    if(minimal){
      ensureMetric(minimal,'preference-confidence','Preference confidence',pct(c.preference_confidence??m.preference_confidence));
      if(m.comparison_metric==='fast_timing_preference'){
        ensureMetric(minimal,'timing-gain','Timing vs Parent',signed(m.timing_objective_gain));
      }
    }
  }

  async function refresh(){
    if(busy||document.hidden)return;
    busy=true;
    try{
      const response=await fetch('api/candidates',{cache:'no-store'});
      if(!response.ok)return;
      const data=await response.json();
      for(const candidate of data.candidates||[])decorate(candidate);
    }catch(_e){
      // The base Candidate UI owns connectivity/error messaging.
    }finally{
      busy=false;
    }
  }

  const root=document.getElementById('agents');
  if(root){
    let queued=false;
    new MutationObserver(()=>{
      if(queued)return;
      queued=true;
      queueMicrotask(()=>{queued=false;refresh();});
    }).observe(root,{childList:true,subtree:true});
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  setInterval(refresh,2500);
  refresh();
})();
