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
  const uiState=new Map();
  // Promote is an explicit human acceptance action. Default evidence rules therefore do
  // not add a second hidden gate; hard model/config/atomic/Control guards stay backend-owned.
  const defaultUi=()=>({detailsOpen:false,minFuture:'0',minPerAction:'0',maxRegression:'',allowOffline:true});
  const candidateRef=c=>String(c.generation_id||c.candidate_id||'');
  const stateFor=ref=>{if(!uiState.has(ref))uiState.set(ref,defaultUi());return uiState.get(ref);};
  const candidateBaseName=c=>{
    const raw=String(c?.parent_name||c?.candidate_name||c?.root_agent_id||c?.parent_agent_id||'Agent').trim();
    return raw.replace(/(?:\s*[·-]\s*Candidate)+\s*$/i,'').trim()||raw;
  };
  const candidateTitle=c=>String(c?.candidate_name||`${candidateBaseName(c)} · Candidate`).replace(/(?:\s*[·-]\s*Candidate)+\s*$/i,' · Candidate').trim();
  const candidateGeneration=c=>c?.candidate_generation_number??c?.generation_number??c?.generation??1;

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
    if(c.state==='offline_blocked')return 'Offline regression gate blocks automatic promotion, but passive Shadow A/B evidence keeps accumulating. Explicit Promote may accept the current evidence after confirmation.';
    if(c.state==='insufficient_evidence')return 'Offline history is insufficient for automatic promotion. Explicit Promote may accept the current evidence after confirmation.';
    if(m.per_action_ready===false)return `Automatic promotion waits for ${perAction} future samples for each binary action; explicit Promote remains a user choice.`;
    if(c.promotable)return 'Offline regression and paired future evidence passed the standard Candidate safety gates.';
    return 'The direct parent remains authoritative until standard gates pass or you explicitly confirm Promote.';
  };
  const exploreLine=c=>{
    const x=c.explore;if(!x)return '';
    const mode=x.mode==='targeted_sensor'?'Targeted sensor':'Free exploration';
    const message=x.result_message||x.status||'collecting evidence';
    const sensor=x.targeted_sensor?` · ${x.targeted_sensor}`:'';
    return `<p class="candidate-small candidate-explore-result"><b>Explore · ${esc(mode)}</b>${esc(sensor)} · ${esc(message)}</p>`;
  };
  const automationNames=xs=>(xs||[]).map(x=>esc(x.name||x.entity_id)).join(' · ')||'none';
  const ownershipDetails=c=>{
    const o=c.automation_ownership||{};
    return `<div class="candidate-small candidate-automation-ownership">
      <b>Currently controlling target:</b> ${automationNames(o.currently_controlling)}<br>
      <b>Disabled by HomeMind:</b> ${automationNames(o.disabled_by_homemind)}<br>
      <b>Previously linked:</b> ${automationNames(o.previously_linked)}
      ${o.ownership_valid===false?'<br><b class="candidate-error">Ownership metadata mismatch</b>':''}
    </div>`;
  };

  function card(c,draft){
    const m=c.comparison||{},q=c.queue||{},gate=c.offline_gate||{};
    const progress=c.state==='building'?Math.max(0,Math.min(100,Math.round((c.training_progress||0)*100))):null;
    const queueText=q.state==='queued'?` · queue #${q.position||1}`:q.state==='active'?' · active':'';
    const perAction=m.required_future_samples_per_action||20;
    const correctionTotal=c.teach_fit_total;
    const correctionFit=correctionTotal==null?'—':`${c.teach_fit_before_count??0}/${correctionTotal} → ${c.teach_fit_after_count??0}/${correctionTotal}`;
    const regression=c.historical_regression_delta==null?'—':pp(c.historical_regression_delta);
    const gateSamples=c.historical_benchmark_samples==null?'—':String(c.historical_benchmark_samples);
    const generation=candidateGeneration(c);
    const timing=m.on_lead_gain_seconds!=null?`ON timing ${signedSec(m.on_lead_gain_seconds)}`:m.off_lead_gain_seconds!=null?`OFF timing ${signedSec(m.off_lead_gain_seconds)}`:null;
    const targetMode=c.promotion_target_mode==='control'?'control':'shadow';
    const gateReason=(gate.reasons||[]).join(' · ')||'—';
    const customEligible=c.training_state==='qualified'&&!['queued','building','exploring','failed','discarding'].includes(c.state);
    return `<article class="agent candidate-agent" data-candidate-parent="${esc(c.parent_agent_id)}" data-generation-id="${esc(c.generation_id||'')}" data-candidate-ref="${esc(candidateRef(c))}">
      <div class="candidate-top"><div><span class="candidate-badge">CANDIDATE</span><h3>${esc(candidateTitle(c))} · Gen ${esc(generation)}</h3></div><span class="candidate-state">${esc(stateLabel(c))}${queueText}</span></div>
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
      <p class="candidate-small"><b>Physical Candidate mode: ${esc(c.candidate_physical_mode||'shadow').toUpperCase()}</b> · promotion target ${esc(targetMode.toUpperCase())} · current Live ${esc((c.live_mode||'shadow').toUpperCase())}. Target mode never grants Candidate dispatch authority.</p>
      <p class="candidate-small">${statusText(c,m,perAction)}</p>
      <details class="candidate-details" ${draft.detailsOpen?'open':''}><summary>Details</summary><div class="candidate-compare candidate-compare-details">
        <div><span>Current</span><b>${val(c.shadow_current)}</b></div>
        <div><span>Candidate Desired</span><b>${val(c.candidate_desired)}</b></div>
        <div><span>Confidence</span><b>${pct(c.candidate_confidence)}</b></div>
        <div><span>Correction fit</span><b>${esc(correctionFit)}</b></div>
        <div><span>Historical regression</span><b>${esc(regression)}</b></div>
        <div><span>Offline benchmark samples</span><b>${esc(gateSamples)}</b></div>
        <div><span>Offline gate</span><b>${esc(gate.status||'pending')}</b></div>
        <div><span>Offline gate reason</span><b>${esc(gateReason)}</b></div>
        <div><span>Parent accuracy</span><b>${pct(m.live_accuracy)}</b></div>
        <div><span>Candidate accuracy</span><b>${pct(m.candidate_accuracy)}</b></div>
        <div><span>Accuracy gain</span><b>${pp(m.accuracy_gain)}</b></div>
        <div><span>ON future samples</span><b>${m.on_events||0}</b></div>
        <div><span>OFF future samples</span><b>${m.off_events||0}</b></div>
        <div><span>ON lead · Parent / Candidate</span><b>${sec(m.live_on_lead_seconds)} / ${sec(m.candidate_on_lead_seconds)}</b></div>
        <div><span>OFF lead · Parent / Candidate</span><b>${sec(m.live_off_lead_seconds)} / ${sec(m.candidate_off_lead_seconds)}</b></div>
        <div><span>False early · Parent / Candidate</span><b>${m.live_false_early||0} / ${m.candidate_false_early||0}</b></div>
        <div><span>Paired wins · Parent / Candidate</span><b>${m.live_wins||0} / ${m.candidate_wins||0}</b></div>
      </div>
      <div class="candidate-custom-promotion">
        <p class="candidate-small"><b>Promotion rules</b> — by default explicit confirmation accepts the Candidate with the evidence currently available. You can tighten these thresholds; hard model/config checks, atomic swap and Control qualification are never bypassed.</p>
        <div class="candidate-compare candidate-custom-rules">
          <label><span>Min future samples</span><input type="number" min="0" step="1" data-custom-min-future value="${esc(draft.minFuture)}"></label>
          <label><span>Min ON/OFF each</span><input type="number" min="0" step="1" data-custom-min-action value="${esc(draft.minPerAction)}"></label>
          <label><span>Max future regression (pp)</span><input type="number" min="0" max="100" step="0.5" data-custom-max-regression value="${esc(draft.maxRegression)}" placeholder="blank = ignore"></label>
          <label><span>Offline gate</span><span><input type="checkbox" data-custom-offline ${draft.allowOffline?'checked':''}> allow explicit override</span></label>
        </div>
        <button class="ghost" data-promote-custom ${customEligible?'':'disabled'}>Promote</button>
      </div>
      ${ownershipDetails(c)}</details>
      <div class="actions candidate-workflow-actions"><button class="ghost" data-wf="auto">Autonomous</button><button class="primary" data-wf="correct">Correct</button><button class="ghost" data-wf="explore">Explore</button><button class="ghost" data-wf="change">Change decision</button><button class="ghost" data-wf="settings">Settings</button></div>
      <div class="candidate-actions candidate-lifecycle-actions">
        <select data-promote-mode aria-label="Promotion target mode"><option value="shadow" ${targetMode==='shadow'?'selected':''}>Promote as Shadow</option><option value="control" ${targetMode==='control'?'selected':''}>Promote as Control</option></select>
        <button class="primary" data-promote ${c.promotable?'':'disabled'}>Promote</button><button class="ghost" data-discard>Discard</button>
      </div>
    </article>`;
  }

  function remember(el,ref){
    const state=stateFor(ref);
    const details=el.querySelector('.candidate-details');
    if(details)state.detailsOpen=details.open;
    const minFuture=el.querySelector('[data-custom-min-future]');if(minFuture)state.minFuture=minFuture.value;
    const minAction=el.querySelector('[data-custom-min-action]');if(minAction)state.minPerAction=minAction.value;
    const maxRegression=el.querySelector('[data-custom-max-regression]');if(maxRegression)state.maxRegression=maxRegression.value;
    const offline=el.querySelector('[data-custom-offline]');if(offline)state.allowOffline=offline.checked;
  }

  async function refresh(){
    if(busy||document.hidden)return;
    busy=true;
    try{
      const data=await api('api/candidates');
      const root=document.getElementById('agents');if(!root)return;
      root.querySelectorAll('.candidate-agent').forEach(el=>{remember(el,el.dataset.candidateRef||el.dataset.generationId||el.dataset.candidateParent||'');el.remove();});
      for(const c of data.candidates||[]){
        const ref=candidateRef(c),draft=stateFor(ref);
        root.insertAdjacentHTML('beforeend',card(c,draft));
        const el=root.lastElementChild;
        const details=el.querySelector('.candidate-details');
        if(details)details.ontoggle=()=>{draft.detailsOpen=details.open;};
        ['[data-custom-min-future]','[data-custom-min-action]','[data-custom-max-regression]','[data-custom-offline]'].forEach(sel=>{
          const node=el.querySelector(sel);if(node)node.onchange=()=>remember(el,ref);
        });
        el.querySelector('[data-wf=auto]').onclick=e=>window.workflowAutonomous?.(ref,e.currentTarget);
        el.querySelector('[data-wf=correct]').onclick=()=>window.openWorkflowCorrect?.(ref);
        el.querySelector('[data-wf=explore]').onclick=()=>window.openExplore?.(ref);
        el.querySelector('[data-wf=change]').onclick=e=>window.workflowChangeDecision?.(ref,e.currentTarget);
        el.querySelector('[data-wf=settings]').onclick=()=>window.workflowSettings?.(ref);
        const modeSelect=el.querySelector('[data-promote-mode]');
        modeSelect.onchange=async()=>{
          const requested=modeSelect.value;modeSelect.disabled=true;
          try{await api(`api/agents/${encodeURIComponent(c.parent_agent_id)}/candidate/target-mode`,{method:'POST',body:JSON.stringify({target_mode:requested})});}
          catch(e){alert(e.message);await refresh();}
          finally{modeSelect.disabled=false;}
        };
        el.querySelector('[data-promote]').onclick=async()=>{
          const targetMode=modeSelect.value,generation=candidateGeneration(c),title=candidateTitle(c);
          const continuity=targetMode===(c.live_mode||'shadow')?'preserving the current Live mode':`changing Live from ${(c.live_mode||'shadow').toUpperCase()} to ${targetMode.toUpperCase()}`;
          if(!confirm(`Promote ${title} · Gen ${generation} as ${targetMode.toUpperCase()}?\n\nYes will atomically replace the active agent with this Candidate, ${continuity}. The Candidate card will disappear after the commit and the next Candidate cycle will start at Gen 1.`))return;
          try{
            await api(`api/agents/${encodeURIComponent(c.parent_agent_id)}/candidate/promote`,{method:'POST',body:JSON.stringify({target_mode:targetMode})});
            el.remove();uiState.delete(ref);await refresh();if(typeof window.load==='function')await window.load();
          }catch(e){alert(e.message);}
        };
        const customBtn=el.querySelector('[data-promote-custom]');
        if(customBtn)customBtn.onclick=async()=>{
          remember(el,ref);
          const d=stateFor(ref),targetMode=modeSelect.value,generation=candidateGeneration(c),title=candidateTitle(c);
          const maxRegression=d.maxRegression===''?null:Number(d.maxRegression);
          const conditions={
            min_future_samples:Math.max(0,Number.parseInt(d.minFuture||'0',10)||0),
            min_per_binary_action:Math.max(0,Number.parseInt(d.minPerAction||'0',10)||0),
            max_future_regression_pp:Number.isFinite(maxRegression)?maxRegression:null,
            allow_offline_gate_override:!!d.allowOffline,
          };
          const warning=conditions.allow_offline_gate_override&&!((c.offline_gate||{}).passed)?'\n\nWARNING: this confirmation explicitly accepts the blocked/insufficient offline historical gate.':'';
          const tightened=conditions.min_future_samples>0||conditions.min_per_binary_action>0||conditions.max_future_regression_pp!=null;
          const rules=tightened?`\n\nPromotion rules: ${conditions.min_future_samples} future samples, ${conditions.min_per_binary_action} ON/OFF each, max regression ${conditions.max_future_regression_pp==null?'ignored':conditions.max_future_regression_pp.toFixed(1)+' pp'}.`:'';
          if(!confirm(`Promote ${title} · Gen ${generation} as ${targetMode.toUpperCase()}?\n\nYes will atomically replace the active agent with this Candidate. The Candidate card will disappear after the commit and the next Candidate cycle will start at Gen 1.${rules}${warning}\n\nHard model/config/atomic/Control qualification checks still apply.`))return;
          customBtn.disabled=true;
          try{
            await api(`api/agents/${encodeURIComponent(c.parent_agent_id)}/candidate/promote-custom`,{method:'POST',body:JSON.stringify({target_mode:targetMode,conditions})});
            el.remove();uiState.delete(ref);await refresh();if(typeof window.load==='function')await window.load();
          }catch(e){alert(e.message);}
          finally{if(customBtn.isConnected)customBtn.disabled=false;}
        };
        el.querySelector('[data-discard]').onclick=async()=>{
          if(!confirm(`Discard ${candidateTitle(c)} · Gen ${candidateGeneration(c)}? The parent generation is not deleted.`))return;
          try{await api(`api/agents/${encodeURIComponent(c.parent_agent_id)}/candidate`,{method:'DELETE'});uiState.delete(ref);await refresh();}
          catch(e){alert(e.message);}
        };
      }
    }catch(_){/* runtime may still be starting; next poll retries */}
    finally{busy=false;}
  }

  window.refreshCandidates=refresh;
  // Candidate lifecycle is not a realtime control signal. Match the main 4 s UI cadence\n  // instead of running a second 1.5 s DB/status poller on Raspberry Pi.\n  async function loop(){await refresh();setTimeout(loop,4000);} loop();
})();