// Explain generation actions and surface hidden Candidate/background work on Live cards.
(()=>{
  if(window.__runtimeActivityUiInstalled)return;

  // candidate_preference_ui historically asks for Candidate Current/Desired four times
  // per second even when no Candidate exists. Keep its UI contract while collapsing the
  // backend traffic to <=1 request/s and zero requests when there is no Candidate card.
  const upstreamFetch=window.fetch.bind(window);
  let candidateLiveCache={at:0,body:'{"candidates":[]}'};
  window.fetch=(input,init={})=>{
    const method=String(init?.method||'GET').toUpperCase();
    const raw=typeof input==='string'?input:(input?.url||'');
    const path=String(raw).split('?')[0].replace(/^\.\//,'');
    if(method==='GET'&&path.endsWith('api/candidate-live')){
      if(!document.querySelector('.candidate-agent')){
        return Promise.resolve(new Response('{"candidates":[]}',{status:200,headers:{'Content-Type':'application/json'}}));
      }
      const now=performance.now();
      if(now-candidateLiveCache.at<1000){
        return Promise.resolve(new Response(candidateLiveCache.body,{status:200,headers:{'Content-Type':'application/json'}}));
      }
      return upstreamFetch(input,init).then(async response=>{
        if(!response.ok)return response;
        const body=await response.text();
        candidateLiveCache={at:performance.now(),body};
        return new Response(body,{status:response.status,headers:response.headers});
      });
    }
    return upstreamFetch(input,init);
  };

  const json=async(path,opts={})=>{
    const response=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});
    let body={};try{body=await response.json();}catch(_e){}
    if(!response.ok)throw Error(body.error||`HTTP ${response.status}`);
    return body;
  };
  const activeCandidateFor=id=>document.querySelector(`.candidate-agent[data-candidate-parent="${CSS.escape(String(id))}"]`);
  const setText=(node,value)=>{if(node&&node.textContent!==value)node.textContent=value;};

  async function autonomous(ref,button){
    const old=button?.textContent||'Autonomous learn';
    if(button){button.disabled=true;setText(button,'Checking…');}
    try{
      const s=await json(`api/agent-workflow/${encodeURIComponent(ref)}/status`,{adaptiveAiTimeoutMs:20000});
      const next=Number(s.generation_number||0)+1;
      if(!confirm(`Autonomous learning is a one-shot action, not a mode.\n\nCreate Candidate Gen ${next} from Gen ${s.generation_number}? The current generation stays unchanged and keeps serving. The Candidate will continue learning in Shadow.\n\nYou can stop/remove the Candidate at any time with Discard Candidate.`))return;
      if(button)setText(button,'Starting Candidate…');
      await json(`api/agent-workflow/${encodeURIComponent(ref)}/autonomous`,{method:'POST',body:'{}'});
      if(button)setText(button,'Candidate queued…');
      try{await window.refreshCandidates?.();}catch(_e){}
      try{await window.load?.();}catch(_e){}
      decorate();
    }catch(e){
      alert(`Autonomous failed: ${e?.message||e}`);
    }finally{
      if(button&&button.isConnected&&!activeCandidateFor(ref)){
        button.disabled=false;setText(button,old==='Autonomous'?'Autonomous learn':old);
      }
    }
  }

  async function discardCandidate(id,button){
    if(!confirm('Discard the active Candidate? The current Live generation, its model and history remain unchanged.'))return;
    const old=button.textContent;
    button.disabled=true;setText(button,'Discarding…');
    try{
      await json(`api/agents/${encodeURIComponent(id)}/candidate`,{method:'DELETE'});
      candidateLiveCache={at:0,body:'{"candidates":[]}'};
      try{await window.refreshCandidates?.();}catch(_e){}
      try{await window.load?.();}catch(_e){}
    }catch(e){alert(`Discard Candidate failed: ${e?.message||e}`);}
    finally{if(button.isConnected){button.disabled=false;setText(button,old);}decorate();}
  }

  function decorate(){
    document.querySelectorAll('#agents > .agent:not(.candidate-agent)').forEach(card=>{
      const id=String(card.dataset.agentId||'');if(!id)return;
      const actions=card.querySelector('.actions');if(!actions)return;
      const auto=actions.querySelector('[data-wf="auto"]');
      const candidate=activeCandidateFor(id);
      if(auto){
        setText(auto,candidate?'Candidate active':'Autonomous learn');
        const title=candidate
          ? 'A child Candidate already exists. Discard it or finish its lifecycle before creating another.'
          : 'Create one child Candidate that continues learning in Shadow. This is not a persistent mode.';
        if(auto.title!==title)auto.title=title;
        auto.disabled=Boolean(candidate);
        auto.onclick=e=>autonomous(id,e.currentTarget);
      }
      let discard=actions.querySelector('[data-wf="discard-candidate"]');
      if(candidate&&!discard){
        discard=document.createElement('button');
        discard.type='button';discard.className='ghost danger';discard.dataset.wf='discard-candidate';
        discard.textContent='Discard Candidate';
        discard.title='Stop/remove the child Candidate; Live remains unchanged.';
        actions.insertBefore(discard,actions.querySelector('[data-wf="settings"]')||null);
      }
      if(discard){
        if(!candidate){discard.remove();}
        else discard.onclick=e=>discardCandidate(id,e.currentTarget);
      }
    });

    document.querySelectorAll('#agents > .candidate-agent [data-wf="auto"]').forEach(button=>{
      if(!button.dataset.activityExplained){
        button.dataset.activityExplained='1';
        setText(button,'Autonomous learn');
        button.title='Create the next child generation. This is a one-shot learning action, not a mode.';
      }
    });
  }

  // The main task panel previously looked only at visible Live-agent training. Hidden
  // Candidate jobs could therefore use CPU while the UI said System ready.
  const baseRenderHistory=typeof renderHistory==='function'?renderHistory:null;
  if(baseRenderHistory){
    renderHistory=(h,status={})=>{
      const result=baseRenderHistory(h,status);
      const q=status.training_queue||{},active=q.active;
      if(active){
        const panel=document.querySelector('#taskPanel');
        if(panel){
          const reason=String(active.reason||'training');
          const labels={
            autonomous_continuation:'Autonomous Candidate training',
            teach_rl:'Candidate correction training',
            manual_rebuild:'Candidate rebuild',
            training:'Agent training',
            resume_training:'Agent training',
            full_rebuild:'Agent rebuild',
          };
          const title=labels[reason]||'Background training';
          const elapsed=active.started_at?Math.max(0,Date.now()/1000-Number(active.started_at)):0;
          panel.innerHTML=`<div class="history-head"><div><b>${esc(title)}</b><span>${esc(active.name||active.agent_id||'agent')} · ${esc(reason)}</span></div><div class="history-percent"><strong>ACTIVE</strong><small>${Math.round(elapsed)} s</small></div></div><div class="history-timing"><b>Low-power historical worker</b><span>Only one heavy job runs at a time. Live control remains available.</span></div>`;
        }
      }
      return result;
    };
  }

  const root=document.getElementById('agents');
  if(root)new MutationObserver(()=>queueMicrotask(decorate)).observe(root,{childList:true,subtree:true});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)decorate();});
  window.decorateRuntimeActivity=decorate;
  window.__runtimeActivityUiInstalled=true;
  decorate();
})();
