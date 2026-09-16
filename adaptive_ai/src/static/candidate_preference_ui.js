(()=>{
  const pct=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const signed=v=>v==null?'—':`${Number(v)>=0?'+':''}${(Number(v)*100).toFixed(1)} pp`;
  const sec=v=>v==null?'—':`${Number(v).toFixed(1)} s`;
  let busy=false;
  const latestByRef=new Map();

  const refOf=c=>String(c?.generation_id||c?.candidate_id||'');
  const cardRef=card=>String(card?.dataset?.candidateRef||card?.dataset?.generationId||'');
  const findCard=ref=>[...document.querySelectorAll('.candidate-agent')]
    .find(el=>cardRef(el)===String(ref));

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

  const decisionValue=(c,v)=>{
    if(v==null||v==='')return '—';
    const n=Number(v);
    if(!Number.isFinite(n))return '—';
    if(String(c.target_property||'')==='power')return n>=.5?'ON':'OFF';
    const abs=Math.abs(n);
    return n.toFixed(abs<10?2:1);
  };

  function ensureStyles(){
    if(document.getElementById('candidatePreferenceCardStyles'))return;
    const style=document.createElement('style');
    style.id='candidatePreferenceCardStyles';
    style.textContent=`
      .candidate-decision-strip{grid-template-columns:repeat(4,minmax(0,1fr));margin:12px 0 8px}
      .candidate-decision-strip .candidate-desired{border-color:#5a4f76;background:#181521}
      .candidate-decision-strip .candidate-desired b{color:#f4f7fb}
      .candidate-lifecycle-actions [data-promote-custom]{flex:1;min-width:0}
      @media(max-width:620px){.candidate-decision-strip{grid-template-columns:repeat(2,minmax(0,1fr))}}
    `;
    document.head.appendChild(style);
  }

  function decisionTile(key,label,classes=''){
    const node=document.createElement('div');
    node.className=classes;node.dataset.candidateDecision=key;
    const span=document.createElement('span');span.textContent=label;
    const bold=document.createElement('b');bold.textContent='—';
    node.append(span,bold);
    return node;
  }

  function ensureDecisionStrip(card,c){
    if(!card)return;
    let strip=card.querySelector('.candidate-decision-strip');
    if(!strip){
      strip=document.createElement('div');
      strip.className='agent-primary candidate-decision-strip';
      strip.append(
        decisionTile('current','Current','state-metric current'),
        decisionTile('desired','Desired','state-metric desired'),
        decisionTile('candidate','Candidate Desired','state-metric candidate-desired'),
        decisionTile('confidence','Confidence')
      );
      const anchor=card.querySelector('.candidate-compare-minimal');
      if(anchor)anchor.insertAdjacentElement('beforebegin',strip);
      else card.querySelector('.candidate-top')?.insertAdjacentElement('afterend',strip);
    }
    const values={
      current:decisionValue(c,c.shadow_current),
      desired:decisionValue(c,c.parent_desired),
      candidate:decisionValue(c,c.candidate_desired),
      confidence:pct(c.candidate_confidence??c.model_confidence),
    };
    for(const [key,value] of Object.entries(values)){
      const node=strip.querySelector(`[data-candidate-decision="${key}"] b`);
      if(node&&node.textContent!==value)node.textContent=value;
    }
  }

  function pruneDuplicatedDecisionDetails(card){
    const details=card.querySelector('.candidate-compare-details');
    if(!details)return;
    const duplicated=new Set(['Current','Candidate Desired','Confidence','Model confidence']);
    for(const row of [...details.children]){
      const label=row.querySelector('span')?.textContent?.trim();
      if(duplicated.has(label))row.remove();
    }
  }

  function normalizePromotion(card){
    const keep=card.querySelector('[data-promote-custom]');
    if(!keep)return;

    for(const button of [...card.querySelectorAll('button')]){
      if(button===keep)continue;
      const text=(button.textContent||'').trim().toLowerCase();
      if(button.matches('[data-promote]')||text.startsWith('promote'))button.remove();
    }

    keep.textContent='Promote';
    keep.classList.remove('ghost');
    keep.classList.add('primary');

    const lifecycle=card.querySelector('.candidate-lifecycle-actions');
    const discard=lifecycle?.querySelector('[data-discard]');
    if(lifecycle&&keep.parentElement!==lifecycle)lifecycle.insertBefore(keep,discard||null);

    const mode=card.querySelector('[data-promote-mode]');
    if(mode){
      mode.setAttribute('aria-label','Mode after promotion');
      for(const option of mode.options){
        if(option.value==='shadow')option.textContent='Shadow';
        if(option.value==='control')option.textContent='Control';
      }
    }

    const rules=card.querySelector('.candidate-custom-promotion');
    const description=rules?.querySelector(':scope > p');
    if(description){
      description.innerHTML='<b>Promotion rules</b> — choose the minimum future evidence you accept. Hard model/config, atomic swap and Control qualification checks remain mandatory.';
    }
  }

  function decorateCard(card,c){
    if(!card||!c)return;
    const m=c.comparison||{};

    ensureDecisionStrip(card,c);
    normalizePromotion(card);
    pruneDuplicatedDecisionDetails(card);

    const details=card.querySelector('.candidate-compare-details');
    if(details){
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

  function decorate(c){
    const ref=refOf(c);
    if(!ref)return;
    latestByRef.set(ref,c);
    decorateCard(findCard(ref),c);
  }

  function applyLiveCandidate(s){
    const ref=String(s?.generation_id||'');
    if(!ref)return;
    const cached=latestByRef.get(ref)||{generation_id:ref,target_property:s.target_property};
    cached.target_property=s.target_property??cached.target_property;
    cached.shadow_current=s.current;
    cached.parent_desired=s.parent_desired;
    cached.candidate_desired=s.candidate_desired;
    cached.candidate_confidence=s.candidate_confidence;
    cached.shadow_timestamp=s.shadow_timestamp;
    latestByRef.set(ref,cached);
    ensureDecisionStrip(findCard(ref),cached);
  }

  function hydrateAddedNode(node){
    if(!(node instanceof Element))return false;
    const cards=node.matches('.candidate-agent')?[node]:[...node.querySelectorAll('.candidate-agent')];
    let missing=false;
    for(const card of cards){
      const ref=cardRef(card);
      const cached=latestByRef.get(ref);
      if(cached)decorateCard(card,cached);
      else missing=true;
    }
    return missing;
  }

  async function refresh(){
    if(busy||document.hidden)return;
    busy=true;
    try{
      const response=await fetch('api/candidates',{cache:'no-store'});
      if(!response.ok)return;
      const data=await response.json();
      const liveRefs=new Set();
      for(const candidate of data.candidates||[]){
        const ref=refOf(candidate);
        if(ref)liveRefs.add(ref);
        decorate(candidate);
      }
      for(const ref of [...latestByRef.keys()])if(!liveRefs.has(ref))latestByRef.delete(ref);
    }catch(_e){
      // The base Candidate UI owns connectivity/error messaging.
    }finally{
      busy=false;
    }
  }

  ensureStyles();
  const root=document.getElementById('agents');
  if(root){
    let queued=false;
    new MutationObserver(records=>{
      let missing=false;
      for(const record of records){
        for(const node of record.addedNodes)missing=hydrateAddedNode(node)||missing;
      }
      if(!missing||queued)return;
      queued=true;
      queueMicrotask(()=>{queued=false;refresh();});
    }).observe(root,{childList:true});
  }

  async function refreshLiveDecisions(){
    if(document.hidden)return;
    try{
      const response=await fetch('api/candidate-live',{cache:'no-store'});
      if(!response.ok)return;
      const data=await response.json();
      for(const candidate of data.candidates||[])applyLiveCandidate(candidate);
    }catch(_){/* runtime may be starting; next 250 ms tick retries */}
  }

  refreshLiveDecisions();
  setInterval(refreshLiveDecisions,250);
  refresh();
})();
