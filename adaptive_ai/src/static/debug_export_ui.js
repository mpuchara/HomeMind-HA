// One-click asynchronous bounded Correct learning diagnostic export for Live/Candidate cards.
(()=>{
  if(window.exportCorrectLearningDebug)return;

  const safePart=value=>String(value??'')
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g,'-')
    .replace(/^-+|-+$/g,'')
    .slice(0,80)||'agent';

  const stamp=()=>{
    const d=new Date();
    const p=n=>String(n).padStart(2,'0');
    return `${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  };

  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));

  async function parseResponse(response){
    const text=await response.text();
    let body=null;
    try{body=JSON.parse(text);}catch(_){}
    if(!response.ok){
      const message=body?.error||text||`HTTP ${response.status}`;
      throw Error(message.slice(0,1200));
    }
    if(!body||typeof body!=='object')throw Error('Debug endpoint did not return JSON');
    return body;
  }

  function triggerDownload(job,ref){
    const filename=job?.filename||`correct-learning-${safePart(ref)}-${stamp()}.json`;
    const anchor=document.createElement('a');
    anchor.href=`api/debug/correct-learning/jobs/${encodeURIComponent(job.job_id)}/download`;
    anchor.download=filename;
    anchor.style.display='none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    return filename;
  }

  async function startJob(ref){
    const response=await fetch(
      `api/agents/${encodeURIComponent(ref)}/debug/correct-learning/export`,
      {
        method:'POST',
        cache:'no-store',
        headers:{'Accept':'application/json','Content-Type':'application/json'},
        body:JSON.stringify({
          detail:'full',
          label_limit:256,
          window_seconds:120,
          raw_rows_per_label:768,
        }),
      }
    );
    return parseResponse(response);
  }

  async function waitForJob(job,button,original){
    const statusUrl=`api/debug/correct-learning/jobs/${encodeURIComponent(job.job_id)}`;
    for(let attempt=0;attempt<1200;attempt++){
      const response=await fetch(statusUrl,{
        cache:'no-store',
        headers:{'Accept':'application/json'},
        adaptiveAiTimeoutMs:5000,
      });
      const state=await parseResponse(response);
      const pct=Math.max(0,Math.min(100,Math.round(Number(state.progress||0)*100)));
      if(button)button.textContent=`Exporting… ${pct}%`;
      if(state.state==='done')return state;
      if(state.state==='failed')throw Error(state.error||'Debug export failed');
      await sleep(750);
    }
    throw Error('Debug export did not finish within 15 minutes');
  }

  window.exportCorrectLearningDebug=async(ref,button)=>{
    ref=String(ref||'').trim();
    if(!ref){alert('Debug export: missing agent/generation reference');return null;}
    if(button?.dataset?.debugExportBusy==='1')return null;

    const original=button?.textContent||'Export debug';
    if(button){
      button.dataset.debugExportBusy='1';
      button.disabled=true;
      button.textContent='Exporting… 0%';
    }
    try{
      const started=await startJob(ref);
      const finished=started.state==='done'?started:await waitForJob(started,button,original);
      const filename=triggerDownload(finished,ref);
      if(button){
        button.textContent='Downloaded ✓';
        setTimeout(()=>{if(button.isConnected)button.textContent=original;},1800);
      }
      return {filename,job:finished};
    }catch(error){
      console.error('Correct learning debug export failed',error);
      alert('Debug export failed: '+(error?.message||String(error)));
      if(button)button.textContent=original;
      return null;
    }finally{
      if(button){
        delete button.dataset.debugExportBusy;
        button.disabled=false;
      }
    }
  };
})();
