// Keep the currently working HA automation visible as the agent's behavioural baseline.
(() => {
  const rowsFor = a => {
    const r=a?.runtime||{}, meta=r.context_meta||{};
    const rows=(meta.automation_baseline_automations||r.automation_priors||[]).map(x=>({...x}));
    rows.sort((x,y)=>Number(Boolean(y.enabled))-Number(Boolean(x.enabled)) || String(x.name||x.entity_id||'').localeCompare(String(y.name||y.entity_id||'')));
    return rows;
  };
  const statusFor = (a,row) => {
    if(row.enabled)return 'active';
    const held=new Set(a?.control_lease?.disabled_automations||[]);
    return held.has(row.entity_id)?'held by Control':'known / off';
  };
  const baselineHtml = a => {
    const rows=rowsFor(a);
    if(!rows.length)return '';
    const meta=a?.runtime?.context_meta||{};
    const entities=(meta.automation_baseline_entities||[]).map(String);
    const primary=meta.primary_occupancy_source==='automation' ? meta.primary_occupancy_sensor : null;
    const chips=rows.slice(0,4).map(row=>`<span class="prior-chip" title="${esc(row.context_count||0)} context entities">${esc(row.name||row.entity_id)} · ${esc(statusFor(a,row))}</span>`).join('');
    const detail=entities.length?`<span>Baseline inputs: ${entities.map(esc).join(' · ')}${primary?` · primary ${esc(primary)}`:''}</span>`:`<span>Current HA automation is the first behavioural reference; extra sensors are evaluated as challengers.</span>`;
    return `<b>Automation baseline</b>${chips}${detail}`;
  };
  const decorate = a => {
    const card=document.querySelector(`article.agent[data-agent-id="${CSS.escape(String(a.id))}"]`);
    if(!card)return;
    let block=card.querySelector('[data-automation-baseline]');
    const html=baselineHtml(a);
    if(!html){ if(block)block.remove(); return; }
    if(!block){
      block=document.createElement('div');
      block.className='automation-prior';
      block.dataset.automationBaseline='1';
      const actions=card.querySelector('.actions');
      if(actions)card.insertBefore(block,actions); else card.appendChild(block);
    }
    block.innerHTML=html;
  };
  const decorateAll = () => { for(const a of (window.lastAgents||lastAgents||[]))decorate(a); };

  if(typeof renderAgents==='function'){
    const previous=renderAgents;
    renderAgents=(...args)=>{const result=previous(...args);decorateAll();return result;};
  }
  if(typeof window.updateAgentLive==='function'){
    const previousLive=window.updateAgentLive;
    window.updateAgentLive=a=>{const result=previousLive(a);decorate(a);return result;};
  }
  if(typeof window.agentDiagnostics==='function'){
    const previousDiagnostics=window.agentDiagnostics;
    window.agentDiagnostics=a=>{
      const html=baselineHtml(a);
      return `${html?`<div class="context-all" data-automation-baseline-diagnostics>${html}</div>`:''}${previousDiagnostics(a)}`;
    };
  }
  decorateAll();
})();
