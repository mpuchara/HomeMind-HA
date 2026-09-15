(()=>{
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot',"'":'&#39;'}[c]));
  const pct=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const sec=v=>v==null?'—':`${Number(v).toFixed(1)} s`;
  const pp=v=>v==null?'—':`${Number(v)>=0?'+':''}${(Number(v)*100).toFixed(1)} pp`;
  const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const b=await r.json();if(!r.ok)throw Error(b.error||`HTTP ${r.status}`);return b;};
  let busy=false;

  // P0 owns the normal Live-agent nodes and periodically removes unknown children from
  // #agents. Candidate cards are intentionally separate UI nodes, so preserve them
  // across that reconciliation instead of letting them disappear until the next 1.5 s
  // Candidate refresh. Detach/reattach is synchronous, so the browser never paints a gap.
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

  const stateLabel=c=>({queued:'Queued',building:'Fine-tuning',comparing:'A/B comparison',ready:'Ready to promote',offline_blocked:'Offline gate blocked',insufficient_evidence:'Insufficient evidence',failed:'Failed',discarding:'Discarding'}[c.state]||c.state);
  const statusText=(c,m,perAction)=>{
    if(c.stale)return 'New Teach feedback arrived after this build snapshot — a newer snapshot will be corrected next.';
    if(c.state==='queued')return 'Candidate is queued to clone the current Live generation and apply this Teach revision.';
    if(c.state==='building')return 'Training the current Teach revision by fine-tuning the exact Live snapshot. Correct keeps the parent schema and does not run a full rebuild.';
    if(c.state==='offline_blocked')return 'Offline regression gate failed. Future A/B is blocked until a safe Candidate is built.';
    if(c.state==='insufficient_evidence')return 'Offline regression gate has insufficient non-Teach historical evidence. Future A/B has not started.';
    if(m.per_action_ready===false)return `Promotion waits for ${perAction} future samples for each binary action.`;
    if(c.promotable)return 'Offline regression and future evidence passed the Candidate safety gates.';
    return 'Live remains authoritative until enough future evidence is collected.';
  };
  function card(c){
    const m=c.comparison||{}, q=c.queue||{}, gate=c.offline_gate||{};
    const progress=c.state==='building'?Math.max(0,Math.min(100,Math.round((c.training_progress||0)*100))):null;
    const gain=m.accuracy_gain==null?'—':`${m.accuracy_gain>=0?'+':''}${(m.accuracy_gain*100).toFixed(1)} pp`;
    const queueText=q.state==='queued'?` · queue #${q.position||1}`:q.state==='active'?' · active':'';
    const perAction=m.required_future_samples_per_action||20;
    const teachTotal=c.teach_fit_total;
    const teachFit=teachTotal==null?'—':`${c.teach_fit_before_count??0}/${teachTotal} → ${c.teach_fit_after_count??0}/${teachTotal}`;
    const regression=c.historical_regression_delta==null?'—':pp(c.historical_regression_delta);
    const gateSamples=c.historical_benchmark_samples==null?'—':String(c.historical_benchmark_samples);
    return `<article class="agent candidate-agent" data-candidate-parent="${esc(c.parent_agent_id)}">
      <div class="candidate-top"><div><span class="candidate-badge">CANDIDATE</span><h3>${esc(c.parent_name)} · Gen ${c.generation}</h3></div><span class="candidate-state">${esc(stateLabel(c))}${queueText}</span></div>
      <p class="candidate-sub">Exact Live snapshot → conservative Correct → offline regression gate → future A/B. Candidate is isolated from Executor.</p>
      ${progress==null?'':`<div class="candidate-progress"><span style="width:${progress}%"></span></div><p class="candidate-small">Fine-tuning ${progress}% · build rev ${c.build_revision} / feedback rev ${c.feedback_revision}</p>`}
      ${c.last_error?`<p class="candidate-error">${esc(c.last_error)}</p>`:''}
      <div class="candidate-compare">
        <div><span>Teach fit</span><b>${esc(teachFit)}</b></div>
        <div><span>Historical regression</span><b>${esc(regression)}</b></div>
        <div><span>Offline benchmark samples</span><b>${esc(gateSamples)}</b></div>
        <div><span>Offline gate</span><b>${esc(gate.status||'pending')}</b></div>
        <div><span>Future samples</span><b>${m.samples||0}</b></div>
        <div><span>Live accuracy</span><b>${pct(m.live_accuracy)}</b></div>
        <div><span>Candidate accuracy</span><b>${pct(m.candidate_accuracy)}</b></div>
        <div><span>Accuracy gain</span><b>${gain}</b></div>
        <div><span>ON lead · Live / Candidate</span><b>${sec(m.live_on_lead_seconds)} / ${sec(m.candidate_on_lead_seconds)}</b></div>
        <div><span>OFF lead · Live / Candidate</span><b>${sec(m.live_off_lead_seconds)} / ${sec(m.candidate_off_lead_seconds)}</b></div>
        <div><span>False early · Live / Candidate</span><b>${m.live_false_early||0} / ${m.candidate_false_early||0}</b></div>
        <div><span>Paired wins · Live / Candidate</span><b>${m.live_wins||0} / ${m.candidate_wins||0}</b></div>
      </div>
      <p class="candidate-small">${statusText(c,m,perAction)}</p>
      <div class="candidate-actions"><button class="primary" data-promote ${c.promotable?'':'disabled'}>Promote</button><button class="ghost" data-discard>Discard</button></div>
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
        const el=root.lastElementChild;
        el.querySelector('[data-promote]').onclick=async()=>{
          if(!confirm(`Promote Candidate Gen ${c.generation} for ${c.parent_name}? The new generation will start in Shadow.`))return;
          try{await api(`api/agents/${encodeURIComponent(c.parent_agent_id)}/candidate/promote`,{method:'POST',body:'{}'});await refresh();}
          catch(e){alert(e.message);}
        };
        el.querySelector('[data-discard]').onclick=async()=>{
          if(!confirm(`Discard Candidate Gen ${c.generation}? Live is not affected.`))return;
          try{await api(`api/agents/${encodeURIComponent(c.parent_agent_id)}/candidate`,{method:'DELETE'});await refresh();}
          catch(e){alert(e.message);}
        };
      }
    }catch(_){/* runtime may still be starting; next poll retries */}
    finally{busy=false;}
  }

  // manual_feedback.js owns the Teach dialog. Candidate mode only adjusts its explanatory
  // copy: adding/undoing points stays available while the isolated Candidate trains.
  const originalOpenTeach=window.openTeach;
  if(typeof originalOpenTeach==='function'){
    window.openTeach=id=>{
      originalOpenTeach(id);
      queueMicrotask(()=>{
        const dialog=document.getElementById('teachDialog');if(!dialog?.open)return;
        const intro=dialog.querySelector('.teach-head + p');
        if(intro)intro.textContent='Dodaj prawidłowe Desired. Każdy punkt koryguje snapshot Candidate; Live agent pracuje bez przerwy.';
        const train=dialog.querySelector('[data-train]');if(train)train.textContent='Build Candidate now';
        const notes=dialog.querySelectorAll('p');
        if(notes.length)notes[notes.length-1].textContent='Correct zachowuje model i sensory Live. Po korekcie Candidate musi przejść offline regression gate, zanim zacznie future A/B.';
      });
    };
  }

  window.refreshCandidates=refresh;
  async function loop(){await refresh();setTimeout(loop,1500);} loop();
})();
