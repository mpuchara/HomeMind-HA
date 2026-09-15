(()=>{
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot',"'":'&#39;'}[c]));
  const pct=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const sec=v=>v==null?'—':`${Number(v).toFixed(1)} s`;
  const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const b=await r.json();if(!r.ok)throw Error(b.error||`HTTP ${r.status}`);return b;};
  let busy=false;

  const stateLabel=c=>({queued:'Queued',building:'Training',comparing:'A/B comparison',ready:'Ready to promote',failed:'Failed',discarding:'Discarding'}[c.state]||c.state);
  function card(c){
    const m=c.comparison||{}, q=c.queue||{};
    const progress=c.state==='building'?Math.max(0,Math.min(100,Math.round((c.training_progress||0)*100))):null;
    const gain=m.accuracy_gain==null?'—':`${m.accuracy_gain>=0?'+':''}${(m.accuracy_gain*100).toFixed(1)} pp`;
    const queueText=q.state==='queued'?` · queue #${q.position||1}`:q.state==='active'?' · active':'';
    const perAction=m.required_future_samples_per_action||20;
    return `<article class="agent candidate-agent" data-candidate-parent="${esc(c.parent_agent_id)}">
      <div class="candidate-top"><div><span class="candidate-badge">CANDIDATE</span><h3>${esc(c.parent_name)} · Gen ${c.generation}</h3></div><span class="candidate-state">${esc(stateLabel(c))}${queueText}</span></div>
      <p class="candidate-sub">Next generation is isolated from Executor. Live keeps serving while this model trains and compares.</p>
      ${progress==null?'':`<div class="candidate-progress"><span style="width:${progress}%"></span></div><p class="candidate-small">Training ${progress}% · feedback rev ${c.build_revision}/${c.feedback_revision}</p>`}
      ${c.last_error?`<p class="candidate-error">${esc(c.last_error)}</p>`:''}
      <div class="candidate-compare">
        <div><span>Future samples</span><b>${m.samples||0}</b></div>
        <div><span>Live accuracy</span><b>${pct(m.live_accuracy)}</b></div>
        <div><span>Candidate accuracy</span><b>${pct(m.candidate_accuracy)}</b></div>
        <div><span>Accuracy gain</span><b>${gain}</b></div>
        <div><span>ON lead · Live / Candidate</span><b>${sec(m.live_on_lead_seconds)} / ${sec(m.candidate_on_lead_seconds)}</b></div>
        <div><span>OFF lead · Live / Candidate</span><b>${sec(m.live_off_lead_seconds)} / ${sec(m.candidate_off_lead_seconds)}</b></div>
        <div><span>False early · Live / Candidate</span><b>${m.live_false_early||0} / ${m.candidate_false_early||0}</b></div>
        <div><span>Paired wins · Live / Candidate</span><b>${m.live_wins||0} / ${m.candidate_wins||0}</b></div>
      </div>
      <p class="candidate-small">${c.stale?'New feedback arrived — another build is required.':m.per_action_ready===false?`Promotion waits for ${perAction} future samples for each binary action.`:c.promotable?'Future evidence passed the Candidate safety gate.':'Live remains authoritative until enough future evidence is collected.'}</p>
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
        if(intro)intro.textContent='Dodaj prawidłowe Desired. Każdy punkt aktualizuje dane Candidate; Live agent pracuje bez przerwy.';
        const train=dialog.querySelector('[data-train]');if(train)train.textContent='Build Candidate now';
        const notes=dialog.querySelectorAll('p');
        if(notes.length)notes[notes.length-1].textContent='Candidate uczy się obok Live. Kolejne punkty Teach mogą być dodawane podczas treningu; jeśli zmienią dane, Candidate przebuduje nowszą rewizję.';
      });
    };
  }

  window.refreshCandidates=refresh;
  async function loop(){await refresh();setTimeout(loop,1500);} loop();
})();
