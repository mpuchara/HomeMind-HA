(()=>{
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot',"'":'&#39;'}[c]));
  const pct=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const sec=v=>v==null?'—':`${Number(v).toFixed(1)} s`;
  const signedSec=v=>v==null?'—':`${Number(v)>=0?'+':''}${Number(v).toFixed(1)} s`;
  const pp=v=>v==null?'—':`${Number(v)>=0?'+':''}${(Number(v)*100).toFixed(1)} pp`;
  const val=v=>v==null?'—':Number(v).toFixed(Math.abs(Number(v))<10?2:1);
  const parentGain=v=>v==null?'vs Parent —':`vs Parent ${Number(v)>=0?'+':''}${(Number(v)*100).toFixed(1)}%`;
  const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const b=await r.json();if(!r.ok)throw Error(b.error||`HTTP ${r.status}`);return b;};
  let busy=false;

  // P0 owns normal Live-agent nodes and periodically reconciles #agents. Candidate cards
  // are separate generation nodes, so detach/reattach them synchronously during Live render.
  const baseRenderAgents=window.renderAgents;
  if(typeof baseRenderAgents==='function'&&!window.__candidateCardRenderGuard){
    window.renderAgents=()=>{
      const root=document.getElementById('agents');
      if(!root)return baseRenderAgents();
      const candidates=[...root.querySelectorAll(':scope > .candidate-agent')];
      candidates.forEach(node=>node.remove());
      try{return baseRenderAgents();}
      finally{candidates.forEach(node=>root.appendChild(node));}
    };
    window.__candidateCardRenderGuard=true;
  }

  const stateLabel=c=>({queued:'Queued',building:'Fine-tuning',exploring:'Explore',comparing:'A/B comparison',ready:'Ready to promote',offline_blocked:'Offline gate blocked',insufficient_evidence:'Insufficient evidence',failed:'Failed',discarding:'Discarding',parent:'Parent / champion'}[c.state]||c.state);
  const statusText=(c,m,perAction)=>{
    if(c.stale)return 'New Correct / Change decision feedback arrived after this build snapshot — the same child will absorb the newer revision.';
    if(c.state==='queued')return 'Candidate is queued from its exact direct-parent snapshot.';
    if(c.state==='building')return 'Child training is running while the parent generation remains immutable.';
    if(c.state==='exploring')return 'Explore is collecting evidence while the direct parent remains immutable.';
    if(c.state==='offline_blocked')return 'Offline regression gate failed. Shadow prediction remains observable, but future A/B is blocked.';
    if(c.state==='insufficient_evidence')return 'Offline regression gate has insufficient historical evidence. Shadow remains observable; future A/B has not started.';
    if(m.per_action_ready===false)return `Promotion waits for ${perAction} future samples for each binary action.`;
    if(c.promotable)return 'Offline regression and paired future evidence passed the Candidate safety gates.';
    return 'The direct parent remains authoritative until enough paired future evidence is collected.';
  };
  const exploreLine=c=>{
    const x=c.explore;if(!x)return '';
    const mode=x.mode==='targeted_sensor'?'Targeted sensor':'Free exploration';
    const message=x.result_message||x.status||'collecting evidence';
    const sensor=x.targeted_sensor?` · ${x.targeted_sensor}`:'';
    return `<p class="candidate-small candidate-explore-result"><b>Explore · ${esc(mode)}</b>${esc(sensor)} · ${esc(message)}</p>`;
  };

  function card(c){
    const m=c.comparison||{},q=c.queue||{},gate=c.offline_gate||{};
    const progress=c.state==='building'?Math.max(0,Math.min(100,Math.round((c.training_progress||0)*100))):null;
    const queueText=q.state==='queued'?` · queue #${q.position||1}`:q.state==='active'?' · active':'';
    const perAction=m.required_future_samples_per_action||20;
    const correctionTotal=c.teach_fit_total;
    const correctionFit=correctionTotal==null?'—':`${c.teach_fit_before_count??0}/${correctionTotal} → ${c.teach_fit_after_count??0}/${correctionTotal}`;
    const regression=c.historical_regression_delta==null?'—':pp(c.historical_regression_delta);
    const gateSamples=c.historical_benchmark_samples==null?'—':String(c.historical_benchmark_samples);
    const generation=c.generation_number??c.generation;
    const timing=m.on_lead_gain_seconds!=null?`ON timing ${signedSec(m.on_lead_gain_seconds)}`:m.off_lead_gain_seconds!=null?`OFF timing ${signedSec(m.off_lead_gain_seconds)}`:null;
    return `<article class="agent candidate-agent" data-candidate-parent="${esc(c.parent_agent_id)}" data-generation-id="${esc(c.generation_id||'')}">
      <div class="candidate-top"><div><span class="candidate-badge">CANDIDATE</span><h3>${esc(c.parent_name)} · Gen ${esc(generation)}</h3></div><span class="candidate-state">${esc(stateLabel(c))}${queueText}</span></div>
      <p class="candidate-sub">Direct-parent snapshot → generation training → persistent Shadow → paired future A/B. Candidate is isolated from Executor.</p>
      ${progress==null?'':`<div class="candidate-progress"><span style="width:${progress}%"></span></div><p class="candidate-small">Training ${progress}% · build rev ${c.build_revision} / feedback rev ${c.feedback_revision}</p>`}
      ${c.last_error?`<p class="candidate-error">${esc(c.last_error)}</p>`:''}
      ${exploreLine(c)}
      <div class="candidate-compare candidate-compare-minimal">
        <div><span>Comparison</span><b>${esc(parentGain(m.accuracy_gain))}</b></div>
        <div><span>Future samples</span><b>${m.samples||0}</b></div>
        ${timing?`<div><span>Timing</span><b>${esc(timing)}</b></div>`:''}
      </div>
      <p class="candidate-small">${c.shadow_active?'Shadow is running on the same current context as its parent.':'No fresh Shadow observation yet — historical Desired stays a gap until this generation actually runs.'}</p>
      <p class="candidate-small">${statusText(c,m,perAction)}</p>
      <details class="candidate-details"><summary>Details</summary><div class="candidate-compare candidate-compare-details">
        <div><span>Current</span><b>${val(c.shadow_current)}</b></div>
        <div><span>Candidate Desired</span><b>${val(c.candidate_desired)}</b></div>
        <div><span>Confidence</span><b>${pct(c.candidate_confidence)}</b></div>
        <div><span>Correction fit</span><b>${esc(correctionFit)}</b></div>
        <div><span>Historical regression</span><b>${esc(regression)}</b></div>
        <div><span>Offline benchmark samples</span><b>${esc(gateSamples)}</b></div>
        <div><span>Offline gate</span><b>${esc(gate.status||'pending')}</b></div>
        <div><span>Parent accuracy</span><b>${pct(m.live_accuracy)}</b></div>
        <div><span>Candidate accuracy</span><b>${pct(m.candidate_accuracy)}</b></div>
        <div><span>Accuracy gain</span><b>${pp(m.accuracy_gain)}</b></div>
        <div><span>ON lead · Parent / Candidate</span><b>${sec(m.live_on_lead_seconds)} / ${sec(m.candidate_on_lead_seconds)}</b></div>
        <div><span>OFF lead · Parent / Candidate</span><b>${sec(m.live_off_lead_seconds)} / ${sec(m.candidate_off_lead_seconds)}</b></div>
        <div><span>False early · Parent / Candidate</span><b>${m.live_false_early||0} / ${m.candidate_false_early||0}</b></div>
        <div><span>Paired wins · Parent / Candidate</span><b>${m.live_wins||0} / ${m.candidate_wins||0}</b></div>
      </div></details>
      <div class="actions candidate-workflow-actions"><button class="ghost" data-wf="auto">Autonomous</button><button class="primary" data-wf="correct">Correct</button><button class="ghost" data-wf="explore">Explore</button><button class="ghost" data-wf="change">Change decision</button><button class="ghost" data-wf="settings">Settings</button></div>
      <div class="candidate-actions candidate-lifecycle-actions"><button class="primary" data-promote ${c.promotable?'':'disabled'}>Promote</button><button class="ghost" data-discard>Discard</button></div>
    </article>`;
  }

  async function refresh(){
    if(busy||document.hidden)return;
    busy=true;
    try{
      const data=await api('api/candidates');
      const root=document.getElementById('agents');if(!root)return;
      root.querySelectorAll('.candidate-agent').forEach(x=>x.remove());
      for(const c of data.candidates||[]){
        root.insertAdjacentHTML('beforeend',card(c));
        const el=root.lastElementChild,ref=c.generation_id||c.candidate_id;
        el.querySelector('[data-wf=auto]').onclick=e=>window.workflowAutonomous?.(ref,e.currentTarget);
        el.querySelector('[data-wf=correct]').onclick=()=>window.openWorkflowCorrect?.(ref);
        el.querySelector('[data-wf=explore]').onclick=()=>window.openExplore?.(ref);
        el.querySelector('[data-wf=change]').onclick=e=>window.workflowChangeDecision?.(ref,e.currentTarget);
        el.querySelector('[data-wf=settings]').onclick=()=>window.workflowSettings?.(ref);
        el.querySelector('[data-promote]').onclick=async()=>{
          if(!confirm(`Promote Candidate Gen ${c.generation_number??c.generation} for ${c.parent_name}? The new generation will start in Shadow.`))return;
          try{await api(`api/agents/${encodeURIComponent(c.parent_agent_id)}/candidate/promote`,{method:'POST',body:'{}'});await refresh();}
          catch(e){alert(e.message);}
        };
        el.querySelector('[data-discard]').onclick=async()=>{
          if(!confirm(`Discard Candidate Gen ${c.generation_number??c.generation}? The parent generation is not deleted.`))return;
          try{await api(`api/agents/${encodeURIComponent(c.parent_agent_id)}/candidate`,{method:'DELETE'});await refresh();}
          catch(e){alert(e.message);}
        };
      }
    }catch(_){/* runtime may still be starting; next poll retries */}
    finally{busy=false;}
  }

  window.refreshCandidates=refresh;
  async function loop(){await refresh();setTimeout(loop,1500);} loop();
})();
