(()=>{
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const names=xs=>(xs||[]).map(x=>esc(x.name||x.entity_id)).join(' · ')||'none';

  function decorateOwnership(){
    if(!Array.isArray(window.lastAgents)&&typeof lastAgents==='undefined')return;
    const agents=typeof lastAgents==='undefined'?window.lastAgents:lastAgents;
    for(const a of agents||[]){
      const details=[...document.querySelectorAll('.agent-details[data-agent-id]')].find(x=>String(x.dataset.agentId)===String(a.id));
      if(!details)continue;
      const ownership=a.runtime?.automation_ownership||{};
      const current=ownership.currently_controlling||[];
      const owned=ownership.disabled_by_homemind||[];
      const previous=ownership.previously_linked||[];
      let summary=details.parentElement?.querySelector(':scope > .automation-ownership-summary');
      if(!summary){
        summary=document.createElement('div');summary.className='automation-prior automation-ownership-summary';
        details.insertAdjacentElement('beforebegin',summary);
      }
      summary.innerHTML=`<b>Automation ownership</b><span class="prior-chip">active ${current.length}</span><span class="prior-chip">HomeMind-disabled ${owned.length}</span><span class="prior-chip">known/previous ${previous.length}</span>`;
      let box=details.querySelector('.automation-ownership-detail');
      if(!box){
        box=document.createElement('div');box.className='detail automation-ownership-detail';
        details.appendChild(box);
      }
      box.innerHTML=`<b>Currently controlling target:</b> ${names(current)}<br><b>Disabled by HomeMind:</b> ${names(owned)}<br><b>Previously linked:</b> ${names(previous)}<br><span class="muted">Ownership lease is authoritative: Shadow restores only automations HomeMind disabled during takeover. Automations that were already disabled by the user remain off.</span>${ownership.ownership_valid===false?'<br><b>⚠ Ownership metadata mismatch</b>':''}`;
    }
  }

  const previousRender=window.renderAgents;
  if(typeof previousRender==='function'&&!window.__promotionLifecycleRenderer){
    window.renderAgents=()=>{const value=previousRender();decorateOwnership();return value;};
    window.__promotionLifecycleRenderer=true;
  }

  // Replace only the user-facing lifecycle message. Backend PATCH semantics remain the
  // established target-lock/release/takeover path; this text now accurately describes the
  // ownership lease instead of claiming Shadow never restores automations.
  window.setMode=async(id,mode)=>{
    if(mode==='control'&&!confirm('Control may disable currently enabled Home Assistant automations that target this device. When returning to Shadow, HomeMind restores only automations HomeMind disabled during takeover; automations already disabled by the user remain off. Enable Control?'))return;
    try{
      const r=await fetch(`api/agents/${encodeURIComponent(id)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
      if(!r.ok)throw new Error(await r.text());
      if(typeof window.load==='function')await window.load();else if(typeof load==='function')await load();
    }catch(e){
      alert('Nie udało się zmienić trybu: '+e.message);
      if(typeof window.load==='function')await window.load();else if(typeof load==='function')await load();
    }
  };

  setTimeout(decorateOwnership,0);
})();
