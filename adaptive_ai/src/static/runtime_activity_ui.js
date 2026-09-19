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
  const TRAINING_REASON_LABELS={
    autonomous_continuation:'Autonomous Candidate training',
    teach_rl:'Candidate correction training',
    manual_rebuild:'Candidate rebuild',
    training:'Agent training',
    resume_training:'Agent training',
    full_rebuild:'Agent rebuild',
  };

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
    const agents=(()=>{try{return Array.isArray(lastAgents)?lastAgents:[];}catch(_){return [];}})();
    document.querySelectorAll('#agents > .agent:not(.candidate-agent)').forEach(card=>{
      const id=String(card.dataset.agentId||'');if(!id)return;
      const actions=card.querySelector('.actions');if(!actions)return;
      const agent=agents.find(x=>String(x.id)===id);
      if(agent){
        const training=String(agent.training_state||agent.runtime?.training_state||'paused');
        const neverTrained=training==='waiting'||training==='needs_retrain'||(training==='paused'&&agent.benchmark_score==null&&!agent.training_cursor_ts);
        const settings=actions.querySelector('[data-wf="settings"]')||null;
        if(!neverTrained&&training!=='training'&&agent.mode==='paused'&&!actions.querySelector('[data-wf="shadow"]')){
          const shadow=document.createElement('button');
          shadow.type='button';shadow.className='primary';shadow.dataset.wf='shadow';shadow.textContent='Start Shadow';
          shadow.onclick=()=>window.setMode?.(id,'shadow');
          actions.insertBefore(shadow,actions.firstChild);
        }
        if(training==='paused'&&!neverTrained&&!actions.querySelector('[data-wf="resume"], .resume')){
          const resume=document.createElement('button');
          resume.type='button';resume.className='ghost resume';resume.dataset.wf='resume';resume.textContent='Resume training';
          resume.onclick=()=>window.resumeLearning?.(id);
          actions.insertBefore(resume,settings);
        }
      }
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
          const reasonLabel=TRAINING_REASON_LABELS[reason]||'Background training';
          const lp=status.low_power_runtime||{};
          const duty=Math.round(Number(lp.training_cpu_duty_cycle||0)*100);
          const slice=Math.round(Number(lp.max_continuous_work_ms||0));
          const observed=Math.round(Number(lp.max_observed_slice_ms||0));
          const overruns=Number(lp.slice_overruns||0);
          const replayBatch=Math.round(Number(lp.experience_batch_rows||0));
          const budget=duty?`CPU budget ${duty}%${slice?` · max slice ${slice} ms`:''}${replayBatch?` · replay batch ${replayBatch}`:''}`:'Pi-safe CPU budget';
          const observedText=observed?` · longest slice ${observed} ms${overruns?` · ${overruns} overrun${overruns===1?'':'s'}`:''}`:'';
          const timing=panel.querySelector('.history-timing span');
          if(timing)timing.textContent=`${timing.textContent||''} ${reasonLabel} · ${budget}${observedText}. Training yields between bounded work slices so Ingress and realtime control keep CPU priority.`.trim();
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
