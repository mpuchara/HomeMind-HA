(()=>{
  const pct=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const signed=v=>v==null?'—':`${Number(v)>=0?'+':''}${(Number(v)*100).toFixed(1)} pp`;
  const sec=v=>v==null?'—':`${Number(v).toFixed(1)} s`;
  let busy=false,liveBusy=false;
  const latestByRef=new Map();
  const DECISION_FIELDS=['parent_desired','candidate_desired','candidate_confidence','shadow_timestamp','target_property'];

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

  function decisionTile(label,value,classes='',key=''){
    const node=document.createElement('div');
    node.className=classes;
    node.dataset.decisionKey=key||String(label).toLowerCase().replace(/\s+/g,'-');
    const span=document.createElement('span');span.textContent=label;
    const bold=document.createElement('b');bold.textContent=value;
    node.append(span,bold);
    return node;
  }

  function ensureDecisionStrip(card,c){
    let strip=card.querySelector('.candidate-decision-strip');
    const values=[
      ['current','Current',decisionValue(c,c.shadow_current),'state-metric current'],
      ['desired','Desired',decisionValue(c,c.parent_desired),'state-metric desired'],
      ['candidate','Candidate Desired',decisionValue(c,c.candidate_desired),'state-metric candidate-desired'],
      ['confidence','Confidence',pct(c.candidate_confidence??c.model_confidence),''],
    ];
    if(!strip){
      strip=document.createElement('div');
      strip.className='agent-primary candidate-decision-strip';
      const anchor=card.querySelector('.candidate-compare-minimal');
      if(anchor)anchor.insertAdjacentElement('beforebegin',strip);
      else card.querySelector('.candidate-top')?.insertAdjacentElement('afterend',strip);
      strip.replaceChildren(
        decisionTile('Current',decisionValue(c,c.shadow_current),'state-metric current','current'),
        decisionTile('Desired',decisionValue(c,c.parent_desired),'state-metric desired','desired'),
        decisionTile('Candidate Desired',decisionValue(c,c.candidate_desired),'state-metric candidate-desired','candidate'),
        decisionTile('Confidence',pct(c.candidate_confidence??c.model_confidence),'','confidence'),
      );
      return;
    }
    // Fast refresh changes text only, avoiding a DOM rebuild four times per second.
    for(const [key,_label,value] of values){
      const bold=strip.querySelector(`[data-decision-key="${key}"] b`);
      if(bold&&bold.textContent!==value)bold.textContent=value;
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

    // There is one user-facing promotion action. Keep the least restrictive/custom path
    // and remove every standard/duplicate Promote button injected by older UI layers.
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

  function mergeCandidate(c,{live=false}={}){
    const ref=refOf(c);
    if(!ref)return null;
    const previous=latestByRef.get(ref)||{};
    const merged={...previous,...c};
    if(live){
      merged._liveSnapshotTs=Number(c.live_snapshot_ts||Date.now()/1000);
    }else if(previous._liveSnapshotTs&&Date.now()/1000-previous._liveSnapshotTs<2){
      // Full Candidate status can arrive after a fresher lightweight physical-state
      // snapshot. Never let the slower response rewind Current or a newer decision.
      merged.shadow_current=previous.shadow_current;
      const previousDecisionTs=Number(previous.shadow_timestamp||0);
      const incomingDecisionTs=Number(c.shadow_timestamp||0);
      if(previousDecisionTs>incomingDecisionTs){
        for(const key of DECISION_FIELDS)merged[key]=previous[key];
      }
      merged._liveSnapshotTs=previous._liveSnapshotTs;
    }
    latestByRef.set(ref,merged);
    return merged;
  }

  function decorate(c){
    const merged=mergeCandidate(c);
    if(!merged)return;
    decorateCard(findCard(refOf(merged)),merged);
  }

  function applyLiveSnapshots(items){
    for(const item of items||[]){
      const merged=mergeCandidate(item,{live:true});
      if(!merged)continue;
      const card=findCard(refOf(merged));
      if(card)ensureDecisionStrip(card,merged);
    }
  }

  async function refreshLive(){
    if(liveBusy||document.hidden)return;
    liveBusy=true;
    try{
      const response=await fetch('api/candidate-live',{cache:'no-store'});
      if(!response.ok)return;
      const data=await response.json();
      applyLiveSnapshots(data.candidates||[]);
    }catch(_e){
      // Full Candidate refresh remains the fallback if the lightweight route is unavailable.
    }finally{
      liveBusy=false;
    }
  }

  async function liveLoop(){
    await refreshLive();
    setTimeout(liveLoop,250);
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
      // Base candidate_ui rebuilds whole cards every poll. Rehydrate from the last
      // payload synchronously in the MutationObserver microtask so the browser never
      // paints a transient card without the decision/preference tiles. Only refetch
      // when a genuinely new generation appears and therefore has no cached payload.
      if(!missing||queued)return;
      queued=true;
      queueMicrotask(()=>{queued=false;refresh();});
    }).observe(root,{childList:true});
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshLive();refresh();}});
  setInterval(refresh,1500);
  liveLoop();
  refresh();
})();
